"""HUI (Mackie / DigiDesign Human User Interface) wire codec.

Pro Tools' native control-surface protocol. Athens emulates an 8-fader HUI
surface so Pro Tools' MIXER — faders, pan, mute/solo/select/arm, transport,
banking — drives the ROTO. That mixer layer is the only thing Pro Tools exposes
to a surface: HUI carries no plugin parameters and no track colours, and its
meters are coarse mono, so VU is deliberately NOT wired (see protools_source).

Protocol cross-verified against three independent implementations (MIDIKit,
theMartzSound/HUI-MCU-Logger, matthewmx86/mackie-hui-osc). Facts:
  * HUI lives entirely on MIDI channel 0, status 0x90 / 0xA0 / 0xB0.
  * Switches/LEDs use a two-CC (zone, port+state) pair whose CC numbers FLIP by
    direction — DAW->surface (LED) uses zone 0x0C / port 0x2C, surface->DAW
    (press) uses zone 0x0F / port 0x2F. port byte = port | (0x40 if on else 0).
  * Faders are a 14-bit CC MSB/LSB pair, same CCs both ways.
  * V-Pots are RELATIVE surface->DAW (CC 0x40+ch, 0x40 bit = increment) and an
    LED-ring preset index DAW->surface (CC 0x10+ch).
mido delivers complete messages (running status already resolved), so the
decoder is fed one message at a time and latches state across them.
"""
from __future__ import annotations

from typing import List, Tuple

# -- ping / handshake --------------------------------------------------------
PING = b"\x90\x00\x00"          # host -> surface, ~1/s
PING_REPLY = b"\x90\x00\x7f"    # surface -> host, reply to EVERY ping or PT drops us
SYSTEM_RESET = b"\xff"          # surface -> host on connect: "present, (re)flood state"

# -- faders (14-bit MSB/LSB CC pair, per channel 0..7; same CCs both ways) ---
FADER_MSB = 0x00                # + ch
FADER_LSB = 0x20                # + ch

# -- v-pots (pan) ------------------------------------------------------------
VPOT_LED = 0x10                 # + ch : DAW -> surface, LED-ring preset index
VPOT_DELTA = 0x40               # + ch : surface -> DAW, relative encoder

# -- switch zone/port CCs (direction-flipped) --------------------------------
ZONE_LED, PORT_LED = 0x0C, 0x2C     # DAW -> surface (light an LED)
ZONE_PRESS, PORT_PRESS = 0x0F, 0x2F  # surface -> DAW (press a button)
STATE_ON = 0x40                     # high nibble of the port byte: 0x4=on, 0x0=off

# -- per-channel strip ports (zone = channel 0..7) ---------------------------
P_FADER_TOUCH = 0x0
P_SELECT = 0x1
P_MUTE = 0x2
P_SOLO = 0x3
P_REC = 0x7                     # record-ready / arm. (HUI has NO per-strip monitor.)

# flag name (source.TRACK_FLAGS) -> strip port. "monitoring" is absent in HUI.
FLAG_PORT = {"muted": P_MUTE, "soloed": P_SOLO, "armed": P_REC}

# -- transport (zone 0x0E) ---------------------------------------------------
ZONE_TRANSPORT = 0x0E
T_REWIND, T_FFWD, T_STOP, T_PLAY, T_REC = 0x1, 0x2, 0x3, 0x4, 0x5

# -- bank navigation (zone 0x0A) ---------------------------------------------
ZONE_BANK = 0x0A
BANK_LEFT, BANK_RIGHT = 0x1, 0x3

# -- displays (SysEx, DAW -> surface) ----------------------------------------
_SYSEX_BODY = b"\x00\x00\x66\x05\x00"   # after F0: Mackie mfr + sub-ids
DISPLAY_SMALL = 0x10                    # 4-char channel-name strips

NUM_STRIPS = 8


