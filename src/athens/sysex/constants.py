"""Constants for the native DAW SysEx protocol.

Reverse-read from ROTO-SETUP.app's Ableton Remote Script (ROTO_CONTROL.py).
"""
from enum import IntEnum

SYSEX_START = 0xF0
SYSEX_END = 0xF7
MANUFACTURER_ID = (0x00, 0x22, 0x03)   # Melbourne Instruments
DEVICE_ID = 0x02                       # ROTO-CONTROL

STRING_LEN = 13                        # MAX_STRING_LENGTH: names/values, 7-bit ASCII, NUL-padded

# Real-time value channel (CC), separate from the SysEx channel.
VALUE_MIDI_CHANNEL = 15                # 0-based -> MIDI channel 16
ENCODER_FIRST_CC = 12                  # 8 encoders, 14-bit (absolute_14_bit)
ENCODER_LSB_CC_OFFSET = 32             # LSB rides on CC+32
BUTTON_FIRST_CC = 20                   # 8 surface buttons (DAW mode)
TRANSPORT_FIRST_CC = 28                # MIDI_FIRST_CC + 2*NUM_ENCODERS
TOUCH_FIRST_CC = 52                    # knob touch events
# the two on-device page arrows (config.lua BUTTON_LEFT/RIGHT) — how the Logic
# dialect pages the bank; the Ableton ROTO_PAGE SysEx is not emitted here.
MIX_PAGE_LEFT_CC = 0x3C                # 60 - page/bank left
MIX_PAGE_RIGHT_CC = 0x3D               # 61 - page/bank right
VOLUME_CC = 64                         # default volume CC
METERS_FIRST_CC = 65                   # VU meters
VALUE_UPDATE_FPS = 12

NUM_ENCODERS = 8


class TransportAction(IntEnum):
    """Transport buttons: CC = TRANSPORT_FIRST_CC + action, both directions
    (device sends presses; DAW sends 127/0 LED state on the same CC)."""
    PLAY = 0
    STOP = 1
    RECORD = 2
    SESSION_RECORD = 3
    LOOP = 4
    PUNCH_IN = 5
    PUNCH_OUT = 6
    REENABLE_AUTOMATION = 7
    REWIND = 8                         # playhead jump/scrub, no LED state
    FASTFORWARD = 9
    METRONOME = 10                     # not a wire offset — our assignment
    #                                    for an unlabeled Logic-grid button


class Group(IntEnum):
    GENERAL = 0x0A
    PLUGIN = 0x0B
    MIXER = 0x0C


class General(IntEnum):
    DAW_STARTED = 0x1                  # DAW -> device (on connect)
    PING_DAW = 0x2                     # device -> DAW
    DAW_PING_RESP = 0x3               # DAW -> device, [daw_id]
    NUM_TRACKS = 0x4                  # DAW -> device, u14
    FIRST_TRACK = 0x5                # DAW -> device, u14 (page's first track)
    SET_FIRST_TRACK = 0x6            # device -> DAW, u14
    TRACK_DETAILS = 0x7             # DAW -> device, u14 idx + name[13] + colour + foldable
    TRACK_DETAILS_END = 0x8         # DAW -> device
    SELECT_TRACK = 0x9             # device -> DAW, u14
    REQUEST_TRANSPORT_STATUS = 0xA  # device -> DAW
    TRANSPORT_STATUS = 0xB         # DAW -> device, 8 status bytes
    ROTO_DAW_CONNECTED = 0xC       # device -> DAW (triggers init)
    # --- Logic dialect (daw_id=3, from logic/config.lua + wire captures) ---
    SET_TRACK_DETAILS = 0x11       # DAW -> device, [0, slot, name13, colour, 0]
    RESET_TRACK_DETAILS = 0x12     # DAW -> device, [0, slot]: blank a strip
    SET_TRACK_COLOR = 0x13         # DAW -> device, [0, slot, rgb6]
    SET_CURRENT_TRACK_NAME = 0x16  # DAW -> device, [name13]; selection trio
    #                                with FOCUS_TRACK + 0x17 (logic-mix capture)
    SET_CURRENT_TRACK_COLOR = 0x17  # DAW -> device, [rgb6]
    PARAM_VALUES = 0x18            # DAW -> device, [param u14, value string]


class Plugin(IntEnum):
    SET_PLUGIN_MODE = 0x1           # device -> DAW
    NUM_DEVICES = 0x2               # DAW -> device, [count]
    FIRST_DEVICE = 0x3             # DAW -> device, [index]
    SET_FIRST_DEVICE = 0x4        # device -> DAW, [index]
    PLUGIN_DETAILS = 0x5
    PLUGIN_DETAILS_END = 0x6
    ROTO_CONTROL_SELECT_DEVICE = 0x7  # device -> DAW, [index]
    DAW_SELECT_PLUGIN = 0x8        # DAW -> device
    SET_DEVICE_LEARN = 0x9
    LEARN_PARAM = 0xA             # DAW -> device
    CONTROL_MAPPED = 0xB
    SET_PLUGIN_ENABLE = 0xC
    SET_PLUGIN_LOCK = 0xD
    UNMAP_CTL = 0xE
    SET_MAPPED_CTL_NAME = 0xF      # DAW -> device
    # --- Logic-dialect overlays (device -> DAW mode announcements) ---
    SET_TRACK_SELECT_MODE = 0x15   # track-select overlay opened
    SET_PLUGIN_ENABLE_MODE = 0x16  # plugin enable/bypass overlay opened
    SET_PLUGIN_SELECT_MODE = 0x17  # plugin-select overlay opened
    # --- Logic-dialect sweep-based learn (daw_id=3, logic/config.lua) ---
    SET_PLUGIN_CTL_DETAILS = 0x13    # DAW -> device: smart ctl name [0, ctl, name13, colour]
    PLUGIN_PARAM_SWEEP = 0x19        # DAW -> device: start/advance a learn sweep, [param u14]
    PLUGIN_PARAM_SWEEP_VALUE = 0x1A  # device -> DAW: [chan_off, lsb_cc, msb_cc, lsb, msb]
    PLUGIN_LEARN_COMPLETE = 0x1C     # device -> DAW: mapping stored
    PLUGIN_LEARN_RESTART = 0x1D      # DAW -> device: user moved the knob mid-sweep
    REQUEST_SWITCH_PARAM_VALUE = 0x1E  # device -> DAW: wants PARAM_VALUES for a switch


