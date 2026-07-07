"""Protocol constants for the ROTO-CONTROL Serial API v1.2.

Every value here is a raw wire byte straight from the spec
(docs/ROTO-CONTROL-Serial-API-v1.2.pdf). Byte/field layouts live in codec.py.
"""
from enum import IntEnum

# --- Framing ---------------------------------------------------------------
CMD_START = 0x5A    # first byte of a command TO roto, and of an async command FROM roto
RESP_START = 0xA5   # first byte of a response FROM roto to one of our commands

# Serial line settings (spec section 1, "Introduction").
BAUDRATE = 115200
BYTESIZE = 8
PARITY = "N"
STOPBITS = 1

NAME_LEN = 0x0D     # 13: width of every "0D-byte NULL terminated ASCII" field


class CmdType(IntEnum):
    GENERAL = 0x01
    MIDI = 0x02
    PLUGIN = 0x03


class General(IntEnum):
    GET_FW_VERSION = 0x01
    GET_MODE = 0x02
    SET_MODE = 0x03          # also async FROM roto when mode changes on-device
    START_CONFIG_UPDATE = 0x04
    END_CONFIG_UPDATE = 0x05
    FACTORY_RESET = 0x06


class Midi(IntEnum):
    GET_CURRENT_SETUP = 0x01
    GET_SETUP = 0x02
    SET_SETUP = 0x03         # also async FROM roto when a setup is selected on-device
    SET_SETUP_NAME = 0x04
    GET_KNOB_CONTROL_CONFIG = 0x05
    GET_SWITCH_CONTROL_CONFIG = 0x06
    SET_KNOB_CONTROL_CONFIG = 0x07
    SET_SWITCH_CONTROL_CONFIG = 0x08
    CLEAR_CONTROL_CONFIG = 0x09
    CLEAR_MIDI_SETUP = 0x0A
    MIDI_CONTROL_LEARNED = 0x0B   # async only, FROM roto


class Plugin(IntEnum):
    GET_CURRENT_PLUGIN = 0x01
    GET_FIRST_PLUGIN = 0x02
    GET_NEXT_PLUGIN = 0x03
    GET_PLUGIN = 0x04
    SET_PLUGIN = 0x05             # async only, FROM roto (plugin selected on-device)
    ADD_PLUGIN = 0x06
    SET_PLUGIN_NAME = 0x07
    CLEAR_PLUGIN = 0x08
    GET_PLUGIN_KNOB_CONFIG = 0x09
    GET_PLUGIN_SWITCH_CONFIG = 0x0A
    SET_PLUGIN_KNOB_CONFIG = 0x0B
    SET_PLUGIN_SWITCH_CONFIG = 0x0C
    CLEAR_PLUGIN_CONTROL_CONFIG = 0x0D
    PLUGIN_CONTROL_LEARNED = 0x0E   # async only, FROM roto


class Mode(IntEnum):
    MIDI = 0x00
    PLUGIN = 0x01
    MIX = 0x02


class ControlMode(IntEnum):
    CC_7BIT = 0x00
    CC_14BIT = 0x01
    NRPN_7BIT = 0x02
    NRPN_14BIT = 0x03
    PROGRAM_CHANGE = 0x04   # switch controls only
    NOTE = 0x05             # switch controls only


class KnobHaptic(IntEnum):
    KNOB_300 = 0x00                 # continuous 300-degree knob
    KNOB_N_STEP = 0x01              # detented into N steps
    KNOB_300_CENTRE_INDENT = 0x02   # continuous with a centre detent


class SwitchHaptic(IntEnum):
    PUSH = 0x00
    TOGGLE = 0x01


class ControlType(IntEnum):
    KNOB = 0x00
    SWITCH = 0x01


class RespCode(IntEnum):
    SUCCESS = 0x00
    PLUGIN_EXISTS = 0xFC
    # 0xFD doubles as "no plugin" (enumeration end) and "control unconfigured"
    # (config GETs) — confirmed from ROTO-SETUP's protocol.mjs
    # (RESPONSE_UNCONFIGURED = 0xFD).
    NO_PLUGIN = 0xFD
    UNCONFIGURED = 0xFD
    # Any other non-zero value is a generic ERROR.


# Config GET responses always carry a FIXED array of 16 step-name strings
# (13 bytes each), regardless of the haptic-steps value — per the official
# parser (expectBytes = base + 16 * nameLength). The PDF's "SN:10*0D" is hex.
GET_STEP_NAMES = 16


# Page index is expressed in multiples of 8 (page 1 = 0x00, page 2 = 0x08, ...).
PAGE_STRIDE = 0x08
# Indent position sentinel meaning "unused".
UNUSED_INDENT = 0xFF

# Valid ranges (inclusive) worth asserting on.
SETUP_INDEX_MAX = 0x3F
CONTROL_INDEX_MAX = 0x1F        # MIDI-setup controls: 0x00 - 0x1F
PLUGIN_CONTROL_INDEX_MAX = 0x3F  # plugin controls: 0x00 - 0x3F
COLOUR_MAX = 0x52
CHANNEL_MIN, CHANNEL_MAX = 0x01, 0x10
