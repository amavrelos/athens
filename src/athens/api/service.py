"""BridgeService: binds the Logic-dialect bridge + DAW source to the RPC/event
API.

Starts DISCONNECTED; `connect` opens the device's USB-MIDI port (live SysEx
channel) and/or its serial port (config/backup channel) and attaches them;
`disconnect` tears them down. The DAW source (REAPER feed/OSC) lives across
reconnects, so Live is live before or without any hardware.

Event topics published on the bus (what the UI subscribes to):
    device      {connected, serial}             connection lifecycle
    tracks      {tracks: [...], count}          track snapshots
    selected    {index}                         selection follows
    transport   {playing, recording, ...}
    devices     {devices: [...]}                FX list of selected track
    value       {cc, value}                     raw value-channel CC (high rate)
    touch       {knob, touched}                 knob touch events
    param       {device, param, value, display} mapped plugin-param changes
    frame       {dir, hex, kind}                decoded traffic (diagnostics)
    setups      {index}                         library changes
    progress    {stage, done, total}            long operations (dump/restore)
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Optional

from .. import backup
from ..daw.source import MockSysexSource, SysexDawSource
from ..library import SetupLibrary
from ..protocol.constants import Mode
from ..roto.client import RotoControl
from ..roto.sysex_client import MidiPort
from ..sysex import codec
from ..sysex.constants import General, Group, NUM_ENCODERS
from .rpc import EventBus, JsonRpcApi, RpcError

log = logging.getLogger(__name__)


def _chain(obj, attr: str, extra) -> None:
    """Append `extra` to a callback slot without displacing the existing one."""
    orig = getattr(obj, attr)

    def wrapper(*args):
        if orig is not None:
            orig(*args)
        extra(*args)
    setattr(obj, attr, wrapper)


class BridgeService:
    def __init__(self, port: Optional[MidiPort] = None,
                 source: Optional[SysexDawSource] = None,
                 library_path=None, auto_connect: bool = False,
                 daw: Optional[str] = None):
        # auto_connect: ONLY the app entrypoints pass True — it touches real
        # hardware, so bare construction (tests) must never do it
        self._auto = auto_connect
        self.bus = EventBus()
        self.rpc = JsonRpcApi()

        self.source = source or MockSysexSource()
        # runtime DAW hot-swap: with `--daw auto`, a background monitor swaps the
        # source to whichever DAW goes live — any start order, no relaunch.
        import threading
        self._auto_daw = (daw == "auto")
        self._daw_mode = daw            # which companion scripts to keep current
        self._active_daw = getattr(self.source, "DAW_NAME", "DAW").lower()
        self._daw_lock = threading.Lock()
        self.port: Optional[MidiPort] = None
        self.client: Optional[RotoLogicClient] = None
        self.bridge = None      # LogicBridge, created on connect
        self.roto: Optional[RotoControl] = None      # serial config channel
        self._connected = False
        self._connected_at = 0.0
        self._device_mode = ""  # last mode the hardware announced
        self._daw_alive = True  # False once the DAW's feed heartbeat stops
        self._started = False

        # library_path: the app entrypoints pass the persistent location
        # (the library IS the archive); bare construction stays in-memory so
        # tests never touch the user's real files
        self.library = SetupLibrary(library_path)
        # plugin-link registry: REAPER FX name -> the device plugin identity
        # (hash8) it should attach as — recovers maps learned in other DAWs
        self._links_path = None
        self._links: dict = {}
        # param-reference registry: what the DAW said a learned control
        # really is (full param name + value at learn time) — the device
        # only keeps a 12-char display name
        self._param_refs_path = None
        self._param_refs: dict = {}
        self._pending_learn_ref: Optional[dict] = None
        # user settings (assignable transport buttons, ...) — the bridge
        # applies them, so they live app-side, not in the web UI
        self._settings_path = None
        self._settings: dict = {}
        if library_path is not None:
            from pathlib import Path
            self._links_path = Path(library_path).parent / "plugin-links.json"
            self._param_refs_path = \
                Path(library_path).parent / "param-refs.json"
            self._settings_path = Path(library_path).parent / "settings.json"
            import json
            for attr, path in (("_links", self._links_path),
                               ("_param_refs", self._param_refs_path),
                               ("_settings", self._settings_path)):
                try:
                    setattr(self, attr, json.loads(path.read_text()))
                except (OSError, ValueError):
                    pass

        self._register_methods()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._started = True
        if self._auto:
            self._sync_daw_scripts()    # keep the DAW companions current
        # REAPER and the device are INDEPENDENT endpoints: the DAW feed runs
        # from app launch so Live is live before (or without) any hardware;
        # attaching the device later (connect) re-chains these taps
        self._tap_source_events()
        self.source.start()
        if self._auto:
            # the ROTO is possibly already plugged in — connect without
            # making the user click anything
            import threading
            threading.Thread(target=self._auto_connect, daemon=True,
                             name="auto-connect").start()
        if self._auto_daw:
            # follow whichever DAW is live, whatever the start order
            import threading
            threading.Thread(target=self._daw_monitor, daemon=True,
                             name="daw-monitor").start()
        self._check_system_permissions()
        log.info("BridgeService started (attached=%s)", self.bridge is not None)

    def _sync_daw_scripts(self) -> None:
        """Drop the current REAPER/Cubase companion scripts into each host's
        folder so the user never copies a file by hand. Best-effort — a sync
        hiccup must never block launch."""
        try:
            from athens.daw import script_install
            notes = script_install.sync(self._daw_mode)
        except Exception as exc:  # noqa: BLE001 - never block startup on this
            log.warning("DAW script sync skipped: %s", exc)
            return
        self._publish_reload_notice(notes)

    def _publish_reload_notice(self, notes) -> None:
        """A DAW script changed and needs a reload in the host — tell the log
        and the UI. No-op when nothing changed."""
        if not notes:
            return
        summary = "; ".join(notes)
        log.info("DAW scripts: %s — reload in the DAW to pick it up", summary)
        self.bus.publish("notice", {
            "text": summary + " — reload the script in your DAW."})

    def set_script_override(self, daw: str, path: Optional[str]) -> dict:
        """Point script installation at a user-chosen folder (the 'Locate'
        action), then immediately drop the current script there. Returns the
        new status + what changed; None path clears the override (back to
        auto-discovery)."""
        from athens.daw import script_install
        script_install.set_override(daw, path)
        notes = script_install.sync(daw)
        self._publish_reload_notice(notes)
        log.info("script folder for %s -> %s (%s)", daw, path or "(auto)",
                 "; ".join(notes) or "already current")
        return {"status": script_install.status(), "notes": notes}

    def reinstall_daw_scripts(self, daw: str) -> dict:
        """Force-write a DAW's companion script(s) into place even when the
        on-disk copy is byte-identical (the Settings 'Reinstall'/repair
        button). Returns new status + what was written (empty notes = no target
        folder was found -> the user needs to Locate it)."""
        from athens.daw import script_install
        notes = script_install.sync(daw, force=True)
        self._publish_reload_notice(notes)
        log.info("reinstalled %s scripts: %s", daw, "; ".join(notes) or "none")
        return {"status": script_install.status(), "notes": notes}

    @staticmethod
    def _serial_candidates() -> list:
        import re
        try:
            from serial.tools import list_ports as slp
        except ImportError:
            return []
        return [p.device for p in slp.comports()
                if re.search(r"roto|usbmodem",
                             p.device + " " + (p.description or ""), re.I)]

    def _attach_best_serial(self, candidates) -> Optional[str]:
        """The ROTO enumerates TWO CDC ports (one is debug) and their numbers
        change with the USB socket — probe each candidate with a firmware
        query and keep the one that answers."""
        for dev in candidates:
            roto = None
            try:
                from ..roto.transport import SerialTransport
                roto = RotoControl(SerialTransport(dev))
                fw = str(roto.firmware_version())
                self._attach_serial(roto)
                log.info("serial attached: %s (fw %s)", dev, fw)
                return fw
            except Exception as exc:  # noqa: BLE001 - try the next port
                log.info("serial probe %s failed: %s", dev, exc)
                if roto is not None:
                    try:
                        roto.close()
                    except Exception:  # noqa: BLE001
                        pass
        return None

    def _auto_connect(self) -> None:
        try:
            import mido
            # match the real device ("ROTO-CONTROL") but NOT the Cubase bridge
            # pair ("...roto-bridge") — else the device client binds the bridge
            # port and drowns in the DAW contract's frames.
            if not any("roto" in n.lower() and "bridge" not in n.lower()
                       for n in mido.get_input_names()):
                log.info("auto-connect: no ROTO MIDI port; waiting for manual")
                return
            from ..roto.sysex_client import MidoMidiPort
            self._attach_midi(MidoMidiPort())
            log.info("auto-connect: MIDI attached")
        except Exception as exc:  # noqa: BLE001 - stay silent, manual retry works
            log.warning("auto-connect MIDI failed: %s", exc)
            return
        if self._attach_best_serial(self._serial_candidates()) is None:
            log.warning("auto-connect: no serial candidate answered "
                        "(device maps unavailable until it does)")

    def stop(self) -> None:
        # idempotent: runs from the UI's finally, atexit, AND signal handlers,
        # so it must not double-close the serial/MIDI ports
        if not self._started and self.bridge is None:
            return
        self._started = False
        # Stop the DAW feed FIRST. Its port (the Cubase bridge / REAPER OSC +
        # feed files) must be closed here, not left to GC — GC at exit deadlocks
        # CoreMIDI on the still-live input callback. source.stop() closes it via
        # the callback-detach path and wipes the IPC files, so we exit clean.
        try:
            self.source.stop()
        except Exception:               # noqa: BLE001 - best-effort teardown
            pass
        if self.bridge is not None:
            self.bridge.stop()          # blanks the device (no ghost session)
            self.bridge = None
        # Close the MIDI port EXPLICITLY: MidoMidiPort.close() detaches the input
        # callback first, so CoreMIDI's close can't deadlock against the callback
        # thread on exit (Python 3.14 GIL trap).
        if self.port is not None:
            try:
                self.port.close()
            except Exception:           # noqa: BLE001 - best-effort teardown
                pass
            self.port = None
        self.client = None
        if self.roto is not None:
            self.roto.close()
            self.roto = None

    # -- device attachment ------------------------------------------------------
    def _attach_midi(self, port: MidiPort) -> None:
        """Attach the live SysEx channel: build the Logic client + bridge
        (daw_id=3 — sweep learn, end-stops, the works) around the port, then
        chain the service's event taps on top of the handlers the bridge just
        wired."""
        from ..bridge.logic_bridge import LogicBridge
        from ..roto.logic_client import RotoLogicClient
        self.port = port
        self.client = RotoLogicClient(port)
        # start from clean slots: the bridge's direct wiring only overwrites
        # the handlers IT sets — without this reset, slots it doesn't own
        # (on_frame, ...) would gain a duplicate service chain per re-attach
        self._reset_source_taps()
        self.bridge = LogicBridge(self.client, self.source)
        self.bridge.hash_resolver = self._resolve_plugin_hash
        # remember the DAW's full param identity for each learn — the serial
        # LEARNED event supplies the landing slot to pair it with
        self.bridge.on_learn_reference = self._stash_learn_ref
        # device page arrows move the on-surface window -> refresh the app
        self.bridge.on_bank_changed = \
            lambda: self.bus.publish("tracks", self._tracks_snapshot())
        self._apply_settings()
        self._tap_events()
        if self._started:
            self.bridge.start()

    def _resolve_plugin_hash(self, name: str):
        from ..links import hash_from_entry
        return hash_from_entry(self._links.get(name))

    def _save_links(self) -> None:
        if self._links_path is not None:
            import json
            self._links_path.write_text(json.dumps(self._links, indent=1))

    # default actions for the assignable transport slots (CC keys as
    # strings — JSON round-trip); 32 = the labeled 'punch' key, 33-35 are
    # the firmware grid's unlabeled buttons
    TRANSPORT_DEFAULTS = {"32": "metronome", "33": "rewind",
                          "34": "fastforward", "35": "metronome"}

    def _transport_settings(self) -> dict:
        return {**self.TRANSPORT_DEFAULTS,
                **self._settings.get("transport", {})}

    def _mix_touch_select(self) -> bool:
        return bool(self._settings.get("mix_touch_select", False))

    def _apply_settings(self) -> None:
        if self.client is not None and \
                hasattr(self.client, "set_transport_assignments"):
            self.client.set_transport_assignments(self._transport_settings())
        if self.bridge is not None and hasattr(self.bridge, "mix_touch_select"):
            # mix-mode: does touching a knob select its track (default no)
            self.bridge.mix_touch_select = self._mix_touch_select()

    def _save_settings(self) -> None:
        if self._settings_path is not None:
            import json
            self._settings_path.write_text(json.dumps(self._settings,
                                                      indent=1))

    def _system_control_active(self) -> bool:
        """System control is opt-in: the Settings toggle, or launching with
        --daw system (an explicit ask). DAW-only users stay probe-free."""
        return bool(self._settings.get("system_control")) or \
            getattr(self.source, "DAW_NAME", "") == "System"

    def _check_system_permissions(self) -> None:
        if not self._system_control_active():
            return
        from ..daw.system_source import system_permissions
        status = system_permissions()
        if status["accessibility"] is False:
            log.warning("system control enabled but Accessibility is NOT "
                        "granted — cursor knob and media keys will be inert "
                        "(System Settings > Privacy & Security > "
                        "Accessibility)")
        elif not status["pyobjc"]:
            log.warning("system control enabled but pyobjc is missing — it is "
                        "a base macOS dependency; reinstall with: "
                        'pip install -e "." (there is no [system] extra)')

    def _stash_learn_ref(self, ref: dict) -> None:
        self._pending_learn_ref = ref

    def _save_param_refs(self) -> None:
        if self._param_refs_path is not None:
            import json
            self._param_refs_path.write_text(
                json.dumps(self._param_refs, indent=1))

    def _on_device_map_learned(self, h: bytes, ct: int, ci: int) -> None:
        kind = "switch" if ct else "knob"
        ref = self._pending_learn_ref
        if ref is not None and ref.get("hash") in (None, h.hex()):
            self._param_refs.setdefault(h.hex(), {})[f"{kind}:{ci}"] = ref
            self._pending_learn_ref = None
            self._save_param_refs()
        self.bus.publish("device_map_changed",
                         {"hash": h.hex(), "kind": kind, "slot": ci})

    def _attach_serial(self, roto: RotoControl) -> None:
        self.roto = roto
        # the device announces learns over serial (spec 3.11 + 4.14) — feed
        # them to the UI so maps/badges refresh the moment a knob is learned
        roto.on_plugin_control_learned = self._on_device_map_learned
        roto.on_control_learned = \
            lambda setup, ct, ci: self.bus.publish("setup_learned", {
                "setup": setup, "kind": "switch" if ct else "knob",
                "slot": ci})
        # serial-side mode announcements (spec 3.3 async) — also sees flips
        # into MIDI mode, which the DAW-MIDI side can't
        roto.on_mode_changed = \
            lambda ms: self._set_device_mode(
                {Mode.MIDI: "midi", Mode.PLUGIN: "plugin",
                 Mode.MIX: "mix"}.get(ms.mode, ""))
        # setup/plugin selected on-device (spec 3.3/4.5 async) — the UI
        # follows what the hardware is showing
        roto.on_setup_selected = \
            lambda i: self.bus.publish("setup_selected", {"index": i})
        roto.on_plugin_selected = \
            lambda h: self.bus.publish("device_plugin_selected",
                                       {"hash": h.hex()})
        self._publish_device()

    def _detach(self) -> None:
        if self.bridge is not None:
            self.bridge.stop()          # stops the source too...
        self.port = self.client = self.bridge = None
        if self.roto is not None:
            self.roto.close()
            self.roto = None
        self._set_connected(False)
        if self._started:
            # ...but the REAPER endpoint outlives the device: re-tap + revive
            self._reset_source_taps()
            self._tap_source_events()
            self.source.start()

    def _reset_source_taps(self) -> None:
        """Drop the dead bridge's handlers so revived taps start clean. Must
        cover EVERY slot _tap_source_events chains (including on_daw_alive and
        on_frame) — a missed slot stacks another wrapper per detach/attach cycle
        (duplicate trace rows / double daw-alive publishes)."""
        for name in ("on_tracks_changed", "on_transport_changed",
                     "on_selected_track_changed", "on_devices_changed",
                     "on_device_param_value", "on_plugin_focus_changed",
                     "on_track_volume", "on_track_pan", "on_track_send",
                     "on_track_flag", "on_track_vu", "on_param_touched",
                     "on_daw_alive", "on_frame"):
            if hasattr(self.source, name):
                setattr(self.source, name, None)

    # -- event taps (chained AFTER the bridge wired its own handlers) --------
    def _tap_source_events(self) -> None:
        _chain(self.source, "on_tracks_changed",
               lambda: self.bus.publish("tracks", self._tracks_snapshot()))
        # Per-value mixer changes: REAPER refreshes the whole bank (fires
        # on_tracks_changed), but Cubase fires these GRANULAR events — tap them
        # too so live fader/mute/solo moves reach the UI.
        for _ev in ("on_track_volume", "on_track_pan", "on_track_flag"):
            _chain(self.source, _ev,
                   lambda *_a: self.bus.publish("tracks", self._tracks_snapshot()))
        _chain(self.source, "on_transport_changed",
               lambda: self.bus.publish("transport", asdict(self.source.transport())))
        _chain(self.source, "on_selected_track_changed",
               lambda: self.bus.publish("selected", {"index": self.source.selected_track()}))
        _chain(self.source, "on_devices_changed",
               lambda: self.bus.publish("devices", self._devices_snapshot()))
        _chain(self.source, "on_device_param_value",
               lambda d, p, v, s: self.bus.publish(
                   "param", {"device": d, "param": p, "value": v, "display": s}))
        _chain(self.source, "on_plugin_focus_changed",
               lambda: self.bus.publish("devices", self._devices_snapshot()))
        # DAW liveness (REAPER quit/returned): reflect it in the UI and push
        # the now-empty (or refreshed) snapshots so the app doesn't show a
        # ghost session either
        _chain(self.source, "on_daw_alive", self._publish_daw_alive)
        # diagnostics: decode every bridge frame into the translation trace
        _chain(self.source, "on_frame", self._trace_frame)

    def _trace_frame(self, direction: str, raw: bytes) -> None:
        """Decode a Cubase bridge frame into a trace row (the Cubase column)."""
        from ..daw import cubase_contract as wire
        label, comment = wire.describe(bytes(raw))
        self.bus.publish("trace", {
            "side": "cubase", "dir": direction, "label": label,
            "comment": comment, "hex": bytes(raw).hex(" ")})

    @staticmethod
    def _describe_roto(data: bytes) -> str:
        """A short label for a device (Logic-dialect) frame — the ROTO column."""
        if not data or data[0] != 0xF0:
            return "CC " + bytes(data).hex(" ")
        try:
            from ..sysex import codec
            m = codec.parse_sysex(bytes(data))
            return "SysEx grp=0x%02x cmd=0x%02x" % (m.group, m.command)
        except Exception:      # noqa: BLE001 - unknown frame, show raw
            return "SysEx " + bytes(data).hex(" ")

    def _publish_daw_alive(self, alive: bool) -> None:
        self._daw_alive = alive
        self.bus.publish("daw", {"alive": alive,
                                 "name": getattr(self.source, "DAW_NAME", "DAW")})
        self.bus.publish("tracks", self._tracks_snapshot())
        self.bus.publish("devices", self._devices_snapshot())

    # -- runtime DAW hot-swap (--daw auto) -----------------------------------
    def _daw_monitor(self) -> None:
        """Follow whichever DAW is live, whatever the start order: if the live
        DAW changes (Cubase opened after Athens, REAPER quit, ...) swap the
        source under the running service so the user never relaunches to
        reconnect."""
        import time
        from ..daw.detect import reaper_feed_live
        while self._started:
            time.sleep(3.0)
            if not self._started:
                break
            try:
                # RUNTIME detection opens NO MIDI port: opening one while the
                # device port floods spins forever in a process-wide CoreMIDI
                # lock while holding the GIL, freezing the whole app. So use only
                # free signals — a live Cubase confirmed on its OWN already-open
                # port, and REAPER's heartbeat file-stat. A Cubase that appears
                # mid-session is picked up on relaunch (startup probes safely,
                # before the device port opens).
                cur = self._active_daw
                if cur == "cubase" and self._cubase_alive():
                    continue                        # Cubase still live — stay put
                if reaper_feed_live() and cur != "reaper":
                    self._swap_daw("reaper")        # the only runtime-detectable switch
            except Exception:                       # noqa: BLE001 - keep looping
                log.debug("daw monitor tick failed", exc_info=True)

    def _cubase_alive(self) -> bool:
        """Confirm the running Cubase source on its already-open port — no new
        MIDI port (see CubaseSysexSource.check_alive)."""
        check = getattr(self.source, "check_alive", None)
        return bool(check and check())

    def _swap_daw(self, daw: str) -> None:
        from ..daw.detect import make_source
        with self._daw_lock:
            if daw == self._active_daw:
                return
            log.info("DAW hot-swap: %s -> %s", self._active_daw, daw)
            old = self.source
            self._reset_source_taps()               # silence the old handlers
            new = make_source(daw)
            self.source = new
            if self.bridge is not None:
                # rebind the device push path FIRST (direct assignments own the
                # slots): a plain `bridge.source = new` fixes reads but leaves
                # the device deaf to the new DAW.
                self.bridge.bind_source(new)
            self._tap_source_events()               # then chain the UI taps on top
            new.start()
            self._active_daw = daw
        try:
            old.stop()
        except Exception:                           # noqa: BLE001 - best-effort
            pass
        # refresh the UI: new DAW identity + fresh snapshots
        self._daw_alive = True
        self.bus.publish("daw", {"alive": True, "name": new.DAW_NAME})
        self.bus.publish("tracks", self._tracks_snapshot())
        self.bus.publish("devices", self._devices_snapshot())

    def _tap_events(self) -> None:
        _chain(self.client, "on_connected", lambda: self._set_connected(True))
        _chain(self.client, "on_value",
               lambda cc, v: self.bus.publish("value", {"cc": cc, "value": v}))
        _chain(self.client, "on_touch",
               lambda knob, t: self.bus.publish("touch", {"knob": knob,
                                                          "touched": t}))
        # device -> DAW plugin knob values, for the Diagnostics param panel:
        # comparing device-in against the DAW's value/display exposes range /
        # stepped-param issues where a control "doesn't work" on some plugins.
        _chain(self.client, "on_param_value_cc",
               lambda p, v: self.bus.publish("devparam", {"param": p, "value": v}))
        self._tap_source_events()
        # device mode announcements -> the UI can follow the hardware
        # (remembered so get_state can report it to late-joining UI clients)
        if hasattr(self.client, "on_plugin_mode_logic"):
            _chain(self.client, "on_plugin_mode_logic",
                   lambda smart: self._set_device_mode(
                       "smart" if smart else "plugin"))
            _chain(self.client, "on_mixer_all_mode",
                   lambda k, b, s: self._set_device_mode("mix"))
            _chain(self.client, "on_mixer_focus_mode",
                   lambda: self._set_device_mode("mix"))
            _chain(self.client, "on_transport_request",
                   self._transport_mode_hint)
        # Logic-dialect learn/sweep activity (present once a device is attached)
        if hasattr(self.client, "on_learn_mode"):
            _chain(self.client, "on_learn_mode",
                   lambda m: self.bus.publish("learn", {"mode": m}))
            _chain(self.client, "on_sweep_value",
                   lambda sv: self.bus.publish("sweep", {
                       "param": sv.param_index, "step": sv.step,
                       "value": sv.value}))
            _chain(self.client, "on_learn_complete",
                   lambda: self.bus.publish("learn", {"mode": -1}))
        # diagnostics: decoded outbound traffic
        orig_send = self.port.send

        def tapped_send(data: bytes) -> None:
            orig_send(data)
            self.bus.publish("frame", self._describe_frame("out", data))
            self.bus.publish("trace", {"side": "roto", "dir": "tx",
                             "label": self._describe_roto(data), "comment": "",
                             "hex": bytes(data).hex(" ")})
        self.port.send = tapped_send  # type: ignore[method-assign]

    def _set_connected(self, state: bool) -> None:
        self._connected = state
        if state:
            self._connected_at = time.monotonic()
        self._publish_device()

    def _set_device_mode(self, mode: str) -> None:
        if not mode:
            return
        self._device_mode = mode
        self.bus.publish("mode", {"mode": mode})

    def _transport_mode_hint(self) -> None:
        # the device asks for transport LED state both in the connect
        # handshake (whatever screen it shows — the buttons are global) and
        # when the user opens the transport screen (logic_full_modes_capture
        # line 1651); only the second is a mode change, so ignore requests
        # arriving in the handshake window
        if self._connected and time.monotonic() - self._connected_at > 3.0:
            self._set_device_mode("transport")

    def _publish_device(self) -> None:
        self.bus.publish("device", {"connected": self._connected,
                                    "serial": self.roto is not None})

    @staticmethod
    def _describe_frame(direction: str, data: bytes) -> dict:
        kind = "sysex" if data and data[0] == 0xF0 else "cc"
        return {"dir": direction, "kind": kind, "hex": data.hex(" ")}

    def _progress(self, stage: str, done: int, total: int) -> None:
        self.bus.publish("progress", {"stage": stage, "done": done,
                                      "total": total})

    # -- snapshots -------------------------------------------------------------
    def _tracks_snapshot(self) -> dict:
        tracks = self.source.tracks()
        feed = getattr(self.source, "feed_running", None)
        return {"count": len(tracks),
                "feed_running": feed() if callable(feed) else None,
                # bank window the device paged to (arrows) so the app follows
                "first_track": getattr(self.bridge, "first_track", 0),
                "bank_size": NUM_ENCODERS,
                "tracks": [asdict(t) for t in tracks]}

    def _devices_snapshot(self) -> dict:
        return {"selected": self.source.selected_device(),
                "devices": [asdict(d) for d in self.source.devices()]}

    # -- RPC methods -----------------------------------------------------------
    def _register_methods(self) -> None:
        rpc = self.rpc

        @rpc.method("ping")
        def ping() -> str:
            return "pong"

        @rpc.method("get_state")
        def get_state() -> dict:
            return {
                "daw": getattr(self.source, "DAW_NAME", "DAW"),
                "connected": self._connected,
                "serial": self.roto is not None,
                "attached": self.bridge is not None,
                "mode": self._device_mode,
                "daw_alive": self._daw_alive,
                "selected_track": self.source.selected_track(),
                "transport": asdict(self.source.transport()),
                **self._tracks_snapshot(),
                **self._devices_snapshot(),
            }

        # -- connection ---------------------------------------------------------
        @rpc.method("list_ports")
        def list_ports() -> dict:
            out = {"serial": [], "midi_in": [], "midi_out": []}
            try:
                from serial.tools import list_ports as slp
                out["serial"] = [{"device": p.device, "description": p.description}
                                 for p in slp.comports()]
            except ImportError:
                pass
            try:
                import mido
                out["midi_in"] = mido.get_input_names()
                out["midi_out"] = mido.get_output_names()
            except Exception:  # noqa: BLE001 - backend may be missing entirely
                pass
            return out

        @rpc.method("connect")
        def connect(serial_port: Optional[str] = None,
                    midi_in: Optional[str] = None,
                    midi_out: Optional[str] = None) -> dict:
            result = {"midi": False, "serial": False}
            if self.bridge is None:
                from ..roto.sysex_client import MidoMidiPort
                try:
                    self._attach_midi(MidoMidiPort(midi_in, midi_out))
                    result["midi"] = True
                except Exception as exc:  # noqa: BLE001 - surface to the UI
                    raise RpcError(f"USB-MIDI: {exc}") from exc
            if self.roto is None:
                # try the given port first, then every other candidate (the
                # ROTO has a decoy debug port and renumbers across sockets)
                cands = ([serial_port] if serial_port else []) \
                    + [c for c in self._serial_candidates() if c != serial_port]
                fw = self._attach_best_serial(cands)
                if fw is not None:
                    result["fw"] = fw
                    result["serial"] = True
            return result

        @rpc.method("disconnect")
        def disconnect() -> dict:
            self._detach()
            return {"connected": False}

        @rpc.method("select_track")
        def select_track(index: int) -> dict:
            tracks = self.source.tracks()
            if not 0 <= index < len(tracks):
                raise RpcError(f"track index out of range: {index}")
            self.source.set_selected_track(index)
            if self.bridge is not None:
                self.bridge._push_focus_track()   # reflect on the device
            return {"index": index}

        @rpc.method("set_device_param")
        def set_device_param(device: int, param: int, value: float) -> dict:
            self.source.set_device_param(device, param, float(value))
            # app-initiated changes must reach the DEVICE too: the source's
            # echo gate (rightly) swallows the feed report of our own write,
            # so push the value to the hardware knob directly
            client = getattr(self.bridge, "client", None)
            if client is not None and hasattr(client, "send_param_value_cc") \
                    and 0 <= int(param) < 256 \
                    and int(device) == self.source.selected_device():
                client.send_param_value_cc(int(param), float(value))
            return {"device": device, "param": param, "value": value}

        @rpc.method("get_devices")
        def get_devices() -> dict:
            return self._devices_snapshot()

        @rpc.method("device_params")
        def device_params(device: int) -> list:
            return [asdict(p) for p in self.source.device_params(int(device))]

        # -- device plugin-map store + the LINK registry ----------------------
        @rpc.method("list_device_plugins")
        def list_device_plugins() -> list:
            """Fast inventory of the device's stored plugin maps (names +
            hashes only; per-control detail via get_device_plugin)."""
            if self.roto is None:
                raise RpcError("serial not connected")
            return [{"hash": p.hash.hex(), "name": p.name,
                     "type": p.plugin_type}
                    for p in self.roto.iter_plugins()]

        @rpc.method("get_device_plugin")
        def get_device_plugin(hash: str) -> dict:
            if self.roto is None:
                raise RpcError("serial not connected")
            h = bytes.fromhex(hash)
            # the DAW's full param identity per control: learn-time registry
            # first, else resolved live via the link + the current chain
            # (works for maps learned before this app existed)
            refs = dict(self._param_refs.get(hash, {}))
            live = {}
            linked = next((k for k, v in self._links.items()
                           if v.get("hash") == hash), None)
            if linked is not None:
                fx = next((d for d in self.source.devices()
                           if d.name == linked), None)
                if fx is not None:
                    for p in self.source.device_params(fx.index):
                        live[p.index] = {"param_name": p.name,
                                         "display": p.display,
                                         "fx_name": linked}

            def _ref(kind: str, slot: int, param_index: int):
                return refs.get(f"{kind}:{slot}") or live.get(param_index)

            knobs, switches = {}, {}
            for i in range(0x40):
                cfg = self.roto.read_plugin_knob_config(h, i)
                if cfg is not None:
                    knobs[str(i)] = {
                        "name": cfg.name, "param_index": cfg.mapped_param_index,
                        "colour": cfg.colour, "min": cfg.min_value,
                        "max": cfg.max_value, "steps": cfg.steps,
                        "step_names": list(cfg.step_names),
                        "haptic": int(cfg.haptic),
                        "indent1": cfg.indent1, "indent2": cfg.indent2,
                        "ref": _ref("knob", i, cfg.mapped_param_index)}
                sw = self.roto.read_plugin_switch_config(h, i)
                if sw is not None:
                    switches[str(i)] = {
                        "name": sw["name"],
                        "param_index": sw["mapped_param_index"],
                        "colour": sw["colour"], "min": sw["min_value"],
                        "max": sw["max_value"], "steps": sw["steps"],
                        "step_names": list(sw.get("step_names", [])),
                        "led_on": sw["led_on"], "led_off": sw["led_off"],
                        "ref": _ref("switch", i, sw["mapped_param_index"])}
                if i % 16 == 15:
                    self._progress("reading map", i + 1, 0x40)
            return {"hash": hash, "knobs": knobs, "switches": switches}

        @rpc.method("set_device_plugin_control")
        def set_device_plugin_control(hash: str, kind: str, slot: int,
                                      fields: dict) -> dict:
            """Edit one learned control on the device (display name, colour,
            steps + step names, min/max). Read-merge-write; the write is an
            explicit user action, satisfying the zero-flash policy."""
            if self.roto is None:
                raise RpcError("serial not connected")
            from ..protocol import codec as pcodec
            from ..protocol.constants import UNUSED_INDENT, KnobHaptic

            # bounds the flash store can actually hold — clamp here so no
            # client can write a corrupt config (values are 14-bit, colours
            # one 7-bit byte, detents 0 or 2-10)
            def _b14(v: object) -> int:
                return max(0, min(16383, int(v)))

            def _col(v: object) -> int:
                return max(0, min(127, int(v)))

            def _nsteps(v: object) -> int:
                n = max(0, min(10, int(v)))
                return 2 if n == 1 else n

            def _indent(v: object) -> int:
                if v in (None, ""):
                    return UNUSED_INDENT
                return max(0, min(127, int(v)))

            h = bytes.fromhex(hash)
            slot = int(slot)
            if kind == "knob":
                cfg = self.roto.read_plugin_knob_config(h, slot)
                if cfg is None:
                    raise RpcError(f"no knob learned at slot {slot}")
                cfg.name = str(fields.get("name", cfg.name))[:12]
                cfg.colour = _col(fields.get("colour", cfg.colour))
                cfg.min_value = _b14(fields.get("min", cfg.min_value))
                cfg.max_value = _b14(fields.get("max", cfg.max_value))
                steps = _nsteps(fields.get("steps", cfg.steps))
                names = [str(n)[:12] for n in
                         fields.get("step_names", cfg.step_names)][:steps]
                cfg.steps = steps
                cfg.step_names = names
                if steps >= 2:             # steps imply the haptic mode
                    cfg.haptic = KnobHaptic.KNOB_N_STEP
                    cfg.indent1 = cfg.indent2 = UNUSED_INDENT
                else:
                    if "haptic" in fields or "steps" in fields:
                        cfg.haptic = KnobHaptic.KNOB_300_CENTRE_INDENT \
                            if fields.get("haptic") == "centre" \
                            else KnobHaptic.KNOB_300
                    if "indent1" in fields:
                        cfg.indent1 = _indent(fields["indent1"])
                    if "indent2" in fields:
                        cfg.indent2 = _indent(fields["indent2"])
                with self.roto.config_update():
                    self.roto._req(pcodec.set_plugin_knob_config(cfg))
            elif kind == "switch":
                sw = self.roto.read_plugin_switch_config(h, slot)
                if sw is None:
                    raise RpcError(f"no switch learned at slot {slot}")
                steps = _nsteps(fields.get("steps", sw["steps"]))
                cfg = pcodec.PluginSwitchConfig(
                    plugin_hash=h, control_index=slot,
                    mapped_param_index=sw["mapped_param_index"],
                    mapped_param_hash=sw["mapped_param_hash"],
                    min_value=_b14(fields.get("min", sw["min_value"])),
                    max_value=_b14(fields.get("max", sw["max_value"])),
                    name=str(fields.get("name", sw["name"]))[:12],
                    colour=_col(fields.get("colour", sw["colour"])),
                    led_on=_col(fields.get("led_on", sw["led_on"])),
                    led_off=_col(fields.get("led_off", sw["led_off"])),
                    haptic=sw.get("haptic", 1),
                    steps=steps,
                    step_names=[str(n)[:12] for n in
                                fields.get("step_names",
                                           sw.get("step_names", []))][:steps])
                with self.roto.config_update():
                    self.roto._req(pcodec.set_plugin_switch_config(cfg))
            else:
                raise RpcError(f"unknown control kind: {kind}")
            return {"written": True, "kind": kind, "slot": slot}

        @rpc.method("set_device_mode")
        def set_device_mode(mode: str) -> dict:
            """'Who drives who', app side: flip the hardware to the screen
            matching the app view (serial SET MODE, spec 3.3)."""
            if self.roto is None:
                raise RpcError("serial not connected")
            m = {"midi": Mode.MIDI, "plugin": Mode.PLUGIN,
                 "mix": Mode.MIX}.get(str(mode).lower())
            if m is None:
                raise RpcError(f"unknown mode: {mode}")
            self.roto.set_mode(m)
            return {"mode": str(mode).lower()}

        @rpc.method("rename_device_plugin")
        def rename_device_plugin(hash: str, name: str) -> dict:
            """Rename a plugin map in device flash (spec 4.7); link labels
            follow so the registry keeps making sense."""
            if self.roto is None:
                raise RpcError("serial not connected")
            name = str(name)[:12]
            if not name:
                raise RpcError("name must not be empty")
            with self.roto.config_update():
                self.roto.set_plugin_name(bytes.fromhex(hash), name)
            changed = False
            for link in self._links.values():
                if link.get("hash") == hash:
                    link["device_name"] = name
                    changed = True
            if changed:
                self._save_links()
            return {"hash": hash, "name": name}

        @rpc.method("get_current_device_plugin")
        def get_current_device_plugin() -> dict:
            if self.roto is None:
                raise RpcError("serial not connected")
            h = self.roto.current_plugin()
            return {"hash": h.hex() if h else None}

        @rpc.method("activate_setup")
        def activate_setup(index: int) -> dict:
            """Switch the device's active MIDI setup (spec 3.3 SET SETUP)."""
            if self.roto is None:
                raise RpcError("serial not connected")
            self.roto.select_setup(int(index))
            return {"index": int(index)}

        @rpc.method("get_settings")
        def get_settings() -> dict:
            out = {"transport": self._transport_settings(),
                   "daw": getattr(self.source, "DAW_NAME", "DAW"),
                   "mix": {"touch_select": self._mix_touch_select()},
                   "system": {"enabled": self._system_control_active()}}
            if self._system_control_active():
                # permissions are probed ONLY behind the opt-in
                from ..daw.system_source import system_permissions
                out["system"].update(system_permissions())
            return out

        @rpc.method("set_settings")
        def set_settings(patch: dict) -> dict:
            """Shallow-merge a settings patch, persist, apply live."""
            for key, value in dict(patch).items():
                self._settings[key] = value
            self._save_settings()
            self._apply_settings()
            self._check_system_permissions()
            return {"transport": self._transport_settings()}

        @rpc.method("get_script_paths")
        def get_script_paths() -> dict:
            """Effective DAW companion-script folders + found/located status."""
            from ..daw import script_install
            return script_install.status()

        @rpc.method("set_script_path")
        def set_script_path(daw: str, path: Optional[str] = None) -> dict:
            """Record (or clear) a user-Located folder for a DAW, then re-sync
            the script into it. Returns new status + what changed."""
            return self.set_script_override(daw, path)

        @rpc.method("reinstall_scripts")
        def reinstall_scripts(daw: str) -> dict:
            """Force-copy a DAW's companion script(s) into place, overwriting an
            identical file (the Settings repair button). Returns status +
            notes; the DAW still has to reload the script itself."""
            return self.reinstall_daw_scripts(daw)

        @rpc.method("request_system_permission")
        def request_system_permission() -> dict:
            """User-initiated (Settings button): register the app and open
            the macOS Accessibility settings pane. Runs on the WS worker
            thread (no run loop), so it opens the pane rather than relying on
            the one-shot consent dialog."""
            if not self._system_control_active():
                raise RpcError("enable system control first")
            from ..daw.system_source import request_accessibility
            return request_accessibility()

        @rpc.method("relaunch_app")
        def relaunch_app() -> dict:
            """Quit and reopen the packaged app. macOS caches the
            event-posting permission per process, so a fresh Accessibility
            grant only takes effect after a real relaunch — an in-session
            re-check can never see it."""
            import os
            import subprocess
            import sys
            import threading
            from pathlib import Path
            if not getattr(sys, "frozen", False):
                raise RpcError("relaunch is only for the packaged Athens.app "
                               "— quit and reopen it yourself")
            exe = Path(sys.executable)
            app = next((p for p in exe.parents if p.suffix == ".app"), None)
            if app is None:
                raise RpcError("could not locate the .app bundle")
            pid = os.getpid()
            # a detached waiter reopens the app once THIS process is gone
            # (single-instance, so it must fully exit before `open` relaunches)
            subprocess.Popen(
                ["/bin/sh", "-c",
                 f'while kill -0 {pid} 2>/dev/null; do sleep 0.3; done; '
                 f'sleep 0.4; open "{app}"'],
                start_new_session=True)

            def _bye() -> None:
                try:
                    self.stop()          # blank the device before we vanish
                finally:
                    os._exit(0)
            threading.Timer(0.4, _bye).start()
            return {"relaunching": True}

        @rpc.method("clear_device_setup")
        def clear_device_setup(index: int) -> dict:
            """Wipe a setup slot in device flash (spec 3.10 CLEAR MIDI
            SETUP). The library copy is untouched — redeploy restores it."""
            if self.roto is None:
                raise RpcError("serial not connected")
            with self.roto.config_update():
                self.roto.clear_setup(int(index))
            return {"cleared": int(index)}

        @rpc.method("get_plugin_links")
        def get_plugin_links() -> dict:
            return dict(self._links)

        @rpc.method("link_plugin")
        def link_plugin(reaper_name: str, hash: str, device_name: str) -> dict:
            self._links[reaper_name] = {"hash": hash,
                                        "device_name": device_name}
            self._save_links()
            if self.bridge is not None \
                    and hasattr(self.bridge, "refresh_plugin_context"):
                self.bridge.refresh_plugin_context()
            return {"linked": reaper_name, "to": device_name}

        @rpc.method("unlink_plugin")
        def unlink_plugin(reaper_name: str) -> dict:
            self._links.pop(reaper_name, None)
            self._save_links()
            if self.bridge is not None \
                    and hasattr(self.bridge, "refresh_plugin_context"):
                self.bridge.refresh_plugin_context()
            return {"unlinked": reaper_name}

        @rpc.method("move_device_plugin_control")
        def move_device_plugin_control(hash: str, kind: str, from_slot: int,
                                       to_slot: int) -> dict:
            """Move a learned control to another slot (drag & drop). If the
            target slot is occupied the two controls SWAP; otherwise the
            source slot is cleared."""
            if self.roto is None:
                raise RpcError("serial not connected")
            from ..protocol import codec as pcodec
            h = bytes.fromhex(hash)
            src_i, dst_i = int(from_slot), int(to_slot)
            if src_i == dst_i:
                return {"moved": False}
            if kind == "knob":
                src = self.roto.read_plugin_knob_config(h, src_i)
                if src is None:
                    raise RpcError(f"no knob at slot {src_i}")
                dst = self.roto.read_plugin_knob_config(h, dst_i)
                with self.roto.config_update():
                    src.control_index = dst_i
                    self.roto._req(pcodec.set_plugin_knob_config(src))
                    if dst is not None:
                        dst.control_index = src_i
                        self.roto._req(pcodec.set_plugin_knob_config(dst))
                    else:
                        self.roto._req(pcodec.clear_plugin_control(
                            h, False, src_i))
            elif kind == "switch":
                src = self.roto.read_plugin_switch_config(h, src_i)
                if src is None:
                    raise RpcError(f"no switch at slot {src_i}")
                dst = self.roto.read_plugin_switch_config(h, dst_i)

                def _sw_cfg(d, idx):
                    return pcodec.PluginSwitchConfig(
                        plugin_hash=h, control_index=idx,
                        mapped_param_index=d["mapped_param_index"],
                        mapped_param_hash=d["mapped_param_hash"],
                        min_value=d["min_value"], max_value=d["max_value"],
                        name=d["name"], colour=d["colour"],
                        led_on=d["led_on"], led_off=d["led_off"],
                        haptic=d.get("haptic", 1), steps=d["steps"],
                        step_names=list(d.get("step_names", [])))
                with self.roto.config_update():
                    self.roto._req(pcodec.set_plugin_switch_config(
                        _sw_cfg(src, dst_i)))
                    if dst is not None:
                        self.roto._req(pcodec.set_plugin_switch_config(
                            _sw_cfg(dst, src_i)))
                    else:
                        self.roto._req(pcodec.clear_plugin_control(
                            h, True, src_i))
            else:
                raise RpcError(f"unknown control kind: {kind}")
            return {"moved": True, "swap": dst is not None}

        @rpc.method("delete_device_plugin")
        def delete_device_plugin(hash: str) -> dict:
            """Remove a stored plugin map from device flash (explicit,
            destructive, user-confirmed in the UI)."""
            if self.roto is None:
                raise RpcError("serial not connected")
            from ..protocol import codec as pcodec
            with self.roto.config_update():
                self.roto._req(pcodec.clear_plugin(bytes.fromhex(hash)))
            # drop any links pointing at the deleted map
            stale = [k for k, v in self._links.items() if v.get("hash") == hash]
            for k in stale:
                self._links.pop(k, None)
            if stale:
                self._save_links()
            return {"deleted": hash, "links_removed": stale}

        # -- setup library (local source of truth; deploy = explicit) --------
        def _setups_changed(index: int) -> None:
            self.bus.publish("setups", {"index": index})

        @rpc.method("list_setups")
        def list_setups() -> list:
            return self.library.list()

        @rpc.method("get_setup")
        def get_setup(index: int) -> dict:
            return self.library.get(int(index))

        @rpc.method("set_setup_name")
        def set_setup_name(index: int, name: str) -> dict:
            self.library.set_name(int(index), name)
            _setups_changed(int(index))
            return self.library.get(int(index))

        @rpc.method("update_control")
        def update_control(index: int, kind: str, slot: int,
                           fields: Optional[dict] = None) -> dict:
            result = self.library.update_control(int(index), kind, int(slot),
                                                 fields)
            _setups_changed(int(index))
            return result

        @rpc.method("deploy_setup")
        def deploy_setup(index: int) -> dict:
            """Explicit library -> device write. With a serial link attached
            this performs the real diff-based restore; without one it only
            flips the bookkeeping."""
            index = int(index)
            stats = None
            if self.roto is not None:
                stats = backup.restore_setup(self.roto, index,
                                             self.library.get(index))
            result = self.library.deploy(index)
            if stats is not None:
                result.update(stats)
            _setups_changed(index)
            return result

        @rpc.method("dump_device")
        def dump_device(indices: Optional[list] = None) -> dict:
            """Read every (given) setup off the device into the library —
            the snapshot/backup action. Reads only; zero flash writes."""
            if self.roto is None:
                raise RpcError("no serial link — connect the device first")
            rng = [int(i) for i in indices] if indices else range(backup.NUM_SETUPS)
            dumped = backup.dump_setups(self.roto, rng, progress=self._progress)
            n = backup.snapshot_into_library(dumped, self.library)
            for index in dumped:
                _setups_changed(int(index))
            return {"setups": n}

        @rpc.method("dump_plugins")
        def dump_plugins() -> list:
            if self.roto is None:
                raise RpcError("no serial link — connect the device first")
            return backup.dump_plugins(self.roto, progress=self._progress)

        @rpc.method("export_setup")
        def export_setup(index: int) -> dict:
            return self.library.export_setup(int(index))

        @rpc.method("import_setup")
        def import_setup(data: dict, index: Optional[int] = None) -> dict:
            target = self.library.import_setup(data, index)
            _setups_changed(target)
            return {"index": target}
