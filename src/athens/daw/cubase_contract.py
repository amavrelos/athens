"""Wire contract between the Cubase MIDI Remote script
(the cubase/ MIDI Remote script) and CubaseSysexSource, over the `roto-bridge`
virtual MIDI pair.

Structure, text and live values all ride SysEx frames:

    F0 7D <cmd> <payload...> F7

0x7D is the private/non-commercial SysEx id — the pair carries only our
traffic, so no manufacturer id is needed. Track indices and continuous values
are 14-bit (two 7-bit bytes, MSB first, 0..0x3FFF). The SAME commands travel
both ways: the host reports its state with them, and Athens sends the same
frame back to *set* it (so `set_track_volume` and an incoming volume update are
one command). See cubase/README.md for the wiring.

This module is pure encode/decode — no MIDI I/O — so it is fully unit-testable
without Cubase or hardware.
"""
from __future__ import annotations

from enum import IntEnum
from typing import NamedTuple, Optional

SYSEX_START = 0xF0
SYSEX_END = 0xF7
PRIVATE_ID = 0x7D            # SysEx "non-commercial / educational" id

# Direction byte, sent immediately after PRIVATE_ID. A single virtual MIDI pair
# is a loopback: whatever one side sends, it also hears back. Without a
# direction tag Cubase re-applies the very state it just emitted (its mOnSysex
# consumes its own echo), which re-fires the change callback and storms the
# port. Tagging each frame lets each side ignore its OWN echo.
DIR_TO_ATHENS = 0x01        # Cubase -> Athens (state):   Athens acts, Cubase ignores
DIR_TO_CUBASE = 0x00        # Athens -> Cubase (control):  Cubase acts, Athens ignores


class Cmd(IntEnum):
    # -- mixer layer (per track, absolute index) --
    COUNT = 0x01            # [n14]                 total track count
    NAME = 0x02            # [idx14, ascii...]     track name (no terminator)
    VOLUME = 0x03          # [idx14, val14]        0..0x3FFF, normalised on decode
    PAN = 0x04            # [idx14, val14]        0x2000 = centre
    FLAG = 0x05          # [idx14, flag, on]     flag = Flag.*, on = 0/1
    SELECT = 0x06        # [idx14]               selected track (absolute)
    TRANSPORT = 0x07    # [bits]                bit0 play, bit1 record, bit2 loop
    HELLO = 0x08        # [ascii]  identity handshake: Cubase announces "cubase"
                        # (TO_ATHENS); Athens sends it empty (TO_CUBASE) to ask
    VU = 0x09           # [idx14, val14]        track meter level (live, TO_ATHENS)
    PAGE = 0x0A         # [dir]                 scroll the bank: 1 = next, 0 = prev
    # -- plugin layer (the selected track's inserts + the focused plugin) --
    DEVICE_COUNT = 0x10    # [n14]                 inserts on the selected track
    DEVICE_NAME = 0x11    # [idx14, ascii...]     insert name (the link identity)
    DEVICE_ENABLED = 0x12  # [idx14, on]           insert active/bypassed; also set
    FOCUS_DEVICE = 0x13   # [idx14]               focused insert; params below = its
    PARAM_COUNT = 0x14    # [n14]                 params the focused insert exposes
    PARAM_NAME = 0x15    # [slot14, ascii...]    param name
    PARAM_VALUE = 0x16   # [slot14, val14]       param value; also sent to set it
    PARAM_DISPLAY = 0x17  # [slot14, ascii...]    formatted read-out, "2.4 kHz"

    DIAG = 0x7F          # [ascii]  JS per-block load status ("pan=ok"/"mute=FAIL")


class Flag(IntEnum):
    """FLAG payload's flag byte — the index into daw.source.TRACK_FLAGS."""
    MUTE = 0
    SOLO = 1
    ARM = 2
    MONITOR = 3


# transport bit positions in the TRANSPORT payload byte
T_PLAY = 0x01
T_RECORD = 0x02
T_LOOP = 0x04

_FULL = 0x3FFF


def u14(value: int) -> bytes:
    v = max(0, min(_FULL, int(value)))
    return bytes(((v >> 7) & 0x7F, v & 0x7F))


def parse_u14(data: bytes, offset: int = 0) -> int:
    return (data[offset] << 7) | data[offset + 1]


def norm(value14: int) -> float:
    """14-bit -> 0.0..1.0."""
    return value14 / _FULL


def denorm(value: float) -> int:
    """0.0..1.0 -> 14-bit."""
    return max(0, min(_FULL, round(value * _FULL)))


def frame(cmd: Cmd, payload: bytes = b"", direction: int = DIR_TO_ATHENS) -> bytes:
    # default is Cubase->Athens state, because that's what every encoder below
    # simulates (host reports); Athens' own control sends are re-tagged by
    # `as_control` at the single outbound chokepoint (CubaseSysexSource._send).
    return bytes((SYSEX_START, PRIVATE_ID, direction, int(cmd))) + payload \
        + bytes((SYSEX_END,))


def as_control(frame_bytes: bytes) -> bytes:
    """Re-tag a built frame as Athens->Cubase control (flip the direction byte).
    Used on every outbound send; see the direction-byte note above for why."""
    return frame_bytes[:2] + bytes((DIR_TO_CUBASE,)) + frame_bytes[3:]


# -- encoders (used by tests, the JS mirror, and Athens' control sends) --------

def count(n: int) -> bytes:
    return frame(Cmd.COUNT, u14(n))


def name(index: int, text: str) -> bytes:
    return frame(Cmd.NAME, u14(index) + text.encode("ascii", "replace"))