class Mixer(IntEnum):
    SET_MIXER_ALL_MODE = 0x1        # device -> DAW
    SET_MIXER_SELECTED_MODE = 0x2  # device -> DAW
    NUM_SENDS = 0x3                # DAW -> device, [count]
    DAW_SELECT_TRACK = 0x4        # DAW -> device, u14 idx + name[13] + colour + foldable
    SET_MIXER_CHANNEL_MODE = 0x5  # device -> DAW
    TOGGLE_GROUP_TRACK = 0x6      # device -> DAW
    DAW_SELECT_FOCUS_TRACK = 0xA  # DAW -> device (Logic): [0, slot, name13, rgb6]
    SET_MIX_VU_METER_POINTS = 0xB  # DAW -> device, [yellow, red]
    SET_MIX_VU_METER_STATES = 0xC


class DawId(IntEnum):
    # We masquerade as Logic Pro: the device gates its dialect on this id, and
    # daw_id=3 unlocks the richer sweep-based learn (full mapping incl. buttons)
    # + end-stops. (config.lua: LOGIC_PRO_DAW = 3.)
    LOGIC_PRO = 3


class LearnMode(IntEnum):
    """SET_DEVICE_LEARN payload in the Logic dialect (3-state, not on/off)."""
    DISABLED = 0
    ENABLED = 1
    END = 2


# Logic dialect (daw_id=3) COMMAND channel from config.lua (MAX_SENDS=12): CC on
# MIDI channel 7 (status 0xB6). Logic-INTERNAL — never emitted on the wire; see
# RotoLogicClient.send_command.
LOGIC_COMMAND_STATUS = 0xB6            # CC, MIDI channel 7 (MIDI_CHANNELS.COMMAND)


class LogicCommand(IntEnum):
    """The config.lua command-CC table (~309-342, MAX_SENDS=12). Logic-internal
    (see LOGIC_COMMAND_STATUS) — listed for reference, not sent on the wire."""
    MIXER_VOLUME = 0x51
    MIXER_PAN = 0x52
    MIXER_SEND_0 = 0x53            # +send index, 12 slots (0x53-0x5E)
    MIXER_MUTE = 0x5F
    MIXER_SOLO = 0x60
    MIXER_ARM = 0x61
    MIXER_INPUT_MON = 0x62
    FOCUS = 0x63
    SMART = 0x64
    PLUGIN = 0x65
    PLUGIN_SLOT = 0x66
    INSTRUMENT = 0x67
    PLUGIN_ENABLE = 0x68
    PLUGIN_SELECT = 0x69
    INSTRUMENT_PARAMS = 0x6A
    PLUGIN_PARAMS = 0x6B
    TRANSPORT = 0x6C
    TRACK_1_SELECT = 0x6D          # +slot, 8 slots (0x6D-0x74)
    TRACK_TOGGLE = 0x75
    FOCUS_2 = 0x76
    INSTRUMENT_SWEEP = 0x77
    PLUGIN_SWEEP = 0x78
    PING = 0x79
    GLOBAL = 0x7A
    NULL_KNOB = 0x7B
    NULL_BUTTON = 0x7C
    TOUCH_VALUE = 0x7D


# Logic dialect: plugin parameter VALUES ride CC on their own channel block —
# param i lives on channel status (PLUGIN_CHAN_END_STATUS - i//32), value MSB
# on CC i%32, LSB on CC 0x20 + i%32, knob-touch on CC 0x40 + i%32. 8 channels
# x 32 params = 256 (config.lua MAX_PLUGIN_PARAMETERS, controls table ~1302).
PLUGIN_CHAN_END_STATUS = 0xBE          # params 0-31 (highest channel)
PLUGIN_CHAN_START_STATUS = 0xB7        # params 224-255 (lowest channel)
PLUGIN_PARAMS_PER_CHANNEL = 32
PLUGIN_LSB_CC_OFFSET = 0x20
PLUGIN_TOUCH_FIRST_CC = 0x40
MAX_PLUGIN_PARAMETERS = 256
SMART_MODE_PARAMS = 16                 # smart mode exposes 16 curated controls


# VU meter thresholds. Logic's connect preamble sends 0x6A/0x78; the Ableton
# script used 87/113.
METER_LEVEL_YELLOW = 0x6A   # 106
METER_LEVEL_RED = 0x78      # 120

# Plugin-mode extras.
HAPTIC_CENTER_INDENT = 0
MACRO_PLUGIN_PAGES = 0
MAX_QUANTISED_STEPS = 24
MAX_QUANTISED_STRING_STEPS = 16


class RackKind(IntEnum):
    NORMAL = 0
    MACRO_RACK = 1
    THIRD_PARTY_PLUGIN = 2


class ControlKind(IntEnum):
    KNOB = 0
    SWITCH = 1
