"""Serializer for the native DAW SysEx protocol.

Pure functions: build the messages the DAW sends to the device, and parse the
messages the device sends back. No I/O. See docs/DAW-SYSEX-PROTOCOL.md.

Framing:  F0 00 22 03 02 <group> <command> <data...> F7
14-bit values are big-endian 7-bit pairs (msb, lsb). Strings are 13 bytes,
7-bit ASCII, NUL-padded.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List

from .constants import (
    DEVICE_ID, HAPTIC_CENTER_INDENT, MACRO_PLUGIN_PAGES, MANUFACTURER_ID,
    MAX_PLUGIN_PARAMETERS, PLUGIN_CHAN_END_STATUS, PLUGIN_LSB_CC_OFFSET,
    PLUGIN_PARAMS_PER_CHANNEL, STRING_LEN, SYSEX_END, SYSEX_START,
    DawId, General, Group, Mixer, Plugin, RackKind,
)


class ProtocolError(Exception):
    pass


# --- helpers ---------------------------------------------------------------

def u14(value: int) -> bytes:
    """14-bit value as (msb, lsb), each 7-bit."""
    if not 0 <= value <= 0x3FFF:
        raise ValueError(f"u14 out of range: {value}")
    return bytes(((value >> 7) & 0x7F, value & 0x7F))


def parse_u14(data: bytes, offset: int = 0) -> int:
    return (data[offset] << 7) | data[offset + 1]


def frac14(msb: int, lsb: int) -> float:
    """14-bit MSB/LSB pair -> normalized 0.0..1.0. The device streams knob and
    param values as MSB then LSB-on-cc+32; this is the reassembly every decoder
    shares (encoder, plugin param, NRPN)."""
    return ((msb << 7) | lsb) / 0x3FFF


def frac7(msb: int) -> float:
    """7-bit value -> normalized 0.0..1.0 — the coarse fallback when an LSB was
    dropped and a pending MSB must be flushed rather than lost."""
    return msb / 127.0


def encode_string(text: str, length: int = STRING_LEN) -> bytes:
    # ascii+replace, NOT `& 0x7F` bit-folding: folding maps 'ä' (0xE4) to 'd'
    # (0x64) and code points that are multiples of 128 to embedded NULs
    raw = text.encode("ascii", errors="replace")[: length - 1]
    return raw + b"\x00" * (length - len(raw))


def decode_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def build_sysex(group: int, command: int, data: bytes = b"") -> bytes:
    return (bytes((SYSEX_START, *MANUFACTURER_ID, DEVICE_ID, group, command))
            + bytes(data) + bytes((SYSEX_END,)))


@dataclass
class SysexMessage:
    group: int
    command: int
    data: bytes


def parse_sysex(msg: bytes) -> SysexMessage:
    if (len(msg) < 8 or msg[0] != SYSEX_START or msg[-1] != SYSEX_END
            or tuple(msg[1:4]) != MANUFACTURER_ID or msg[4] != DEVICE_ID):
        raise ProtocolError(f"not a ROTO sysex frame: {bytes(msg)!r}")
    return SysexMessage(msg[5], msg[6], bytes(msg[7:-1]))


# --- GENERAL (DAW -> device) ----------------------------------------------

def daw_started() -> bytes:
    return build_sysex(Group.GENERAL, General.DAW_STARTED)


def daw_ping_response(daw_id: int = DawId.LOGIC_PRO) -> bytes:
    return build_sysex(Group.GENERAL, General.DAW_PING_RESP, bytes((daw_id,)))


def num_tracks(count: int) -> bytes:
    return build_sysex(Group.GENERAL, General.NUM_TRACKS, u14(count))


def first_track(index: int) -> bytes:
    return build_sysex(Group.GENERAL, General.FIRST_TRACK, u14(index))


def track_details(index: int, name: str, colour: int, foldable: bool = False) -> bytes:
    return build_sysex(Group.GENERAL, General.TRACK_DETAILS,
                       u14(index) + encode_string(name)
                       + bytes((colour, 1 if foldable else 0)))


def track_details_end() -> bytes:
    return build_sysex(Group.GENERAL, General.TRACK_DETAILS_END)


def transport_status(*, playing=False, recording=False, session_recording=False,
                     loop=False, punch_in=False, punch_out=False,
                     reenable_automation=False) -> bytes:
    # byte 1 is reserved (stop has no state) per the Ableton script
    return build_sysex(Group.GENERAL, General.TRANSPORT_STATUS, bytes((
        int(playing), 0, int(recording), int(session_recording), int(loop),
        int(punch_in), int(punch_out), int(reenable_automation))))


def param_value(index: int, value_text: str, is_button: bool = False) -> bytes:
    """The formatted value read-out shown under a knob/button (e.g. '1.2 kHz')."""
    return build_sysex(Group.GENERAL, General.PARAM_VALUES,
                       bytes((1 if is_button else 0, index)) + encode_string(value_text))


# --- MIXER (DAW -> device) -------------------------------------------------

def num_sends(count: int) -> bytes:
    return build_sysex(Group.MIXER, Mixer.NUM_SENDS, bytes((count,)))


def daw_select_track(index: int, name: str, colour: int, foldable: bool = False) -> bytes:
    return build_sysex(Group.MIXER, Mixer.DAW_SELECT_TRACK,
                       u14(index) + encode_string(name)
                       + bytes((colour, 1 if foldable else 0)))


def vu_meter_points(yellow: int, red: int) -> bytes:
    return build_sysex(Group.MIXER, Mixer.SET_MIX_VU_METER_POINTS, bytes((yellow, red)))


# --- PLUGIN (DAW -> device) ------------------------------------------------
# Devices and their params are identified by 7-bit-safe SHA-1 digests, so the
# device can remember a mapping across sessions ("learn once, remembers forever").

def sha1_7bit(data: bytes, size: int) -> bytes:
    """First `size` bytes of SHA-1(data), each masked to 7 bits."""
    return bytes(b & 0x7F for b in hashlib.sha1(data).digest()[:size])


def device_hash(name: str) -> bytes:
    """8-byte device identity hash (REAPER: hash the FX name)."""
    return sha1_7bit(name.encode("utf-8"), 8)


def param_hash(name: str) -> bytes:
    """6-byte parameter identity hash."""
    return sha1_7bit(name.encode("utf-8"), 6)


def num_devices(count: int) -> bytes:
    return build_sysex(Group.PLUGIN, Plugin.NUM_DEVICES, bytes((count,)))


def first_device(index: int) -> bytes:
    return build_sysex(Group.PLUGIN, Plugin.FIRST_DEVICE, bytes((index,)))


def plugin_details(index: int, name: str, hash8: bytes, *, enabled: bool = True,
                   rack_kind: int = RackKind.NORMAL,
                   macro_pages: int = MACRO_PLUGIN_PAGES) -> bytes:
    """One device in the list: index, 8-byte hash, enabled, name[13], rack kind."""
    if len(hash8) != 8:
        raise ValueError("device hash must be 8 bytes")
    return build_sysex(Group.PLUGIN, Plugin.PLUGIN_DETAILS,
                       bytes((index,)) + hash8 + bytes((1 if enabled else 0,))
                       + encode_string(name) + bytes((rack_kind, macro_pages)))


def plugin_details_end() -> bytes:
    return build_sysex(Group.PLUGIN, Plugin.PLUGIN_DETAILS_END)


@dataclass
class PluginDetails:
    """Parsed PLUGIN_DETAILS — the plugin-select message that carries the
    identity the device keys its stored maps on. The cross-DAW Link proxy reads
    `name` (to resolve a canonical hash) and rewrites `hash8`."""
    index: int
    hash8: bytes
    enabled: bool
    name: str
    rack_kind: int
    macro_pages: int


# PLUGIN_DETAILS raw frame: F0 00 22 03 02 <grp> <cmd> <data> F7, data =
# [index][hash8 x8][enabled][name x13][rack_kind][macro_pages]. Data starts at
# offset 7, so the identity is the fixed 8-byte slice frame[8:16].
_PD_HASH_OFF = 8                          # 7 (data start) + 1 (index)
_PD_NAME_OFF = 7 + 1 + 8 + 1              # data start + index + hash8 + enabled
_PD_MIN_LEN = 7 + 1 + 8 + 1 + STRING_LEN + 2 + 1   # header+fields+end = 33


def is_plugin_details(frame: bytes) -> bool:
    """Cheap check (no allocation) that a raw frame is a PLUGIN_DETAILS — the
    proxy's hot-path filter, so it only parses the one message it rewrites."""
    return (len(frame) >= _PD_MIN_LEN and frame[0] == SYSEX_START
            and frame[-1] == SYSEX_END and tuple(frame[1:4]) == MANUFACTURER_ID
            and frame[4] == DEVICE_ID and frame[5] == Group.PLUGIN
            and frame[6] == Plugin.PLUGIN_DETAILS)


