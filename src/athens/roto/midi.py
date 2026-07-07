"""USB-MIDI real-time value channel.

The serial API only *configures* the knobs (mapping, label, colour, haptics).
Live values in both directions travel as MIDI on the device's USB-MIDI port:
turning a knob emits CC/NRPN; sending CC/NRPN back drives the motor and the
on-knob value read-out. This module wraps that: normalised 0.0-1.0 values in,
CC/NRPN on the wire, and a callback for incoming knob movement.

`mido` + `python-rtmidi` are optional (extra `midi`); everything else in the
package works without them.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

# incoming: (channel 1-16, controller/nrpn-address, value 0.0-1.0)
ValueHandler = Callable[[int, int, float], None]

ROTO_PORT_HINT = "ROTO"   # substring used to auto-pick the device's MIDI ports


def list_ports() -> tuple[list[str], list[str]]:
    import mido
    return mido.get_input_names(), mido.get_output_names()


class MidiIO:
    def __init__(self, in_name: Optional[str] = None, out_name: Optional[str] = None,
                 cc14_msb: Optional[set] = None):
        import mido  # lazy: keep the package importable without the midi extra

        self._mido = mido
        self.on_value: Optional[ValueHandler] = None
        self._in = mido.open_input(in_name or self._pick(mido.get_input_names()),
                                   callback=self._on_message)
        self._out = mido.open_output(out_name or self._pick(mido.get_output_names()))
        # per-channel NRPN reassembly state: channel -> {"addr","msb","vmsb"}
        self._nrpn: dict[int, dict] = {}
        # inbound 14-bit CC pairing is opt-in per MSB controller (< 32): a CC
        # in 32..63 is only treated as an LSB when its base CC is listed here,
        # so unrelated controls in that range are never swallowed
        self._cc14_msb: set = cc14_msb or set()
        self._cc_msb: dict[tuple, int] = {}   # (channel, msb_cc) -> last MSB

    @staticmethod
    def _pick(names: list[str]) -> str:
        for n in names:
            if ROTO_PORT_HINT.lower() in n.lower():
                return n
        if not names:
            raise RuntimeError("no MIDI ports found")
        return names[0]

    # -- outbound ----------------------------------------------------------
    def send_cc(self, channel: int, controller: int, value: float,
                bits: int = 7) -> None:
        if bits == 14:
            raw = int(round(value * 0x3FFF))
            self._send(self._mido.Message("control_change", channel=channel - 1,
                                          control=controller, value=(raw >> 7) & 0x7F))
            self._send(self._mido.Message("control_change", channel=channel - 1,
                                          control=controller + 32, value=raw & 0x7F))
        else:
            raw = int(round(value * 0x7F))
            self._send(self._mido.Message("control_change", channel=channel - 1,
                                          control=controller, value=raw))

    def send_nrpn(self, channel: int, address: int, value: float,
                  bits: int = 7) -> None:
        ch = channel - 1
        raw = int(round(value * (0x3FFF if bits == 14 else 0x7F)))
        m = self._mido.Message
        self._send(m("control_change", channel=ch, control=99, value=(address >> 7) & 0x7F))
        self._send(m("control_change", channel=ch, control=98, value=address & 0x7F))
        if bits == 14:
            self._send(m("control_change", channel=ch, control=6, value=(raw >> 7) & 0x7F))
            self._send(m("control_change", channel=ch, control=38, value=raw & 0x7F))
        else:
            self._send(m("control_change", channel=ch, control=6, value=raw & 0x7F))

    def _send(self, msg) -> None:
        log.debug("MIDI -> %s", msg)
        self._out.send(msg)

    # -- inbound -----------------------------------------------------------
    def _on_message(self, msg) -> None:
        if msg.type != "control_change" or self.on_value is None:
            return
        ch = msg.channel + 1
        c, v = msg.control, msg.value
        st = self._nrpn.setdefault(ch, {})
        if c == 99:                              # NRPN address MSB
            st["msb"] = v
        elif c == 98:                            # NRPN address LSB -> select
            st["addr"] = (st.pop("msb", 0) << 7) | v
            st.pop("vmsb", None)
        elif c in (101, 100):                    # RPN select cancels NRPN context
            st.pop("addr", None)
            st.pop("vmsb", None)
        elif c == 6 and "addr" in st:            # data entry MSB
            st["vmsb"] = v
            self.on_value(ch, st["addr"], v / 0x7F)
        elif c == 38 and "addr" in st:           # data entry LSB (14-bit NRPN)
            if "vmsb" in st:
                self.on_value(ch, st["addr"],
                              ((st["vmsb"] << 7) | v) / 0x3FFF)
        elif c in self._cc14_msb:                # 14-bit CC MSB: coarse value now
            self._cc_msb[(ch, c)] = v
            self.on_value(ch, c, v / 0x7F)
        elif 32 <= c < 64 and (c - 32) in self._cc14_msb \
                and (ch, c - 32) in self._cc_msb:  # 14-bit CC LSB: refine
            self.on_value(ch, c - 32,
                          ((self._cc_msb[(ch, c - 32)] << 7) | v) / 0x3FFF)
        else:
            self.on_value(ch, c, v / 0x7F)

    def close(self) -> None:
        for p in (self._in, self._out):
            try:
                p.close()
            except Exception:  # pragma: no cover
                pass
