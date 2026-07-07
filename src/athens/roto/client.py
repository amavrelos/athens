"""RotoControl: the high-level facade the bridge talks to.

It combines the two channels behind one object:
  * config  (serial)  -> firmware/mode queries, config-update sessions, writing
                         knob/switch/plugin configs, and async device events
  * values  (MIDI)    -> push a value to a knob (motor), receive knob movement

Construct it with any `Transport` (real serial or loopback) and, optionally, a
`MidiIO`. Everything degrades gracefully when MIDI is absent.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Optional

from ..protocol import codec
from ..protocol.constants import CmdType, General, Midi, Mode, Plugin, RespCode
from .midi import MidiIO
from .transport import Transport

log = logging.getLogger(__name__)


class RotoControl:
    def __init__(self, transport: Transport, midi: Optional[MidiIO] = None):
        self._t = transport
        self._midi = midi
        self._t.on_async = self._dispatch_async
        if self._midi is not None:
            self._midi.on_value = self._on_midi_value

        # Async event callbacks (all optional). Signatures documented inline.
        self.on_mode_changed: Optional[Callable[[codec.ModeState], None]] = None
        self.on_setup_selected: Optional[Callable[[int], None]] = None
        self.on_control_learned: Optional[Callable[[int, int, int], None]] = None  # (setup, type, index)
        self.on_plugin_selected: Optional[Callable[[bytes], None]] = None
        self.on_plugin_control_learned: Optional[Callable[[bytes, int, int], None]] = None
        # Real-time knob movement from the device: (channel, controller/addr, 0.0-1.0)
        self.on_value: Optional[Callable[[int, int, float], None]] = None

    # ---- GENERAL ---------------------------------------------------------
    def firmware_version(self) -> codec.FirmwareVersion:
        r = self._req(codec.get_fw_version(),
                      (CmdType.GENERAL, General.GET_FW_VERSION))
        return codec.decode_fw_version(r.data)

    def mode(self) -> codec.ModeState:
        r = self._req(codec.get_mode(), (CmdType.GENERAL, General.GET_MODE))
        return codec.decode_mode(r.data)

    def set_mode(self, mode: Mode, page: int = 1) -> None:
        self._req(codec.set_mode(mode, page))

    @contextmanager
    def config_update(self):
        """Wrap config writes in a START/END CONFIG UPDATE session, as the spec
        requires for every SET/CLEAR that touches stored config."""
        self._req(codec.start_config_update())
        try:
            yield
        finally:
            self._req(codec.end_config_update())

    # ---- MIDI setups -----------------------------------------------------
    def current_setup(self) -> codec.SetupInfo:
        r = self._req(codec.get_current_setup(),
                      (CmdType.MIDI, Midi.GET_CURRENT_SETUP))
        return codec.decode_setup(r.data)

    def select_setup(self, setup_index: int) -> None:
        self._req(codec.set_setup(setup_index))

    def set_setup_name(self, setup_index: int, name: str) -> None:
        self._req(codec.set_setup_name(setup_index, name))

    def write_knob_config(self, cfg: codec.KnobConfig) -> None:
        self._req(codec.set_knob_control_config(cfg))

    def write_switch_config(self, cfg: codec.SwitchConfig) -> None:
        self._req(codec.set_switch_control_config(cfg))

    def clear_control(self, setup_index, control_type, control_index) -> None:
        self._req(codec.clear_control_config(setup_index, control_type, control_index))

    def clear_setup(self, setup_index: int) -> None:
        self._req(codec.clear_midi_setup(setup_index))

    # ---- real-time (MIDI) ------------------------------------------------
    def send_value(self, channel: int, controller: int, value: float,
                   nrpn: bool = False, bits: int = 7) -> None:
        """Push a value (0.0-1.0) to a knob; the motor follows."""
        if self._midi is None:
            log.debug("send_value ignored (no MIDI): ch%d cc%d = %.3f",
                      channel, controller, value)
            return
        if nrpn:
            self._midi.send_nrpn(channel, controller, value, bits)
        else:
            self._midi.send_cc(channel, controller, value, bits)

    def close(self) -> None:
        self._t.close()
        if self._midi is not None:
            self._midi.close()

    # ---- config read-back (for backup/library; flash-free) ----------------
    def setup_info(self, setup_index: int) -> codec.SetupInfo:
        r = self._req(codec.get_setup(setup_index), (CmdType.MIDI, Midi.GET_SETUP))
        return codec.decode_setup(r.data)

    def read_knob_config(self, setup_index: int, control_index: int):
        """KnobConfig for a slot, or None if the slot is unconfigured."""
        r = self._req(codec.build_frame(CmdType.MIDI, Midi.GET_KNOB_CONTROL_CONFIG,
                                        bytes((setup_index, control_index))),
                      (CmdType.MIDI, Midi.GET_KNOB_CONTROL_CONFIG),
                      ok_codes=(RespCode.SUCCESS, RespCode.UNCONFIGURED))
        return codec.decode_knob_config(r.data) if r.ok else None

    def read_switch_config(self, setup_index: int, control_index: int):
        r = self._req(codec.build_frame(CmdType.MIDI, Midi.GET_SWITCH_CONTROL_CONFIG,
                                        bytes((setup_index, control_index))),
                      (CmdType.MIDI, Midi.GET_SWITCH_CONTROL_CONFIG),
                      ok_codes=(RespCode.SUCCESS, RespCode.UNCONFIGURED))
        return codec.decode_switch_config(r.data) if r.ok else None

    def current_plugin(self):
        """Hash of the plugin map the device currently shows (spec 4.1),
        or None when the device isn't on a plugin."""
        r = self._req(codec.get_current_plugin(),
                      (CmdType.PLUGIN, Plugin.GET_CURRENT_PLUGIN),
                      ok_codes=(RespCode.SUCCESS, RespCode.NO_PLUGIN))
        return bytes(r.data[:8]) if r.ok and len(r.data) >= 8 else None

    def set_plugin_name(self, plugin_hash: bytes, name: str) -> None:
        """Rename a stored plugin map (spec 4.7 SET PLUGIN NAME). Wrap in
        config_update() like every stored-config write."""
        self._req(codec.set_plugin_name(plugin_hash, name))

    def iter_plugins(self):
        """Yield PluginInfo for every plugin stored on the device.
        NO_PLUGIN (0xFD) is the documented end-of-enumeration, not an error."""
        r = self._req(codec.get_first_plugin(),
                      (CmdType.PLUGIN, Plugin.GET_FIRST_PLUGIN),
                      ok_codes=(RespCode.SUCCESS, RespCode.NO_PLUGIN))
        while r.ok:
            yield codec.decode_plugin_info(r.data)
            r = self._req(codec.get_next_plugin(),
                          (CmdType.PLUGIN, Plugin.GET_NEXT_PLUGIN),
                          ok_codes=(RespCode.SUCCESS, RespCode.NO_PLUGIN))

    def read_plugin_knob_config(self, plugin_hash: bytes, control_index: int):
        r = self._req(codec.build_frame(CmdType.PLUGIN, Plugin.GET_PLUGIN_KNOB_CONFIG,
                                        plugin_hash + bytes((control_index,))),
                      (CmdType.PLUGIN, Plugin.GET_PLUGIN_KNOB_CONFIG),
                      ok_codes=(RespCode.SUCCESS, RespCode.UNCONFIGURED))
        return codec.decode_plugin_knob_config(r.data) if r.ok else None

    def read_plugin_switch_config(self, plugin_hash: bytes, control_index: int):
        r = self._req(codec.build_frame(CmdType.PLUGIN, Plugin.GET_PLUGIN_SWITCH_CONFIG,
                                        plugin_hash + bytes((control_index,))),
                      (CmdType.PLUGIN, Plugin.GET_PLUGIN_SWITCH_CONFIG),
                      ok_codes=(RespCode.SUCCESS, RespCode.UNCONFIGURED))
        return codec.decode_plugin_switch_config(r.data) if r.ok else None

    # ---- internals -------------------------------------------------------
    def _req(self, frame: bytes, key=None,
             ok_codes=(RespCode.SUCCESS,)) -> codec.Response:
        resp_len = codec.RESPONSE_DATA_LEN.get(key, 0) if key else 0
        r = self._t.request(frame, resp_len)
        if r.code not in ok_codes:
            raise RuntimeError(f"ROTO error 0x{r.code:02X} for {frame[:3].hex(' ')}")
        return r

    def _on_midi_value(self, channel: int, controller: int, value: float) -> None:
        if self.on_value:
            self.on_value(channel, controller, value)

    def _dispatch_async(self, ev: codec.AsyncEvent) -> None:
        t, s, d = ev.cmd_type, ev.subtype, ev.data
        if t == CmdType.GENERAL and s == General.SET_MODE and len(d) >= 2:
            if self.on_mode_changed:
                self.on_mode_changed(codec.decode_mode(d))
        elif t == CmdType.MIDI and s == Midi.SET_SETUP and len(d) >= 1:
            if self.on_setup_selected:
                self.on_setup_selected(d[0])
        elif t == CmdType.MIDI and s == Midi.MIDI_CONTROL_LEARNED and len(d) >= 3:
            if self.on_control_learned:
                self.on_control_learned(d[0], d[1], d[2])
        elif t == CmdType.PLUGIN and s == Plugin.SET_PLUGIN and len(d) >= 8:
            if self.on_plugin_selected:
                self.on_plugin_selected(d[:8])
        elif t == CmdType.PLUGIN and s == Plugin.PLUGIN_CONTROL_LEARNED and len(d) >= 10:
            if self.on_plugin_control_learned:
                self.on_plugin_control_learned(d[:8], d[8], d[9])
        else:
            log.debug("unhandled async event: %s", ev)