def parse_plugin_details(frame: bytes) -> PluginDetails:
    """Invert plugin_details(): pull identity + name out of a raw frame."""
    m = parse_sysex(frame)
    if m.group != Group.PLUGIN or m.command != Plugin.PLUGIN_DETAILS:
        raise ProtocolError("not a PLUGIN_DETAILS frame")
    d = m.data
    return PluginDetails(
        index=d[0], hash8=bytes(d[1:9]), enabled=bool(d[9]),
        name=decode_string(d[10:10 + STRING_LEN]),
        rack_kind=d[10 + STRING_LEN], macro_pages=d[10 + STRING_LEN + 1])


def rewrite_plugin_details_hash(frame: bytes, new_hash8: bytes) -> bytes:
    """Return the frame with ONLY its 8-byte identity replaced — the single
    edit the cross-DAW Link proxy makes; every other byte passes through."""
    if len(new_hash8) != 8:
        raise ValueError("device hash must be 8 bytes")
    return frame[:_PD_HASH_OFF] + bytes(new_hash8) + frame[_PD_HASH_OFF + 8:]


def learn_param(param_index: int, name: str, value: float, hash6: bytes, *,
                is_macro: bool = False, quantised_steps: int = 0,
                quantised_strings: bytes = b"") -> bytes:
    """Describe a plugin param mapped to a control: index, 6-byte hash, macro
    flag, haptic, quantise steps, current value (14-bit), name[13], step names."""
    if len(hash6) != 6:
        raise ValueError("param hash must be 6 bytes")
    v = max(0, min(0x3FFF, int(round(value * 0x3FFF))))
    return build_sysex(Group.PLUGIN, Plugin.LEARN_PARAM,
                       u14(param_index) + bytes(hash6)
                       + bytes((1 if is_macro else 0, HAPTIC_CENTER_INDENT,
                                quantised_steps, (v >> 7) & 0x7F, v & 0x7F))
                       + encode_string(name) + bytes(quantised_strings))


