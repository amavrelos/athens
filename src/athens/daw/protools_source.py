"""Pro Tools adapter — the mixer layer, over HUI.

Pro Tools exposes no plugin parameters and no track colours to any surface, and
its HUI meters are coarse mono — so this is a MIXER-ONLY source (faders, pan,
mute/solo/select/arm, transport, banking). The plugin/link layer stays inert
(the SysexDawSource defaults), and VU is intentionally not wired.

Athens emulates an 8-strip HUI surface over an IAC bus (Pro Tools' MIDI
Controllers list shows only IAC buses + hardware, never app-created virtual
ports). Athens auto-binds a spare bus; the user points Pro Tools' HUI at the
SAME bus (Setup -> Peripherals -> MIDI Controllers -> Type: HUI, # Ch's: 8).
Pro Tools then pings ~1/s and pushes the 8-strip surface state, which we decode
into the SysexDawSource model; the ROTO's moves go back the other way as HUI.
The bus is a LOOPBACK — everything we transmit is echoed back at us — so the
codec gates the ping (hui.py) and decodes only DAW->surface messages.

The wire codec is `hui`; this file is the state machine + Athens-facing model,
mirroring the role cubase_source plays for the Cubase MIDI Remote script.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

from ..sysex.constants import TransportAction
from . import hui
from .source import SysexDawSource, TrackInfo, TransportState

log = logging.getLogger(__name__)

HUI_PORT = "roto-hui"            # IAC-bus name substring Pro Tools' HUI points at
#                                  (must be a real IAC bus; see _pick_hui_bus)
_FADER_RELEASE_S = 0.25          # untouch a fader this long after the last move
_PING_GONE_S = 3.0               # no ping for this long -> Pro Tools is gone
# absolute pan (from the ROTO) -> relative V-Pot steps. HUI has no absolute-pan
# set, so we send the turn incrementally; this scales a full 0..1 sweep to ~40
# detents. Tune on hardware if pan feels too fast/slow.
_PAN_STEPS = 40
_PAN_GESTURE_S = 0.5             # idle gap that ends a pan gesture: the delta
#                                  baseline re-anchors to Pro Tools' reported pan


def _pick_hui_bus(names: List[str]) -> Optional[str]:
    """The IAC bus Pro Tools' HUI talks over. Pro Tools lists only IAC buses +
    hardware (never app-created virtual ports), so this MUST be a real IAC bus:
    prefer a dedicated 'roto-hui', else the first IAC bus that isn't Cubase's
    'roto-bridge' pair (e.g. the default 'IAC Driver Bus 1')."""
    pairs = [(n, n.lower()) for n in names]
    for n, low in pairs:
        if HUI_PORT in low:
            return n
    for n, low in pairs:
        if "iac" in low and "roto-bridge" not in low:
            return n
    return None


class ProToolsSource(SysexDawSource):
    DAW_NAME = "Pro Tools"

    def __init__(self, port=None) -> None:
        super().__init__()
        self._port = port                       # injectable for tests
        self._dec = hui.HuiDecoder()
        self._tracks = [TrackInfo(i, "") for i in range(hui.NUM_STRIPS)]
        self._selected = 0
        self._transport = TransportState()
        self._touch: dict[int, threading.Timer] = {}   # ch -> fader-release timer
        self._touch_lock = threading.Lock()     # arm/release are on different threads
        # outgoing pan-delta baseline per channel: (value, last_move_monotonic).
        # Kept SEPARATE from _tracks[].pan — Pro Tools' LED-ring feedback is
        # ~11-step coarse and lands mid-gesture; using the reported value as the
        # accumulator corrupts step counts (can even flip direction).
        self._pan_out: dict[int, tuple] = {}
        self._last_ping = 0.0
        self._alive = False
        self._stop_evt = threading.Event()
        self._poll: Optional[threading.Thread] = None
        self._running = False

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return                      # idempotent: detach/revive may re-enter
        if self._port is None:
            try:
                import mido
                from ..roto.sysex_client import MidoMidiPort
                inp = _pick_hui_bus(mido.get_input_names())
                outp = _pick_hui_bus(mido.get_output_names())
                if inp is None or outp is None:
                    raise RuntimeError("no IAC bus available")
                self._port = MidoMidiPort(inp, outp)
                log.info("Pro Tools HUI bound to IAC bus %r. Point Pro Tools at the "
                         "SAME bus: Setup > Peripherals > MIDI Controllers > "
                         "Type=HUI, Receive From & Send To = %r, # Ch's = 8. "
                         "(details: docs/protools-hui.md)", inp, inp)
            except Exception as exc:  # noqa: BLE001 - no midi extra / no IAC bus
                log.warning("Pro Tools: no IAC bus found (%s). In Audio MIDI Setup, "
                            "enable the IAC Driver (any bus works — e.g. the default "
                            "'Bus 1'), then point Pro Tools' HUI at it. Running with "
                            "an empty session.", exc)
                return
        self._port.on_receive = self._on_midi
        self._running = True
        self._stop_evt = threading.Event()      # fresh per run (revive-safe)
        # announce presence so Pro Tools (re)floods the 8-strip surface state;
        # a still-bad port must degrade, never abort start() half-initialised
        try:
            self._send(hui.system_reset())
        except Exception:      # noqa: BLE001 - keepalive will pick it up
            log.debug("HUI system-reset failed", exc_info=True)
        self._poll = threading.Thread(target=self._liveness, daemon=True,
                                      name="protools-liveness")
        self._poll.start()

    def stop(self) -> None:
        self._running = False
        self._stop_evt.set()
        poll, self._poll = self._poll, None
        if poll is not None and poll is not threading.current_thread():
            poll.join(timeout=1.5)      # no second liveness thread on a revive
        with self._touch_lock:
            timers = list(self._touch.values())
            self._touch.clear()
        for t in timers:
            t.cancel()
        # null the port: the service DETACH path stops this source then re-starts
        # the SAME instance ("revive") — a stale closed port would make start()
        # skip re-opening and send on a dead port, killing the session
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:         # noqa: BLE001 - best-effort
                pass

    def feed_running(self) -> bool:
        return self._port is not None

    def _liveness(self) -> None:
        """Pro Tools pings ~1/s; if it stops, the session is gone. Fire the edge
        so the bridge blanks the device instead of showing a ghost mixer."""
        while not self._stop_evt.wait(1.0):
            if self._alive and time.monotonic() - self._last_ping > _PING_GONE_S:
                self._alive = False
                self._fire(self.on_daw_alive, False)

    def refresh_state(self) -> None:
        # a (re)connected device wants fresh state: nudge Pro Tools to re-flood.
        self._send(hui.system_reset())

    # -- session model ------------------------------------------------------
    def tracks(self) -> List[TrackInfo]:
        return list(self._tracks)

    def selected_track(self) -> int:
        return self._selected

    def transport(self) -> TransportState:
        return self._transport

    # -- Pro Tools -> here: decode HUI --------------------------------------
    def _on_midi(self, data: bytes) -> None:
        raw = bytes(data)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("HUI rx %s", raw.hex(" "))
        for ev in self._dec.feed(raw):
            self._handle(ev)

    def _handle(self, ev) -> None:
        kind = ev[0]
        if kind == "ping":
            self._send(hui.ping_reply())        # KEEPALIVE — reply to every ping
            self._last_ping = time.monotonic()
            if not self._alive:
                self._alive = True
                log.info("Pro Tools HUI: handshake up — ping received, replying")
                self._fire(self.on_daw_alive, True)
        elif kind == "fader":
            _, ch, v = ev
            self._tracks[ch].volume = v
            self._fire(self.on_track_volume, ch, v)
        elif kind == "pan":
            _, ch, v = ev
            self._tracks[ch].pan = v
            self._fire(self.on_track_pan, ch, v)
        elif kind == "flag":
            _, ch, flag, on = ev
            setattr(self._tracks[ch], flag, on)
            self._fire(self.on_track_flag, ch, flag, on)
        elif kind == "select":
            _, ch, on = ev
            if on and ch != self._selected:
                self._selected = ch
                self._fire(self.on_selected_track_changed)
        elif kind == "transport":
            _, name, on = ev
            if name == "play":
                self._transport.playing = on
            elif name == "stop" and on:
                self._transport.playing = False
            elif name == "record":
                self._transport.recording = on
            self._fire(self.on_transport_changed)
        elif kind == "name":
            _, ch, text = ev
            if self._tracks[ch].name != text:
                self._tracks[ch].name = text
                self._fire(self.on_tracks_changed)

    # -- here -> Pro Tools: apply the ROTO's moves as HUI -------------------
    def _send(self, data: bytes) -> None:
        if self._port is not None and data:
            if log.isEnabledFor(logging.DEBUG):
                log.debug("HUI tx %s", bytes(data).hex(" "))
            self._port.send_raw(data)

    def set_track_volume(self, index: int, value: float) -> None:
        if not 0 <= index < hui.NUM_STRIPS:
            return
        self._tracks[index].volume = value
        self._arm_touch(index)                  # touch so PT stops fighting the motor
        self._send(hui.fader(index, value))

    def _arm_touch(self, ch: int) -> None:
        # Timer.cancel() cannot stop a callback that already started; without the
        # lock + the identity check in _release, a move landing as the release
        # fires would send touch-OFF mid-gesture and skip the new touch-ON, so
        # Pro Tools motor-fights the user / stops writing touch automation.
        with self._touch_lock:
            t = self._touch.pop(ch, None)
            if t is not None:
                t.cancel()              # a fired one is caught by _release's check
            def _fire() -> None:
                self._release(ch, rel)
            rel = threading.Timer(_FADER_RELEASE_S, _fire)
            rel.daemon = True
            self._touch[ch] = rel
        if t is None:
            self._send(hui.fader_touch(ch, True))
        rel.start()

    def _release(self, ch: int, timer: threading.Timer) -> None:
        with self._touch_lock:
            if self._touch.get(ch) is not timer:
                return                  # superseded: a newer move owns the fader
            del self._touch[ch]
        self._send(hui.fader_touch(ch, False))

    def set_track_pan(self, index: int, value: float) -> None:
        if not 0 <= index < hui.NUM_STRIPS:
            return
        # deltas accumulate against the PRIVATE baseline (see __init__): it
        # re-anchors to PT's reported pan between gestures, never mid-gesture
        now = time.monotonic()
        base, ts = self._pan_out.get(index, (None, 0.0))
        if base is None or now - ts > _PAN_GESTURE_S:
            base = self._tracks[index].pan
        steps = round((value - base) * _PAN_STEPS)
        if steps:
            base += steps / _PAN_STEPS
            self._send(hui.vpot_delta(index, steps))
        self._pan_out[index] = (base, now)      # sub-step turns keep accumulating

    def set_track_flag(self, index: int, flag: str, on: bool) -> None:
        port = hui.FLAG_PORT.get(flag)          # "monitoring" -> None (no HUI switch)
        if port is None or not 0 <= index < hui.NUM_STRIPS:
            return
        if getattr(self._tracks[index], flag) == on:
            return                              # HUI toggles: only press to change
        setattr(self._tracks[index], flag, on)  # optimistic; the LED confirms
        self._send(hui.press(index, port))

    def set_selected_track(self, index: int) -> None:
        if not 0 <= index < hui.NUM_STRIPS:
            return
        self._selected = index
        self._send(hui.press(index, hui.P_SELECT))

    _TRANSPORT_PORT = {
        TransportAction.PLAY: hui.T_PLAY,
        TransportAction.STOP: hui.T_STOP,
        TransportAction.RECORD: hui.T_REC,
        TransportAction.REWIND: hui.T_REWIND,
        TransportAction.FASTFORWARD: hui.T_FFWD,
    }

    def transport_action(self, action: TransportAction, pressed: bool) -> None:
        if not pressed:
            return
        port = self._TRANSPORT_PORT.get(action)
        if port is not None:
            self._send(hui.press(hui.ZONE_TRANSPORT, port))

    def page(self, delta: int) -> bool:
        """The ROTO paged: shift Pro Tools' 8-strip bank. PT re-floods the strips
        for the new window, so the model follows. (Source-side paging, like
        Cubase — the bridge doesn't window our list itself.)"""
        self._send(hui.press(hui.ZONE_BANK,
                             hui.BANK_RIGHT if delta > 0 else hui.BANK_LEFT))
        return True

    @staticmethod
    def _fire(cb, *args) -> None:
        if cb is not None:
            cb(*args)