def _clampi(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


# =============================================================================
# encoders (Athens -> Pro Tools). Each returns raw bytes (may be >1 message).
# =============================================================================
def ping_reply() -> bytes:
    return PING_REPLY


def system_reset() -> bytes:
    return SYSTEM_RESET


def fader(ch: int, value01: float) -> bytes:
    """Set channel `ch`'s fader to a normalised 0..1 position (14-bit pair)."""
    v = _clampi(round(value01 * 0x3FFF), 0, 0x3FFF)
    return bytes([0xB0, FADER_MSB + ch, (v >> 7) & 0x7F,
                  0xB0, FADER_LSB + ch, v & 0x7F])


def switch(zone: int, port: int, on: bool) -> bytes:
    """A surface->DAW button press/release: zone-select then port+state."""
    return bytes([0xB0, ZONE_PRESS, zone,
                  0xB0, PORT_PRESS, port | (STATE_ON if on else 0)])


def press(zone: int, port: int) -> bytes:
    """A momentary press: down then up. HUI switches TOGGLE the DAW state, so a
    single press flips mute/solo/select/transport/bank."""
    return switch(zone, port, True) + switch(zone, port, False)


def fader_touch(ch: int, on: bool) -> bytes:
    """Touch/untouch a fader (zone = channel, port 0). Sent while the user drives
    the fader so Pro Tools stops fighting it with motor feedback."""
    return switch(ch, P_FADER_TOUCH, on)


def vpot_delta(ch: int, steps: int) -> bytes:
    """Relative pan turn: +steps = clockwise (0x40 bit set), -steps = ccw.
    Magnitude 1..63. NB Pro Tools' sense is opposite to MCU — flip if wrong."""
    mag = _clampi(abs(steps), 0, 63)
    if mag == 0:
        return b""
    val = (STATE_ON | mag) if steps > 0 else mag
    return bytes([0xB0, VPOT_DELTA + ch, val])


# =============================================================================
# decoder (Pro Tools -> Athens). Stateful: fed one complete message at a time.
# Returns a list of events, each a tuple whose first item names it:
#   ("ping",)                       -> must be answered with ping_reply()
#   ("fader",  ch, value01)
#   ("pan",    ch, value01)         -> coarse (LED-ring, ~11 steps)
#   ("flag",   ch, "muted"|..., on)
#   ("select", ch, on)
#   ("transport", "play"|"stop"|"record", on)
#   ("name",   ch, text)
# =============================================================================
Event = Tuple


class HuiDecoder:
    def __init__(self) -> None:
        self._zone: int | None = None          # latched zone from a 0x0C CC
        self._fader_msb: dict = {}             # ch -> latched fader MSB

    def feed(self, data: bytes) -> List[Event]:
        if not data:
            return []
        status = data[0]
        hi = status & 0xF0
        # ping: note-on note 0 VEL 0 (CoreMIDI may deliver it as a note-off). Our
        # OWN reply is note-on note 0 vel 127 — gate on vel 0 so that, over a
        # shared IAC bus, the echo of our reply is not mistaken for a ping (which
        # would ping-storm ourselves).
        if len(data) >= 2 and data[1] == 0x00 and (
                hi == 0x80 or (hi == 0x90 and (len(data) < 3 or data[2] == 0x00))):
            return [("ping",)]
        if status == 0xF0:
            return self._sysex(data)
        if hi == 0xA0:                          # poly-AT == level meter -> ignore (no VU)
            return []
        if hi != 0xB0 or len(data) < 3:
            return []
        cc, val = data[1], data[2]

        if FADER_MSB <= cc <= FADER_MSB + 7:            # fader high byte
            self._fader_msb[cc - FADER_MSB] = val
            return []
        if FADER_LSB <= cc <= FADER_LSB + 7:            # fader low byte -> emit
            ch = cc - FADER_LSB
            msb = self._fader_msb.get(ch)
            if msb is None:
                return []
            return [("fader", ch, ((msb << 7) | val) / 0x3FFF)]
        if VPOT_LED <= cc <= VPOT_LED + 7:              # pan LED ring (coarse)
            ch = cc - VPOT_LED
            led = val & 0x0F                            # 1..0x0B position, 0=off
            if led == 0:
                return []
            return [("pan", ch, _clampi(led - 1, 0, 10) / 10.0)]  # 1->0, 6->.5, 11->1
        if cc == ZONE_LED:                              # zone select (latch)
            self._zone = val
            return []
        if cc == PORT_LED:                              # port+state -> a switch LED
            zone, self._zone = self._zone, None
            if zone is None:
                return []
            state_nib = val & 0xF0
            if state_nib not in (0x00, STATE_ON):       # 0x2N = PT automation artifact
                return []
            return self._switch(zone, val & 0x0F, state_nib == STATE_ON)
        return []

    @staticmethod
    def _switch(zone: int, port: int, on: bool) -> List[Event]:
        if 0 <= zone <= 7:                              # channel strip
            if port == P_SELECT:
                return [("select", zone, on)]
            if port == P_MUTE:
                return [("flag", zone, "muted", on)]
            if port == P_SOLO:
                return [("flag", zone, "soloed", on)]
            if port == P_REC:
                return [("flag", zone, "armed", on)]
            return []
        if zone == ZONE_TRANSPORT:
            name = {T_PLAY: "play", T_STOP: "stop", T_REC: "record"}.get(port)
            return [("transport", name, on)] if name else []
        return []

    @staticmethod
    def _sysex(data: bytes) -> List[Event]:
        # F0 00 00 66 05 00 <type> <payload...> F7
        if len(data) < 8 or bytes(data[1:6]) != _SYSEX_BODY:
            return []
        if data[6] != DISPLAY_SMALL:
            return []
        payload = data[7:-1] if data[-1] == 0xF7 else data[7:]
        events: List[Event] = []
        i = 0
        while i + 5 <= len(payload):                    # groups of <disp><4 chars>
            disp = payload[i]
            if 0 <= disp <= 7:
                text = bytes(payload[i + 1:i + 5]).decode("ascii", "replace").rstrip()
                events.append(("name", disp, text))
            i += 5
        return events