def set_mapped_control_name(param_index: int, name: str, hash6: bytes) -> bytes:
    if len(hash6) != 6:
        raise ValueError("param hash must be 6 bytes")
    return build_sysex(Group.PLUGIN, Plugin.SET_MAPPED_CTL_NAME,
                       u14(param_index) + bytes(hash6) + encode_string(name))


def daw_select_plugin(device_index: int, macro_pages: int = 0,
                      macro_force: int = 0) -> bytes:
    """Confirm/announce the DAW's selected device (Ableton script
    _send_selected_device_update: [index, MACRO_PLUGIN_PAGES, MACRO_FORCE_PLUGIN])."""
    return build_sysex(Group.PLUGIN, Plugin.DAW_SELECT_PLUGIN,
                       bytes((device_index, macro_pages, macro_force)))


# --- device -> DAW payload decoders ---------------------------------------
# Dispatch on (msg.group, msg.command); these decode the ones carrying data.

def decode_select_track(data: bytes) -> int:
    return parse_u14(data)


def decode_set_first_track(data: bytes) -> int:
    return parse_u14(data)


def decode_set_first_device(data: bytes) -> int:
    return data[0]


def decode_select_device(data: bytes) -> int:
    return data[0]


def decode_set_device_learn(data: bytes) -> bool:
    return bool(data[0])


@dataclass
class ControlMapped:
    """Device -> DAW: a control was mapped to a plugin param (CONTROL_MAPPED)."""
    param_index: int
    param_hash: bytes        # 6 bytes
    control_kind: int        # 0 = knob, 1 = switch
    control_index: int       # encoder / button 0-7
    is_macro: bool


def decode_control_mapped(data: bytes) -> ControlMapped:
    param_index = ((data[0] & 0x7F) << 7) | (data[1] & 0x7F)
    return ControlMapped(param_index=param_index, param_hash=bytes(data[2:8]),
                         control_kind=data[8], control_index=data[9],
                         is_macro=bool(data[10]))


# --- Logic dialect (daw_id=3) ------------------------------------------------
# Reverse-read from logic/config.lua + wire captures (reference/logic-protocol).
# Colours here are real RGB, sent as three (bit-7, low-7) byte pairs.

def rgb6(rgb: tuple) -> bytes:
    """(r, g, b) 0-255 each -> 6 bytes: per component (bit7, low 7 bits)."""
    out = bytearray()
    for c in rgb:
        c = max(0, min(255, int(c)))
        out += bytes(((c & 0x80) >> 7, c & 0x7F))
    return bytes(out)


def daw_select_focus_track(slot: int, name: str, rgb: tuple) -> bytes:
    """The focused track's strip: MIX/DAW_SELECT_FOCUS_TRACK
    [0, slot(0-7), name13, rgb6] (config.lua CONTROL_ID_SELECTED_TRACK)."""
    return build_sysex(Group.MIXER, Mixer.DAW_SELECT_FOCUS_TRACK,
                       bytes((0, slot & 0x7F)) + encode_string(name) + rgb6(rgb))


