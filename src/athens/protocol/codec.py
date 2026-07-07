"""Serializer / codec for the ROTO-CONTROL Serial API v1.2.

Pure functions and dataclasses: build command frames (host -> ROTO) and parse
frames coming back (responses and asynchronous notifications). No I/O lives
here, so the whole protocol is unit-testable without hardware.

Framing (spec section 1):
    command  TO   roto:  5A <TYPE> <SUBTYPE> <LEN:2 BE> <DATA...>
    response FROM roto:  A5 <RC> <DATA...>          (fixed layout per command)
    async    FROM roto:  5A <TYPE> <SUBTYPE> <LEN:2 BE> <DATA...>   (no reply)

The command LEN is always recomputed from the payload here rather than trusting
the spec's stated constants (a couple of them are stale, e.g. GET KNOB CONTROL
CONFIG lists 0001 for a 2-byte SI/CI payload).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .constants import (
    CMD_START, GET_STEP_NAMES, NAME_LEN, PAGE_STRIDE, RESP_START, UNUSED_INDENT,
    CmdType, General, Midi, Plugin,
    Mode, ControlMode, KnobHaptic, SwitchHaptic, ControlType, RespCode,
)


class ProtocolError(Exception):
    """Bytes on the wire did not match the expected framing."""


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

def _u16_be(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"u16 out of range: {value}")
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


def encode_name(text: str, length: int = NAME_LEN) -> bytes:
    """ASCII, NUL-terminated, zero-padded to `length` bytes. Truncated so there
    is always at least one terminating NUL (spec: '0D-byte NULL terminated
    ASCII string, padded with 00s')."""
    raw = text.encode("ascii", errors="replace")[: length - 1]
    return raw + b"\x00" * (length - len(raw))


def decode_name(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _step_names_blob(steps: int, names: List[str]) -> bytes:
    """SN field: exactly `steps` NUL-terminated 13-byte strings."""
    return b"".join(
        encode_name(names[i] if i < len(names) else "") for i in range(steps)
    )


def build_frame(cmd_type: int, subtype: int, data: bytes = b"") -> bytes:
    """Wrap a payload in command framing with a computed big-endian length."""
    return bytes((CMD_START, cmd_type, subtype)) + _u16_be(len(data)) + data


# --------------------------------------------------------------------------
# GENERAL commands (0x01)
# --------------------------------------------------------------------------

def get_fw_version() -> bytes:
    return build_frame(CmdType.GENERAL, General.GET_FW_VERSION)


def get_mode() -> bytes:
    return build_frame(CmdType.GENERAL, General.GET_MODE)


def set_mode(mode: Mode, page: int = 0) -> bytes:
    """`page` is the human page number (1-based); encoded as multiples of 8."""
    return build_frame(CmdType.GENERAL, General.SET_MODE,
                       bytes((int(mode), max(0, page - 1) * PAGE_STRIDE)))


def start_config_update() -> bytes:
    return build_frame(CmdType.GENERAL, General.START_CONFIG_UPDATE)


def end_config_update() -> bytes:
    return build_frame(CmdType.GENERAL, General.END_CONFIG_UPDATE)


def factory_reset() -> bytes:
    """DANGER: reformats the device file system; erases all setups & plugins."""
    return build_frame(CmdType.GENERAL, General.FACTORY_RESET)


# --------------------------------------------------------------------------
# MIDI commands (0x02)
# --------------------------------------------------------------------------

def get_current_setup() -> bytes:
    return build_frame(CmdType.MIDI, Midi.GET_CURRENT_SETUP)


def get_setup(setup_index: int) -> bytes:
    return build_frame(CmdType.MIDI, Midi.GET_SETUP, bytes((setup_index,)))


def set_setup(setup_index: int) -> bytes:
    return build_frame(CmdType.MIDI, Midi.SET_SETUP, bytes((setup_index,)))


def set_setup_name(setup_index: int, name: str) -> bytes:
    return build_frame(CmdType.MIDI, Midi.SET_SETUP_NAME,
                       bytes((setup_index,)) + encode_name(name))


def clear_control_config(setup_index: int, control_type: ControlType,
                         control_index: int) -> bytes:
    return build_frame(CmdType.MIDI, Midi.CLEAR_CONTROL_CONFIG,
                       bytes((setup_index, int(control_type), control_index)))


def clear_midi_setup(setup_index: int) -> bytes:
    return build_frame(CmdType.MIDI, Midi.CLEAR_MIDI_SETUP, bytes((setup_index,)))


@dataclass
class KnobConfig:
    """A MIDI-setup knob control (spec 3.7 SET KNOB CONTROL CONFIG).

    Wire payload (base 0x1D bytes + steps*0x0D for SN):
        SI CI CM CC CP NA:2 MN:2 MX:2 CN:13 CS HM IP1 IP2 HS SN[HS*13]
    """
    setup_index: int
    control_index: int                       # 0x00 - 0x1F
    control_mode: ControlMode = ControlMode.CC_7BIT
    channel: int = 1                         # 1 - 16
    param: int = 0                           # CC number / control param
    nrpn_address: int = 0                    # 2 bytes, only for NRPN modes
    min_value: int = 0                       # 2 bytes BE (MSB = 0 for 7-bit)
    max_value: int = 0x7F
    name: str = ""
    colour: int = 0                          # 0x00 - 0x52
    haptic: KnobHaptic = KnobHaptic.KNOB_300
    indent1: int = UNUSED_INDENT             # KNOB_300 only, else 0xFF
    indent2: int = UNUSED_INDENT
    steps: int = 0                           # 2 - 10 for KNOB_N_STEP, else 0
    step_names: List[str] = field(default_factory=list)

    def payload(self) -> bytes:
        return (
            bytes((self.setup_index, self.control_index, int(self.control_mode),
                   self.channel, self.param))
            + _u16_be(self.nrpn_address)
            + _u16_be(self.min_value)
            + _u16_be(self.max_value)
            + encode_name(self.name)
            + bytes((self.colour, int(self.haptic),
                     self.indent1, self.indent2, self.steps))
            + _step_names_blob(self.steps, self.step_names)
        )


def set_knob_control_config(cfg: KnobConfig) -> bytes:
    return build_frame(CmdType.MIDI, Midi.SET_KNOB_CONTROL_CONFIG, cfg.payload())


@dataclass
class SwitchConfig:
    """A MIDI-setup switch control (spec 3.8 SET SWITCH CONTROL CONFIG).

    Wire payload (base 0x1D bytes + steps*0x0D for SN):
        SI CI CM CC CP NA:2 MN:2 MX:2 CN:13 CS LN LF HM HS SN[HS*13]
    """
    setup_index: int
    control_index: int
    control_mode: ControlMode = ControlMode.CC_7BIT
    channel: int = 1
    param: int = 0                # CC param / program number / note value (0xFF unused for NRPN)
    nrpn_address: int = 0         # NRPN addr, or PROGRAM CHANGE bank-select (0xFFFF if none)
    min_value: int = 0
    max_value: int = 0x7F
    name: str = ""
    colour: int = 0
    led_on: int = 0               # LED ON colour 0x00 - 0x52
    led_off: int = 0              # LED OFF colour 0x00 - 0x52
    haptic: SwitchHaptic = SwitchHaptic.PUSH
    steps: int = 0                # 0 for a plain two-position switch, else 2 - 10
    step_names: List[str] = field(default_factory=list)

    def payload(self) -> bytes:
        return (
            bytes((self.setup_index, self.control_index, int(self.control_mode),
                   self.channel, self.param))
            + _u16_be(self.nrpn_address)
            + _u16_be(self.min_value)
            + _u16_be(self.max_value)
            + encode_name(self.name)
            + bytes((self.colour, self.led_on, self.led_off,
                     int(self.haptic), self.steps))
            + _step_names_blob(self.steps, self.step_names)
        )


def set_switch_control_config(cfg: SwitchConfig) -> bytes:
    return build_frame(CmdType.MIDI, Midi.SET_SWITCH_CONTROL_CONFIG, cfg.payload())


# --------------------------------------------------------------------------
# PLUGIN commands (0x03)  -- Phase 2; a plugin is addressed by an 8-byte hash
# --------------------------------------------------------------------------

def get_current_plugin() -> bytes:
    return build_frame(CmdType.PLUGIN, Plugin.GET_CURRENT_PLUGIN)


def get_first_plugin() -> bytes:
    return build_frame(CmdType.PLUGIN, Plugin.GET_FIRST_PLUGIN)


def get_next_plugin() -> bytes:
    return build_frame(CmdType.PLUGIN, Plugin.GET_NEXT_PLUGIN)


def get_plugin(plugin_hash: bytes) -> bytes:
    _check_hash(plugin_hash)
    return build_frame(CmdType.PLUGIN, Plugin.GET_PLUGIN, plugin_hash)


def add_plugin(plugin_hash: bytes, name: str) -> bytes:
    _check_hash(plugin_hash)
    return build_frame(CmdType.PLUGIN, Plugin.ADD_PLUGIN,
                       plugin_hash + encode_name(name))


def clear_plugin(plugin_hash: bytes) -> bytes:
    """Delete a stored plugin map (spec CLEAR PLUGIN, payload = hash)."""
    _check_hash(plugin_hash)
    return build_frame(CmdType.PLUGIN, Plugin.CLEAR_PLUGIN, plugin_hash)


def clear_plugin_control(plugin_hash: bytes, is_switch: bool,
                         control_index: int) -> bytes:
    """Unassign one learned control (spec 4.13: PH:8 CT CI,
    CT = 00 knob / 01 switch)."""
    _check_hash(plugin_hash)
    return build_frame(CmdType.PLUGIN, Plugin.CLEAR_PLUGIN_CONTROL_CONFIG,
                       plugin_hash + bytes((1 if is_switch else 0,
                                            control_index)))


def set_plugin_name(plugin_hash: bytes, name: str) -> bytes:
    _check_hash(plugin_hash)
    return build_frame(CmdType.PLUGIN, Plugin.SET_PLUGIN_NAME,
                       plugin_hash + encode_name(name))


def clear_plugin(plugin_hash: bytes) -> bytes:
    _check_hash(plugin_hash)
    return build_frame(CmdType.PLUGIN, Plugin.CLEAR_PLUGIN, plugin_hash)


def clear_plugin_control_config(plugin_hash: bytes, control_type: ControlType,
                                control_index: int) -> bytes:
    _check_hash(plugin_hash)
    return build_frame(CmdType.PLUGIN, Plugin.CLEAR_PLUGIN_CONTROL_CONFIG,
                       plugin_hash + bytes((int(control_type), control_index)))


@dataclass
class PluginKnobConfig:
    """A plugin-mode knob control (spec 4.11 SET PLUGIN KNOB CONFIG).

    Wire payload (base 0x27 bytes + steps*0x0D for SN):
        PH:8 CI MI MH:6 MA MN:2 MX:2 CN:13 CS HM IP1 IP2 HS SN[HS*13]
    """
    plugin_hash: bytes                       # 8 bytes
    control_index: int                       # 0x00 - 0x3F
    mapped_param_index: int = 0              # MI
    mapped_param_hash: bytes = b"\x00" * 6   # MH, 6 bytes
    macro_param: bool = False                # MA
    min_value: int = 0
    max_value: int = 0x7F
    name: str = ""
    colour: int = 0
    haptic: KnobHaptic = KnobHaptic.KNOB_300
    indent1: int = UNUSED_INDENT
    indent2: int = UNUSED_INDENT
    steps: int = 0
    step_names: List[str] = field(default_factory=list)

    def payload(self) -> bytes:
        _check_hash(self.plugin_hash)
        if len(self.mapped_param_hash) != 6:
            raise ValueError("mapped_param_hash must be 6 bytes")
        return (
            self.plugin_hash
            + bytes((self.control_index, self.mapped_param_index))
            + self.mapped_param_hash
            + bytes((1 if self.macro_param else 0,))
            + _u16_be(self.min_value)
            + _u16_be(self.max_value)
            + encode_name(self.name)
            + bytes((self.colour, int(self.haptic),
                     self.indent1, self.indent2, self.steps))
            + _step_names_blob(self.steps, self.step_names)
        )


def set_plugin_knob_config(cfg: PluginKnobConfig) -> bytes:
    return build_frame(CmdType.PLUGIN, Plugin.SET_PLUGIN_KNOB_CONFIG, cfg.payload())


@dataclass
class PluginSwitchConfig:
    """A plugin-mode switch control (layout confirmed from the official
    implementation, resolving spec 4.12's ambiguity: min/max are SINGLE bytes
    and there is no macro byte).

    Wire payload (base 0x25 bytes + steps*0x0D for SN):
        PH:8 CI MI:2 MH:6 MN MX CN:13 CS LN LF HM HS SN[HS*13]
    """
    plugin_hash: bytes
    control_index: int
    mapped_param_index: int = 0
    mapped_param_hash: bytes = b"\x00" * 6
    min_value: int = 0                       # single byte
    max_value: int = 0x7F                    # single byte
    name: str = ""
    colour: int = 0
    led_on: int = 0
    led_off: int = 0
    haptic: SwitchHaptic = SwitchHaptic.TOGGLE
    steps: int = 0
    step_names: List[str] = field(default_factory=list)

    def payload(self) -> bytes:
        _check_hash(self.plugin_hash)
        if len(self.mapped_param_hash) != 6:
            raise ValueError("mapped_param_hash must be 6 bytes")
        return (self.plugin_hash
                + bytes((self.control_index,))
                + _u16_be(self.mapped_param_index)
                + self.mapped_param_hash
                + bytes((self.min_value & 0xFF, self.max_value & 0xFF))
                + encode_name(self.name)
                + bytes((self.colour, self.led_on, self.led_off,
                         int(self.haptic), self.steps))
                + _step_names_blob(self.steps, self.step_names))


def set_plugin_switch_config(cfg: PluginSwitchConfig) -> bytes:
    return build_frame(CmdType.PLUGIN, Plugin.SET_PLUGIN_SWITCH_CONFIG,
                       cfg.payload())


def _check_hash(plugin_hash: bytes) -> None:
    if len(plugin_hash) != 8:
        raise ValueError("plugin_hash must be 8 bytes")


# --------------------------------------------------------------------------
# Response parsing (host <- roto)
# --------------------------------------------------------------------------

@dataclass
class Response:
    code: int
    data: bytes

    @property
    def ok(self) -> bool:
        return self.code == RespCode.SUCCESS


def parse_response(frame: bytes) -> Response:
    """`frame` is a full response starting with 0xA5 (the transport slices it to
    the correct length using RESPONSE_DATA_LEN)."""
    if len(frame) < 2 or frame[0] != RESP_START:
        raise ProtocolError(f"not a response frame: {frame!r}")
    return Response(code=frame[1], data=frame[2:])


@dataclass
class FirmwareVersion:
    major: int
    minor: int
    patch: int
    git_commit: str

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}+{self.git_commit}"


def decode_fw_version(data: bytes) -> FirmwareVersion:
    if len(data) < 10:
        raise ProtocolError("short fw-version payload")
    return FirmwareVersion(data[0], data[1], data[2],
                           data[3:10].decode("ascii", errors="replace"))


@dataclass
class ModeState:
    mode: Mode
    page: int   # human page number, 1-based

    def __str__(self) -> str:
        return f"{self.mode.name} (page {self.page})"


def decode_mode(data: bytes) -> ModeState:
    if len(data) < 2:
        raise ProtocolError("short mode payload")
    return ModeState(Mode(data[0]), data[1] // PAGE_STRIDE + 1)


@dataclass
class SetupInfo:
    index: int
    name: str


def decode_setup(data: bytes) -> SetupInfo:
    if len(data) < 1 + NAME_LEN:
        raise ProtocolError("short setup payload")
    return SetupInfo(data[0], decode_name(data[1:1 + NAME_LEN]))


# DATA bytes (after A5 RC) in a *successful* response, keyed by the command
# that was sent. The transport uses this to know how many bytes to read
# (responses carry no length field). Config GETs are FIXED length: the step-
# name array is always GET_STEP_NAMES (16) strings — values cross-checked
# against the official implementation's expectBytes.
_SN_BLOCK = GET_STEP_NAMES * NAME_LEN                        # 16 * 13 = 208

RESPONSE_DATA_LEN = {
    (CmdType.GENERAL, General.GET_FW_VERSION): 10,          # VX VY VZ GC[7]
    (CmdType.GENERAL, General.GET_MODE): 2,                 # AM PI
    (CmdType.MIDI, Midi.GET_CURRENT_SETUP): 1 + NAME_LEN,   # SI SN[13]
    (CmdType.MIDI, Midi.GET_SETUP): 1 + NAME_LEN,
    (CmdType.MIDI, Midi.GET_KNOB_CONTROL_CONFIG): 29 + _SN_BLOCK,     # 237
    (CmdType.MIDI, Midi.GET_SWITCH_CONTROL_CONFIG): 29 + _SN_BLOCK,   # 237
    (CmdType.PLUGIN, Plugin.GET_CURRENT_PLUGIN): 8 + NAME_LEN + 2,  # PH PN PT DT
    (CmdType.PLUGIN, Plugin.GET_FIRST_PLUGIN): 8 + NAME_LEN + 1,    # PH PN PT
    (CmdType.PLUGIN, Plugin.GET_NEXT_PLUGIN): 8 + NAME_LEN + 1,     # PH PN PT
    (CmdType.PLUGIN, Plugin.GET_PLUGIN): 8 + NAME_LEN,              # PH PN
    (CmdType.PLUGIN, Plugin.GET_PLUGIN_KNOB_CONFIG): 40 + _SN_BLOCK,   # 248
    (CmdType.PLUGIN, Plugin.GET_PLUGIN_SWITCH_CONFIG): 37 + _SN_BLOCK, # 245
}


# --- config GET decoders (fixed-length responses -> the SET dataclasses) ----

def _read_step_names(data: bytes, offset: int, steps: int) -> List[str]:
    names = []
    for i in range(min(steps, GET_STEP_NAMES)):
        start = offset + i * NAME_LEN
        names.append(decode_name(data[start:start + NAME_LEN]))
    return names


def decode_knob_config(data: bytes) -> KnobConfig:
    """GET KNOB CONTROL CONFIG data: SI CI CM CC CP NA:2 MN:2 MX:2 CN:13 CS HM
    IP1 IP2 HS SN[16*13]."""
    if len(data) < 29 + _SN_BLOCK:
        raise ProtocolError("short knob-config payload")
    steps = data[28]
    return KnobConfig(
        setup_index=data[0], control_index=data[1],
        control_mode=ControlMode(data[2]), channel=data[3], param=data[4],
        nrpn_address=(data[5] << 8) | data[6],
        min_value=(data[7] << 8) | data[8], max_value=(data[9] << 8) | data[10],
        name=decode_name(data[11:24]), colour=data[24],
        haptic=KnobHaptic(data[25]), indent1=data[26], indent2=data[27],
        steps=steps, step_names=_read_step_names(data, 29, steps))


def decode_switch_config(data: bytes) -> SwitchConfig:
    """GET SWITCH CONTROL CONFIG data: SI CI CM CC CP NA:2 MN:2 MX:2 CN:13 CS
    LN LF HM HS SN[16*13]."""
    if len(data) < 29 + _SN_BLOCK:
        raise ProtocolError("short switch-config payload")
    steps = data[28]
    return SwitchConfig(
        setup_index=data[0], control_index=data[1],
        control_mode=ControlMode(data[2]), channel=data[3], param=data[4],
        nrpn_address=(data[5] << 8) | data[6],
        min_value=(data[7] << 8) | data[8], max_value=(data[9] << 8) | data[10],
        name=decode_name(data[11:24]), colour=data[24],
        led_on=data[25], led_off=data[26], haptic=SwitchHaptic(data[27]),
        steps=steps, step_names=_read_step_names(data, 29, steps))


def decode_plugin_knob_config(data: bytes) -> PluginKnobConfig:
    """GET PLUGIN KNOB CONFIG data: PH:8 CI MI:2 MH:6 MA MN:2 MX:2 CN:13 CS HM
    IP1 IP2 HS SN[16*13]."""
    if len(data) < 40 + _SN_BLOCK:
        raise ProtocolError("short plugin-knob-config payload")
    steps = data[39]
    return PluginKnobConfig(
        plugin_hash=bytes(data[0:8]), control_index=data[8],
        mapped_param_index=(data[9] << 8) | data[10],
        mapped_param_hash=bytes(data[11:17]), macro_param=bool(data[17]),
        min_value=(data[18] << 8) | data[19], max_value=(data[20] << 8) | data[21],
        name=decode_name(data[22:35]), colour=data[35],
        haptic=KnobHaptic(data[36]), indent1=data[37], indent2=data[38],
        steps=steps, step_names=_read_step_names(data, 40, steps))


def decode_plugin_switch_config(data: bytes) -> dict:
    """GET PLUGIN SWITCH CONFIG data: PH:8 CI MI:2 MH:6 MN MX CN:13 CS LN LF HM
    HS SN[16*13]. Note: min/max are SINGLE bytes here (per the official
    parser's 37-byte base). Returned as a dict (no SET counterpart yet)."""
    if len(data) < 37 + _SN_BLOCK:
        raise ProtocolError("short plugin-switch-config payload")
    steps = data[36]
    return {"plugin_hash": bytes(data[0:8]), "control_index": data[8],
            "mapped_param_index": (data[9] << 8) | data[10],
            "mapped_param_hash": bytes(data[11:17]),
            "min_value": data[17], "max_value": data[18],
            "name": decode_name(data[19:32]), "colour": data[32],
            "led_on": data[33], "led_off": data[34], "haptic": data[35],
            "steps": steps, "step_names": _read_step_names(data, 37, steps)}


@dataclass
class PluginInfo:
    hash: bytes
    name: str
    plugin_type: int = 0
    daw_type: int = 0


def decode_plugin_info(data: bytes) -> PluginInfo:
    """GET FIRST/NEXT/CURRENT PLUGIN data: PH:8 PN:13 [PT [DT]]."""
    if len(data) < 8 + NAME_LEN:
        raise ProtocolError("short plugin-info payload")
    return PluginInfo(hash=bytes(data[0:8]), name=decode_name(data[8:8 + NAME_LEN]),
                      plugin_type=data[21] if len(data) > 21 else 0,
                      daw_type=data[22] if len(data) > 22 else 0)


# --------------------------------------------------------------------------
# Async parsing (host <- roto, unsolicited)
# --------------------------------------------------------------------------

@dataclass
class AsyncEvent:
    cmd_type: int
    subtype: int
    data: bytes


def parse_async(frame: bytes) -> AsyncEvent:
    """`frame` starts with 0x5A. Returns the raw event; semantic interpretation
    (mode changed / setup selected / control learned / plugin ...) is done by
    the client from (cmd_type, subtype)."""
    if len(frame) < 5 or frame[0] != CMD_START:
        raise ProtocolError(f"not an async frame: {frame!r}")
    length = (frame[3] << 8) | frame[4]
    data = frame[5:5 + length]
    if len(data) < length:
        raise ProtocolError(
            f"truncated async frame: declared {length}, got {len(data)} bytes")
    return AsyncEvent(frame[1], frame[2], data)