def volume(index: int, value: float) -> bytes:
    return frame(Cmd.VOLUME, u14(index) + u14(denorm(value)))


def pan(index: int, value: float) -> bytes:
    return frame(Cmd.PAN, u14(index) + u14(denorm(value)))


def flag(index: int, which: Flag, on: bool) -> bytes:
    return frame(Cmd.FLAG, u14(index) + bytes((int(which), 1 if on else 0)))


def select(index: int) -> bytes:
    return frame(Cmd.SELECT, u14(index))


def transport(playing: bool = False, recording: bool = False,
              loop: bool = False) -> bytes:
    bits = (T_PLAY if playing else 0) | (T_RECORD if recording else 0) \
        | (T_LOOP if loop else 0)
    return frame(Cmd.TRANSPORT, bytes((bits,)))


def hello(tag: str = "") -> bytes:
    """Identity handshake. Cubase sends `hello("cubase")`; Athens sends it empty
    (re-tagged control by `_send`) as a 'who are you?' query on the bridge."""
    return frame(Cmd.HELLO, tag.encode("ascii", "replace"))


def vu(index: int, value: float) -> bytes:
    return frame(Cmd.VU, u14(index) + u14(denorm(value)))


def page(delta: int) -> bytes:
    """Scroll the mixer bank window (Athens -> Cubase): +ve = next, -ve = prev."""
    return frame(Cmd.PAGE, bytes((1 if delta > 0 else 0,)))


# plugin layer

def device_count(n: int) -> bytes:
    return frame(Cmd.DEVICE_COUNT, u14(n))


def device_name(index: int, text: str) -> bytes:
    return frame(Cmd.DEVICE_NAME, u14(index) + text.encode("ascii", "replace"))


def device_enabled(index: int, on: bool) -> bytes:
    return frame(Cmd.DEVICE_ENABLED, u14(index) + bytes((1 if on else 0,)))


def focus_device(index: int) -> bytes:
    return frame(Cmd.FOCUS_DEVICE, u14(index))


def param_count(n: int) -> bytes:
    return frame(Cmd.PARAM_COUNT, u14(n))


def param_name(slot: int, text: str) -> bytes:
    return frame(Cmd.PARAM_NAME, u14(slot) + text.encode("ascii", "replace"))


def param_value(slot: int, value: float) -> bytes:
    return frame(Cmd.PARAM_VALUE, u14(slot) + u14(denorm(value)))


def param_display(slot: int, text: str) -> bytes:
    return frame(Cmd.PARAM_DISPLAY, u14(slot) + text.encode("ascii", "replace"))


# -- decoder -------------------------------------------------------------------

class Message(NamedTuple):
    cmd: Cmd
    payload: bytes
    direction: int


def parse(data: bytes) -> Optional[Message]:
    """A raw MIDI frame -> Message, or None if it isn't one of ours."""
    if len(data) < 5 or data[0] != SYSEX_START or data[1] != PRIVATE_ID \
            or data[-1] != SYSEX_END:
        return None
    try:
        cmd = Cmd(data[3])
    except ValueError:
        return None
    return Message(cmd, data[4:-1], data[2])


def describe(raw: bytes) -> tuple[str, str]:
    """(label, comment) for a contract frame — powers the diagnostics trace."""
    m = parse(raw)
    if m is None:
        return (bytes(raw).hex(" "), "not a bridge frame")
    c, p = m.cmd, m.payload

    def trk() -> int:
        return parse_u14(p) + 1                     # 1-based for humans

    if c is Cmd.HELLO:
        tag = bytes(p).decode("ascii", "replace")
        return ("HELLO " + (repr(tag) if tag else "(WHO?)"),
                "identity" if tag else "who-are-you probe")
    if c is Cmd.COUNT:
        return ("COUNT %d" % parse_u14(p), "track count")
    if c is Cmd.NAME:
        return ("NAME trk%d %r" % (trk(), bytes(p[2:]).decode("ascii", "replace")),
                "track name")
    if c is Cmd.VOLUME:
        return ("VOLUME trk%d %.2f" % (trk(), norm(parse_u14(p, 2))),
                "Track %d volume" % trk())
    if c is Cmd.PAN:
        return ("PAN trk%d %.2f" % (trk(), norm(parse_u14(p, 2))),
                "Track %d pan" % trk())
    if c is Cmd.FLAG:
        try:
            which = Flag(p[2]).name
        except (ValueError, IndexError):
            which = "?"
        on = len(p) > 3 and p[3]
        return ("FLAG trk%d %s %s" % (trk(), which, "on" if on else "off"),
                "Track %d %s" % (trk(), which.title()))
    if c is Cmd.SELECT:
        return ("SELECT trk%d" % trk(), "Track %d selected" % trk())
    if c is Cmd.VU:
        return ("VU trk%d %.2f" % (trk(), norm(parse_u14(p, 2))),
                "Track %d meter" % trk())
    if c is Cmd.TRANSPORT:
        bits = p[0] if p else 0
        on = ", ".join(n for n, b in (("play", T_PLAY), ("rec", T_RECORD),
                                      ("loop", T_LOOP)) if bits & b) or "stop"
        return ("TRANSPORT %s" % on, "transport")
    if c is Cmd.PAGE:
        return ("PAGE %s" % ("next" if p and p[0] else "prev"), "bank scroll")
    if c is Cmd.DIAG:
        return ("DIAG %s" % bytes(p).decode("ascii", "replace"),
                "script self-report")
    if c is Cmd.PARAM_VALUE:
        return ("PARAM_VALUE slot%d %.2f" % (parse_u14(p), norm(parse_u14(p, 2))),
                "plugin param")
    return (c.name, "")