def set_current_track_name(name: str) -> bytes:
    return build_sysex(Group.GENERAL, General.SET_CURRENT_TRACK_NAME,
                       encode_string(name))


def set_current_track_color(rgb: tuple) -> bytes:
    return build_sysex(Group.GENERAL, General.SET_CURRENT_TRACK_COLOR, rgb6(rgb))


def set_track_color(slot: int, rgb: tuple) -> bytes:
    return build_sysex(Group.GENERAL, General.SET_TRACK_COLOR,
                       bytes((0, slot & 0x7F)) + rgb6(rgb))


def logic_track_details(slot: int, name: str, colour: int) -> bytes:
    """Mix-mode strip name (Logic dialect): GEN/SET_TRACK_DETAILS
    [0, slot(0-7), name13, palette_colour, 0]. NOTE hw fw3.2 accepts but does
    NOT render these — strip names need the Ableton-style track_details flow;
    kept for completeness/other firmwares."""
    return build_sysex(Group.GENERAL, General.SET_TRACK_DETAILS,
                       bytes((0, slot & 0x7F)) + encode_string(name)
                       + bytes((colour & 0x7F, 0)))


def reset_track_details(slot: int) -> bytes:
    """Blank a mix strip (config.lua: sent when Logic clears a screen)."""
    return build_sysex(Group.GENERAL, General.RESET_TRACK_DETAILS,
                       bytes((0, slot & 0x7F)))


def set_plugin_ctl_details(ctl_index: int, name: str, colour: int) -> bytes:
    """Smart-mode control name: PLG/SET_PLUGIN_CTL_DETAILS
    [0, ctl, name13, colour]."""
    return build_sysex(Group.PLUGIN, Plugin.SET_PLUGIN_CTL_DETAILS,
                       bytes((0, ctl_index & 0x7F)) + encode_string(name)
                       + bytes((colour & 0x7F,)))


def logic_param_value(param_index: int, value_text: str) -> bytes:
    """Formatted read-out for a PARAM (not a control): GEN/PARAM_VALUES
    [param u14, value13]. The device knows which control shows it."""
    return build_sysex(Group.GENERAL, General.PARAM_VALUES,
                       u14(param_index) + encode_string(value_text))


def plugin_param_sweep(param_index: int) -> bytes:
    """Start/advance the learn sweep of a param (DAW -> device)."""
    if not 0 <= param_index < MAX_PLUGIN_PARAMETERS:
        raise ValueError(f"sweepable param index 0-255, got {param_index}")
    return build_sysex(Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP, u14(param_index))


def plugin_learn_restart() -> bytes:
    return build_sysex(Group.PLUGIN, Plugin.PLUGIN_LEARN_RESTART)


@dataclass
class SweepValue:
    """Device -> DAW: 'set this param to this value' during a learn sweep.
    Wire data [chan_off, lsb_cc, msb_cc, lsb, msb] names the param by its
    value-CC address; the ramp's 7-bit step rides in the MSB byte."""
    param_index: int
    value: float             # normalised 0..1 (14-bit on the wire)
    step: int                # 7-bit ramp position (0x7f = sweep end)


def decode_sweep_value(data: bytes) -> SweepValue:
    if len(data) < 5:
        raise ProtocolError(f"short PLUGIN_PARAM_SWEEP_VALUE: {data.hex(' ')}")
    chan_off, _lsb_cc, msb_cc, lsb, msb = data[:5]
    param = ((PLUGIN_CHAN_END_STATUS & 0x0F) - (chan_off & 0x0F)) \
        * PLUGIN_PARAMS_PER_CHANNEL + (msb_cc % PLUGIN_LSB_CC_OFFSET)
    return SweepValue(param_index=param,
                      value=((msb << 7) | lsb) / 0x3FFF, step=msb)


def decode_switch_param_request(data: bytes) -> int:
    return parse_u14(data)


def logic_param_cc(param_index: int, value: float) -> List[bytes]:
    """A param's live value as its two value CCs (MSB then LSB) on the
    param's own channel — how plugin values flow DAW -> device in Logic mode."""
    if not 0 <= param_index < MAX_PLUGIN_PARAMETERS:
        raise ValueError(f"param index 0-255, got {param_index}")
    status = PLUGIN_CHAN_END_STATUS - param_index // PLUGIN_PARAMS_PER_CHANNEL
    cc = param_index % PLUGIN_PARAMS_PER_CHANNEL
    raw = max(0, min(0x3FFF, int(round(value * 0x3FFF))))
    return [bytes((status, cc, (raw >> 7) & 0x7F)),
            bytes((status, cc + PLUGIN_LSB_CC_OFFSET, raw & 0x7F))]
