"""Bridge: DAW <-> ROTO mapping engine (serial-config path).

Builds one MIDI-mode setup on the device and keeps it in sync with a DawBackend:
  * 8 knobs  -> CC (base_cc + i)          bidirectional (motor follow + turn)
  * buttons  -> CC (button_base_cc + i)   bidirectional (LED follow + press)
Labels/colours go over serial (knob/switch configs); live values go over MIDI.

Concurrency: apply_bank can be invoked from the main thread (start) and from
backend worker threads (debounced refreshes), while knob/button CCs arrive on
the MIDI callback thread. A single lock serialises bank writes; the routing
dicts are swapped atomically (never mutated in place) so the MIDI thread always
sees a consistent map.

Flash care: device configs are stored in flash, so apply_bank diffs each slot's
wire payload against what was last written and opens a config-update session
only when something actually changed. A byte-identical bank refresh costs zero
serial writes.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from ..daw.backend import BUTTONS_TOTAL, KNOBS_PER_PAGE, Bank, DawBackend
from ..protocol import codec
from ..protocol.constants import ControlType, Mode, SwitchHaptic
from ..roto.client import RotoControl

log = logging.getLogger(__name__)

_UNKNOWN = object()   # cache sentinel: device state for this slot is unknown


@dataclass
class BridgeConfig:
    setup_index: int = 0
    setup_name: str = "REAPER"
    channel: int = 1            # MIDI channel for all controls (1-16)
    base_cc: int = 0x10         # knob i -> CC (base_cc + i); 14-bit LSB = +32
    button_base_cc: int = 0x66  # buttons at CC 102+, clear of the knob LSB range
    mode: Mode = Mode.MIDI      # device mode to sit in


class Bridge:
    def __init__(self, roto: RotoControl, daw: DawBackend,
                 config: Optional[BridgeConfig] = None):
        self.roto = roto
        self.daw = daw
        self.cfg = config if config is not None else BridgeConfig()

        # Reject CC overlap up front, including the knobs' potential 14-bit
        # LSB range (MSB cc + 32): overlap silently turns knob turns into
        # button presses.
        knob_ccs = set(range(self.cfg.base_cc, self.cfg.base_cc + KNOBS_PER_PAGE))
        knob_ccs |= {cc + 32 for cc in knob_ccs}
        button_ccs = set(range(self.cfg.button_base_cc,
                               self.cfg.button_base_cc + BUTTONS_TOTAL))
        if knob_ccs & button_ccs:
            raise ValueError(
                "knob CC range (incl. 14-bit LSBs at base_cc+32) overlaps "
                f"button CC range: {sorted(knob_ccs & button_ccs)}")

        self._lock = threading.RLock()     # serialises bank writes
        self._stopped = False
        self._written: dict[tuple, object] = {}   # slot -> last wire payload
        # routing maps: swapped atomically, read lock-free by the MIDI thread
        self._cc_to_param: dict[int, str] = {}
        self._param_to_cc: dict[str, int] = {}
        self._cc_to_switch: dict[int, str] = {}
        self._switch_to_cc: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.daw.on_bank_changed = self.apply_bank
        self.daw.on_param_value = self._on_daw_value
        self.daw.on_switch_state = self._on_daw_switch
        self.roto.on_value = self._on_control_value
        self.daw.start()

        self.roto.set_mode(self.cfg.mode)
        bank = self.daw.current_bank()
        with self._lock:
            self._write_configs(bank)
        # activate the setup BEFORE pushing values, or the CCs land on
        # whatever setup was previously live on the device
        self.roto.select_setup(self.cfg.setup_index)
        with self._lock:
            self._push_values(bank)
        log.info("bridge started: setup %d '%s', channel %d",
                 self.cfg.setup_index, self.cfg.setup_name, self.cfg.channel)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True   # blocks until any in-flight refresh drains
        self.daw.stop()
        self.roto.close()

    # -- DAW -> ROTO -------------------------------------------------------
    def apply_bank(self, bank: Bank) -> None:
        """Sync the device to this bank: write changed configs, push values."""
        with self._lock:
            if self._stopped:
                return
            self._write_configs(bank)
            self._push_values(bank)

    def _write_configs(self, bank: Bank) -> None:
        """Diff each slot's wire payload against the last write and send only
        the changes, inside a single config-update session (or none at all)."""
        knobs = bank.knobs_padded()
        buttons = bank.buttons_padded()
        pending = []   # (cache_key, cache_value, write_fn)
        cc_to_param: dict[int, str] = {}
        cc_to_switch: dict[int, str] = {}

        if self._written.get(("setup-name",), _UNKNOWN) != self.cfg.setup_name:
            pending.append((("setup-name",), self.cfg.setup_name,
                            lambda: self.roto.set_setup_name(
                                self.cfg.setup_index, self.cfg.setup_name)))

        for i in range(KNOBS_PER_PAGE):
            cc = self.cfg.base_cc + i
            key = ("knob", i)
            p = knobs[i]
            if p is None:
                if self._written.get(key, _UNKNOWN) is not None:
                    pending.append((key, None,
                                    lambda i=i: self.roto.clear_control(
                                        self.cfg.setup_index, ControlType.KNOB, i)))
                continue
            kc = codec.KnobConfig(
                setup_index=self.cfg.setup_index, control_index=i,
                channel=self.cfg.channel, param=cc, name=p.name, colour=p.colour)
            payload = kc.payload()
            if self._written.get(key, _UNKNOWN) != payload:
                pending.append((key, payload,
                                lambda kc=kc: self.roto.write_knob_config(kc)))
            cc_to_param[cc] = p.key

        for i in range(BUTTONS_TOTAL):
            cc = self.cfg.button_base_cc + i
            key = ("switch", i)
            b = buttons[i]
            if b is None:
                if self._written.get(key, _UNKNOWN) is not None:
                    pending.append((key, None,
                                    lambda i=i: self.roto.clear_control(
                                        self.cfg.setup_index, ControlType.SWITCH, i)))
                continue
            sc = codec.SwitchConfig(
                setup_index=self.cfg.setup_index, control_index=i,
                channel=self.cfg.channel, param=cc, name=b.name, colour=b.colour,
                led_on=b.colour, led_off=0,
                haptic=SwitchHaptic.PUSH if b.momentary else SwitchHaptic.TOGGLE)
            payload = sc.payload()
            if self._written.get(key, _UNKNOWN) != payload:
                pending.append((key, payload,
                                lambda sc=sc: self.roto.write_switch_config(sc)))
            cc_to_switch[cc] = b.key

        if pending:
            with self.roto.config_update():
                for key, value, write in pending:
                    write()
                    self._written[key] = value

        # atomic swap: the MIDI thread sees the old or the new map, never a
        # half-built one
        self._cc_to_param = cc_to_param
        self._param_to_cc = {k: cc for cc, k in cc_to_param.items()}
        self._cc_to_switch = cc_to_switch
        self._switch_to_cc = {k: cc for cc, k in cc_to_switch.items()}
        log.info("applied bank '%s' (%d knobs, %d buttons, %d config writes)",
                 bank.title, len(cc_to_param), len(cc_to_switch), len(pending))

    def _push_values(self, bank: Bank) -> None:
        """Motors + LEDs to current state (MIDI only, no flash writes)."""
        knobs = bank.knobs_padded()
        buttons = bank.buttons_padded()
        for i in range(KNOBS_PER_PAGE):
            if knobs[i] is not None:
                self.roto.send_value(self.cfg.channel, self.cfg.base_cc + i,
                                     knobs[i].value)
        for i in range(BUTTONS_TOTAL):
            if buttons[i] is not None:
                self.roto.send_value(self.cfg.channel, self.cfg.button_base_cc + i,
                                     1.0 if buttons[i].on else 0.0)

    def _on_daw_value(self, key: str, value: float) -> None:
        if self._stopped:
            return
        cc = self._param_to_cc.get(key)
        if cc is not None:
            self.roto.send_value(self.cfg.channel, cc, value)

    def _on_daw_switch(self, key: str, on: bool) -> None:
        if self._stopped:
            return
        cc = self._switch_to_cc.get(key)
        if cc is not None:
            self.roto.send_value(self.cfg.channel, cc, 1.0 if on else 0.0)

    # -- ROTO -> DAW -------------------------------------------------------
    def _on_control_value(self, channel: int, controller: int, value: float) -> None:
        if self._stopped or channel != self.cfg.channel:
            return
        key = self._cc_to_param.get(controller)
        if key is not None:
            self.daw.set_param(key, value)
            return
        key = self._cc_to_switch.get(controller)
        if key is not None:
            self.daw.set_switch(key, value >= 0.5)
