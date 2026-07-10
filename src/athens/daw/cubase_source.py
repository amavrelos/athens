"""Cubase adapter — the mixer layer.

Proves the multi-DAW seam: the bridge and device dialect are DAW-agnostic; a
DAW is just a SysexDawSource that translates its session state. Cubase exposes
that state through the MIDI Remote API (JavaScript, since v12); the script in
the cubase/ MIDI Remote script binds host values (track names/volumes/pan/mute/
solo/selection, transport) and streams them over the `roto-bridge` virtual MIDI
pair using the compact contract in `cubase_contract` — the same role
roto_fx_feed.lua plays for REAPER over file IPC + OSC. This source decodes that
stream into the SysexDawSource callbacks; nothing device-facing changes (the
LogicBridge keeps speaking the Logic dialect to the ROTO).

The wire is symmetric: the same contract commands report state (host -> here)
and set it (here -> host), so `set_track_volume` is just the volume command
sent the other way.

Plugin-parameter mode (the focused plugin's params, for the link registry) is a
second layer on the same pair — not in this file yet.

Pro Tools stays mixer-only: its HUI exposes no plugin params and the PTSL gRPC
API exposes none either (verified), so the plugin/link feature can't live there.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

from ..roto.sysex_client import MidiPort
from . import cubase_contract as wire
from .echo import EchoGate
from .source import (
    TRACK_FLAGS, DeviceInfo, PluginParam, SysexDawSource, TrackInfo,
    TransportAction, TransportState,
)

log = logging.getLogger(__name__)

BRIDGE_PORT = "roto-bridge"          # macOS exposes the JS's virtual pair as
                                     # "IAC Driver roto-bridge" — match by substring
# Cubase's MIDI Remote script goes briefly silent when it re-binds/reloads (seen
# ~8 s on a plugin/track change). Don't blank the device on the first missed
# keepalive — only declare the session gone after this long of CONTINUOUS
# silence, so a re-bind hiccup no longer wipes and re-floods the surface.
_GONE_GRACE_S = 6.0


class CubaseSysexSource(SysexDawSource):
    DAW_NAME = "Cubase"

    def __init__(self, port: Optional[MidiPort] = None) -> None:
        super().__init__()
        self._port = port            # injectable; real port opened in start()
        self._count = 0
        self._track: dict[int, TrackInfo] = {}
        self._selected = 0
        self._transport = TransportState()
        self._daw_tag = ""           # identity the script announced (HELLO)
        self._got_diag = False       # seen the per-block DIAG this session yet?
        self._script_version = None  # version from the HELLO token ("" = a
        #                              pre-version script); None until first HELLO
        #                              so even "" fires once (base on_script_version)
        self._last_rx = 0.0          # monotonic time of the last inbound frame;
        #                              lets the DAW monitor confirm liveness on
        #                              THIS open port (see check_alive)
        self._alive = True           # optimistic: edges fire on transitions only,
        #                              so a live Cubase produces zero extra events
        self._gone_since = 0.0       # monotonic of the first missed keepalive in
        #                              the current silence (0 = answering)
        self._stop_evt = threading.Event()
        self._poll: Optional[threading.Thread] = None
        # plugin layer: the selected track's inserts + the focused plugin's
        # exposed params (Cubase's focused quick controls — 8 params/page)
        self._dev_count = 0
        self._device: dict[int, DeviceInfo] = {}
        self._focused_device = 0
        self._param_count = 0
        self._param: dict[int, PluginParam] = {}
        # learn: LEARN on the device -> a genuine Cubase param move (not our own
        # write bouncing back) is the "grab" that triggers the device sweep.
        self._learn_armed = False
        self._echo = EchoGate()
        # grab-inference debounce: on plugin focus Cubase DUMPS all 8 bank params
        # at once — that is NOT a user grab. Collect a short burst and judge it
        # together: many distinct slots == a dump (ignore); one isolated changed,
        # non-echo param == the grab.
        self._grab_lock = threading.Lock()
        self._grab_seen: set = set()        # every slot seen in the burst
        self._grab_moved: dict = {}         # slot -> value: changed & non-echo
        self._grab_timer: Optional[threading.Timer] = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._port is None:
            try:
                import mido
                from ..roto.sysex_client import MidoMidiPort
                inp = next((n for n in mido.get_input_names()
                            if BRIDGE_PORT in n), None)
                outp = next((n for n in mido.get_output_names()
                             if BRIDGE_PORT in n), None)
                if inp is None or outp is None:
                    raise RuntimeError("no MIDI port containing %r" % BRIDGE_PORT)
                self._port = MidoMidiPort(inp, outp)
                log.info("Cubase source bound to MIDI in=%r out=%r", inp, outp)
            except Exception as exc:  # noqa: BLE001 - no port / no midi extra
                log.warning("Cubase: no MIDI pair matching '%s' (%s). Install the "
                            "MIDI Remote script from cubase/ and create the "
                            "virtual pair — running with an empty session.",
                            BRIDGE_PORT, exc)
                return
        self._port.on_receive = self._on_midi
        log.info("Cubase source listening for '%s'", BRIDGE_PORT)
        # WHO probe OFF the startup thread: a blocking MIDI send on the freshly
        # opened loopback can wedge launch before the Ctrl-C net is armed (an
        # un-interruptible startup hang). A daemon timer also lets the reply land
        # after the UI has subscribed, so the handshake + DIAG show in the trace.
        t = threading.Timer(0.4, self._probe_who)
        t.daemon = True
        t.start()
        # own liveness poller: Cubase quitting must blank the device / UI (the
        # on_daw_alive edge) in EVERY mode — the --daw auto monitor also calls
        # check_alive, but under an explicit --daw cubase nobody else would
        self._stop_evt = threading.Event()
        self._poll = threading.Thread(target=self._liveness, daemon=True,
                                      name="cubase-liveness")
        self._poll.start()

    def _probe_who(self) -> None:
        """Ask the running script to announce itself + ship its per-feature DIAG."""
        try:
            self._send(wire.hello())
        except Exception:      # noqa: BLE001 - best-effort, never crash the timer
            log.debug("Cubase WHO probe failed", exc_info=True)

    def refresh_state(self) -> None:
        """Make the script re-announce its full state (a plain HELLO triggers
        the mixer replay + insert list + DIAG). Used when the auto monitor
        ADOPTS this already-open watcher: unlike a fresh source there is no
        start() to re-announce it, so the device + UI would otherwise stay empty
        until the next Cubase change."""
        self._probe_who()

    def _liveness(self) -> None:
        while not self._stop_evt.wait(3.0):
            self.check_alive()          # fires the on_daw_alive edges itself

    def stop(self) -> None:
        self._stop_evt.set()
        poll, self._poll = self._poll, None
        if poll is not None and poll is not threading.current_thread():
            poll.join(timeout=1.0)
        # null the port: the service detach path stops then RE-STARTS this same
        # instance — a stale closed port would make start() skip re-opening and
        # leave a silently dead session
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:         # noqa: BLE001 - best-effort
                pass

    def feed_running(self) -> bool:
        """The bridge/UI treat a missing feed like REAPER's: no live session."""
        return self._port is not None

    def check_alive(self, timeout: float = 0.6) -> bool:
        """Is the Cubase script still answering? Confirmed on our ALREADY-OPEN
        port — this NEVER opens a new MIDI port. The DAW hot-swap monitor calls
        it every poll instead of re-opening the roto-bridge pair, which would
        create/destroy a CoreMIDI client each time and deadlock openPort against
        the live device port (un-interruptible hang).

        Fires the on_daw_alive EDGES itself (Cubase quit -> blank the device;
        return -> re-flood), so it works the same from the auto-mode monitor and
        from our own liveness poller. The GONE edge is debounced: a brief silence
        (script re-bind) is ridden out, and only _GONE_GRACE_S of continuous
        silence blanks the device. Time-based, so the two pollers that call this
        can't race the debounce. Returns the debounced state — but NEVER True
        for a watcher that has not heard Cubase at all: the debounce's
        optimistic initial _alive=True exists so an ESTABLISHED session rides
        out hiccups, not so the auto-DAW monitor adopts a phantom Cubase at
        its first tick (~3s, well inside the grace window)."""
        ok = self._check_alive(timeout)
        now = time.monotonic()
        if ok:
            self._gone_since = 0.0
            if not self._alive:
                self._alive = True
                log.info("Cubase answering again")
                self._fire(self.on_daw_alive, True)
        else:
            if self._gone_since == 0.0:
                self._gone_since = now            # first miss: start the clock
            elif self._alive and now - self._gone_since >= _GONE_GRACE_S:
                self._alive = False
                log.info("Cubase stopped answering — session gone (silent %.0fs)",
                         now - self._gone_since)
                self._fire(self.on_daw_alive, False)
        return self._alive and self._last_rx > 0.0

    def _check_alive(self, timeout: float) -> bool:
        if self._port is None:
            return False
        if self._last_rx and (time.monotonic() - self._last_rx) < 4.0:
            return True               # a recent frame already proves it's live
        before = self._last_rx
        try:
            # tagged keepalive: the script answers a bare HELLO with the full
            # DIAG + insert-list replay (wanted on reconnect), but a "ka" gets
            # just the HELLO echo — an idle session isn't re-pushed every poll
            self._send(wire.hello("ka"))
        except Exception:      # noqa: BLE001 - a dead port reads as not-alive
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._last_rx != before:           # the script answered
                return True
            time.sleep(0.02)
        return False

    # -- session state -------------------------------------------------------
    def tracks(self) -> List[TrackInfo]:
        return [self._track.get(i) or TrackInfo(i, "")
                for i in range(self._count)]

    def selected_track(self) -> int:
        return self._selected

    def transport(self) -> TransportState:
        return self._transport

    # -- host -> here: decode the contract ----------------------------------
    def _on_midi(self, data: bytes) -> None:
        if log.isEnabledFor(logging.DEBUG):
            log.debug("Cubase bridge rx %s", bytes(data).hex(" "))
        msg = wire.parse(bytes(data))
        if msg is None:
            return
        if msg.direction != wire.DIR_TO_ATHENS:
            return                    # our own control echo looping back
        self._last_rx = time.monotonic()   # liveness beat for the DAW monitor
        if self.on_frame is not None:
            self.on_frame("rx", bytes(data))
        cmd, p = msg.cmd, msg.payload
        if cmd is wire.Cmd.COUNT:
            self._count = wire.parse_u14(p)
            self._fire(self.on_tracks_changed)
        elif cmd is wire.Cmd.NAME:
            self._at(wire.parse_u14(p)).name = \
                bytes(p[2:]).decode("ascii", "replace")
            self._fire(self.on_tracks_changed)
        elif cmd is wire.Cmd.VOLUME:
            idx = wire.parse_u14(p)
            val = wire.norm(wire.parse_u14(p, 2))
            self._at(idx).volume = val
            self._fire(self.on_track_volume, idx, val)
        elif cmd is wire.Cmd.PAN:
            idx = wire.parse_u14(p)
            val = wire.norm(wire.parse_u14(p, 2))
            self._at(idx).pan = val
            self._fire(self.on_track_pan, idx, val)
        elif cmd is wire.Cmd.FLAG:
            idx = wire.parse_u14(p)
            name = TRACK_FLAGS[p[2]] if p[2] < len(TRACK_FLAGS) else None
            if name is not None:
                on = bool(p[3])
                setattr(self._at(idx), name, on)
                self._fire(self.on_track_flag, idx, name, on)
        elif cmd is wire.Cmd.SELECT:
            self._selected = wire.parse_u14(p)
            self._fire(self.on_selected_track_changed)
        elif cmd is wire.Cmd.TRANSPORT:
            bits = p[0] if p else 0
            self._transport = TransportState(
                playing=bool(bits & wire.T_PLAY),
                recording=bool(bits & wire.T_RECORD),
                loop=bool(bits & wire.T_LOOP))
            self._fire(self.on_transport_changed)
        elif cmd is wire.Cmd.HELLO:
            # token is "cubase <version>" (new) or bare "cubase" (older build).
            # Keep the identity word as the tag; a missing version -> "" -> the
            # service treats it as an older build and prompts a reload.
            parts = bytes(p).decode("ascii", "replace").split(None, 1)
            self._daw_tag = parts[0] if parts else ""
            ver = parts[1] if len(parts) > 1 else ""
            if ver != self._script_version:
                self._script_version = ver
                self._fire(self.on_script_version, ver)
            if not self._got_diag:       # a page-activate HELLO carries no DIAG;
                self._probe_who()         # re-probe so the script ships it once

        elif cmd is wire.Cmd.VU:
            idx = wire.parse_u14(p)
            lvl = wire.norm(wire.parse_u14(p, 2))
            self._fire(self.on_track_vu, idx, lvl, lvl)   # mono meter -> L=R
        elif cmd is wire.Cmd.DEVICE_COUNT:
            self._dev_count = wire.parse_u14(p)
            self._fire(self.on_devices_changed)
        elif cmd is wire.Cmd.DEVICE_NAME:
            self._dev(wire.parse_u14(p)).name = \
                bytes(p[2:]).decode("ascii", "replace")
            self._fire(self.on_devices_changed)
            self._ensure_named_focus()
        elif cmd is wire.Cmd.DEVICE_ENABLED:
            self._dev(wire.parse_u14(p)).enabled = bool(p[2])
            self._fire(self.on_devices_changed)
        elif cmd is wire.Cmd.FOCUS_DEVICE:
            # gate on an ACTUAL change: page re-activation re-pulses the current
            # slot, and an ungated clear+refire refloods the device with an
            # empty param set (stale-knob flash) for nothing
            idx = wire.parse_u14(p)
            if idx != self._focused_device:
                self._focused_device = idx
                self._param.clear()           # new plugin -> fresh param set
                self._param_count = 0
                self._fire(self.on_plugin_focus_changed)
        elif cmd is wire.Cmd.PARAM_COUNT:
            self._param_count = wire.parse_u14(p)
        elif cmd is wire.Cmd.PARAM_NAME:
            self._par(wire.parse_u14(p)).name = \
                bytes(p[2:]).decode("ascii", "replace")
        elif cmd is wire.Cmd.PARAM_VALUE:
            slot = wire.parse_u14(p)
            value = wire.norm(wire.parse_u14(p, 2))
            old = self._par(slot).value
            self._par(slot).value = value
            self._fire_param(slot)
            # LEARN: Cubase has no "user grabbed this param" signal (REAPER does)
            # — infer it. But a plugin-focus BANK DUMP reports all 8 params at
            # once and must NOT read as 8 grabs (that swept param 0 every time).
            # Collect the burst; _commit_learn_grab judges it as a whole.
            if self._learn_armed and self.on_param_touched:
                self._note_learn_move(slot, value, abs(old - value) > 1e-4)
        elif cmd is wire.Cmd.PARAM_DISPLAY:
            slot = wire.parse_u14(p)
            self._par(slot).display = bytes(p[2:]).decode("ascii", "replace")
            self._fire_param(slot)
        elif cmd is wire.Cmd.DIAG:
            self._got_diag = True
            # per-block load status the script ships on a WHO probe: which of
            # pan/mute/solo/select/vu/paging the host API actually accepted.
            log.info("cubase script diag: %s",
                     bytes(p).decode("ascii", "replace"))

    def _ensure_named_focus(self) -> None:
        """Insert-only tracks: slot 0 (the instrument) is an empty gap, and an
        empty-named focus makes the bridge push NOTHING to the device (the
        gap-slot bug). When the focused slot has no name but a named device
        exists, focus the first named one — through the ONE existing focus
        mechanism, so the Cubase script switches its subpage."""
        devices = self.devices()
        cur = next((d for d in devices if d.index == self._focused_device), None)
        if cur is not None and cur.name:
            return                                # already on a real plugin
        named = next((d for d in devices if d.name), None)
        if named is None:
            return                                # nothing to focus yet
        log.info("auto-focus: slot %d is empty -> first named plugin %r (slot %d)",
                 self._focused_device, named.name, named.index)
        self.set_selected_device(named.index)
        self._fire(self.on_plugin_focus_changed)  # re-push with the real focus

    def _dev(self, index: int) -> DeviceInfo:
        d = self._device.get(index)
        if d is None:
            d = self._device[index] = DeviceInfo(index, "")
            if index >= self._dev_count:
                self._dev_count = index + 1
        return d

    def _par(self, slot: int) -> PluginParam:
        p = self._param.get(slot)
        if p is None:
            p = self._param[slot] = PluginParam(slot, "")
            if slot >= self._param_count:
                self._param_count = slot + 1
        return p

    def _fire_param(self, slot: int) -> None:
        p = self._param.get(slot)
        if p is not None:
            self._fire(self.on_device_param_value, self._focused_device,
                       slot, p.value, p.display)

    def _at(self, index: int) -> TrackInfo:
        t = self._track.get(index)
        if t is None:
            t = self._track[index] = TrackInfo(index, "")
            if index >= self._count:
                self._count = index + 1
        return t

    @staticmethod
    def _fire(cb, *args) -> None:
        if cb is not None:
            cb(*args)

    # -- here -> host: send the same contract the other way -----------------
    def _send(self, frame: bytes) -> None:
        if self._port is not None:
            out = wire.as_control(frame)              # tag Athens->Cubase
            self._port.send(out)
            if self.on_frame is not None:
                self.on_frame("tx", out)

    def set_selected_track(self, index: int) -> None:
        if 0 <= index < max(self._count, 1):
            self._selected = index
            self._send(wire.select(index))
            self._fire(self.on_selected_track_changed)

    def set_track_volume(self, index: int, value: float) -> None:
        self._at(index).volume = value
        self._send(wire.volume(index, value))

    def set_track_pan(self, index: int, value: float) -> None:
        self._at(index).pan = value
        self._send(wire.pan(index, value))

    def set_track_flag(self, index: int, flag: str, on: bool) -> None:
        if flag in TRACK_FLAGS:
            setattr(self._at(index), flag, on)
            self._send(wire.flag(index, wire.Flag(TRACK_FLAGS.index(flag)), on))

    def transport_action(self, action: TransportAction, pressed: bool) -> None:
        if not pressed:
            return
        st = self._transport
        if action is TransportAction.PLAY:
            st.playing = not st.playing
        elif action is TransportAction.STOP:
            st.playing = False
        elif action is TransportAction.RECORD:
            st.recording = not st.recording
        elif action is TransportAction.LOOP:
            st.loop = not st.loop
        self._send(wire.transport(st.playing, st.recording, st.loop))

    def page(self, delta: int) -> bool:
        """Scroll Cubase's mixer bank (the ROTO page arrows). The script's
        mNextBank/mPrevBank re-streams the 8 channels for the new window, so
        strips 0..7 become the next/prev 8 tracks. Returns True so the bridge
        lets the source page itself — REAPER instead windows its own full track
        list. (Absolute-index labelling of the scrolled window is a follow-up.)"""
        self._send(wire.page(delta))
        return True

    # -- plugin layer --------------------------------------------------------
    def devices(self) -> List[DeviceInfo]:
        return [self._device.get(i) or DeviceInfo(i, "")
                for i in range(self._dev_count)]

    def selected_device(self) -> int:
        return self._focused_device

    def set_selected_device(self, index: int) -> None:
        # the change-gated param reset lives HERE (not only on the echo): we set
        # _focused_device before Cubase echoes FOCUS_DEVICE back, so the echo's
        # own change-gate sees "no change" — without this, a deliberate focus
        # switch would keep showing the previous plugin's params
        if index != self._focused_device:
            self._focused_device = index
            self._param.clear()
            self._param_count = 0
        self._send(wire.focus_device(index))

    def set_device_enabled(self, index: int, enabled: bool) -> None:
        self._dev(index).enabled = enabled
        self._send(wire.device_enabled(index, enabled))

    def device_params(self, device_index: int) -> List[PluginParam]:
        # Cubase exposes only the FOCUSED plugin's params (focused quick
        # controls); other inserts report an empty page until focused.
        if device_index != self._focused_device:
            return []
        return [self._param.get(s) or PluginParam(s, "")
                for s in range(self._param_count)]

    def set_learn_armed(self, armed: bool) -> None:
        # device LEARN toggles this; while armed, a non-echo param move fires
        # on_param_touched (see the PARAM_VALUE handler + _commit_learn_grab).
        self._learn_armed = armed
        if not armed:                       # drop any half-collected burst
            with self._grab_lock:
                if self._grab_timer is not None:
                    self._grab_timer.cancel()
                    self._grab_timer = None
                self._grab_seen.clear()
                self._grab_moved.clear()
        log.info("cubase learn armed=%s", armed)

    def _note_learn_move(self, slot: int, value: float, changed: bool) -> None:
        """Collect PARAM_VALUE activity during learn; a short quiet window later
        _commit_learn_grab decides. A plugin-focus bank dump lights every slot at
        once (not a grab); only an isolated changed, non-echo param is one."""
        with self._grab_lock:
            self._grab_seen.add(slot)
            if changed and not self._echo.is_echo("p%d" % slot, value):
                self._grab_moved[slot] = value
            if self._grab_timer is not None:
                self._grab_timer.cancel()
            self._grab_timer = threading.Timer(0.2, self._commit_learn_grab)
            self._grab_timer.daemon = True
            self._grab_timer.start()

    def _commit_learn_grab(self) -> None:
        with self._grab_lock:
            seen = len(self._grab_seen)
            moved = dict(self._grab_moved)
            self._grab_seen.clear()
            self._grab_moved.clear()
            self._grab_timer = None
        if not (self._learn_armed and self.on_param_touched):
            return
        if seen >= 3:
            log.debug("learn: bank dump (%d params at once) — not a grab", seen)
            return
        if len(moved) == 1:
            slot, value = next(iter(moved.items()))
            log.info("learn: GRAB param %d = %.3f -> sweep", slot, value)
            self._fire(self.on_param_touched, self._focused_device, slot)
        elif len(moved) > 1:
            log.debug("learn: %d params moved together — ambiguous, no grab",
                      len(moved))

    def set_device_param(self, device_index: int, param_index: int,
                         value: float) -> None:
        self._par(param_index).value = value
        self._echo.sent("p%d" % param_index, value)   # so its echo isn't a "grab"
        self._send(wire.param_value(param_index, value))
