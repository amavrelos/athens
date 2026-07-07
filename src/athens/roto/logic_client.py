"""RotoLogicClient: the device-facing layer for the Logic dialect (daw_id=3).

The ROTO gates its behaviour on the daw_id it hears in DAW_PING_RESP. As
"Logic Pro" it runs the richer protocol reverse-engineered from
logic/config.lua + wire captures (reference/logic-protocol/):

  * a COMMAND channel — CC on status 0xB6 — that config.lua uses to switch
    Logic's own assignment-table modes (Logic-internal; never on the wire, see
    send_command);
  * plugin param VALUES on their own channel block (0xBE down to 0xB7,
    32 params per channel, 14-bit MSB/LSB + touch CC), param-indexed both
    directions — the device owns the control->param mapping;
  * the sweep-based learn: SET_DEVICE_LEARN / PLUGIN_PARAM_SWEEP /
    PLUGIN_PARAM_SWEEP_VALUE / LEARN_PARAM / PLUGIN_LEARN_COMPLETE — which is
    what maps buttons and full plugins (the Ableton dialect can't).

Everything dialect-shared (PING keepalive, encoder/button/touch/transport CCs
on channel 16, transport/track/plugin-details senders) is inherited from
RotoSysexClient; only the Logic additions live here.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ..sysex import codec
from ..sysex.constants import (
    LOGIC_COMMAND_STATUS, MIX_PAGE_LEFT_CC, MIX_PAGE_RIGHT_CC,
    PLUGIN_CHAN_END_STATUS, PLUGIN_CHAN_START_STATUS,
    PLUGIN_LSB_CC_OFFSET, PLUGIN_PARAMS_PER_CHANNEL, PLUGIN_TOUCH_FIRST_CC,
    TRANSPORT_FIRST_CC, VALUE_MIDI_CHANNEL,
    DawId, General, Group, LogicCommand, Mixer, Plugin, TransportAction,
)
from .sysex_client import MidiPort, RotoSysexClient

# The Logic dialect's transport CC layout (config.lua controls table ~372)
# differs from the Ableton one: loop rides +3 and punch +4 (Ableton has
# session-record at +3, loop at +4, discrete punch in/out at +5/+6).
_TRANSPORT_CC_TO_ACTION = {
    TRANSPORT_FIRST_CC + 0: TransportAction.PLAY,
    TRANSPORT_FIRST_CC + 1: TransportAction.STOP,
    TRANSPORT_FIRST_CC + 2: TransportAction.RECORD,
    TRANSPORT_FIRST_CC + 3: TransportAction.LOOP,
    TRANSPORT_FIRST_CC + 4: TransportAction.PUNCH_IN,   # the 'punch' button
    # the firmware transport grid has three UNLABELED but live buttons in
    # the Logic dialect (logic_transport.mmon: CC 33/34/35). Logic ignores
    # them — free real estate, so we assign them (user-overridable via
    # set_transport_assignments):
    TRANSPORT_FIRST_CC + 5: TransportAction.REWIND,
    TRANSPORT_FIRST_CC + 6: TransportAction.FASTFORWARD,
    # +7 (CC 35): on-screen label UNCONFIRMED — provisionally metronome; verify
    # against a Logic capture before trusting the label.
    TRANSPORT_FIRST_CC + 7: TransportAction.METRONOME,
    # (config.lua also lists Rewind/Forward at +8/+9 = CC 36/37, but the device
    # never emits those, so they're intentionally not mapped here.)
}

# the slots the user may reassign: the labeled 'punch' key + the three
# unlabeled grid buttons (play/stop/record/cycle keep their meaning)
ASSIGNABLE_TRANSPORT_CCS = tuple(TRANSPORT_FIRST_CC + i for i in (4, 5, 6, 7))
_ASSIGNMENT_ACTIONS = {
    "play": TransportAction.PLAY, "stop": TransportAction.STOP,
    "record": TransportAction.RECORD, "loop": TransportAction.LOOP,
    "rewind": TransportAction.REWIND,
    "fastforward": TransportAction.FASTFORWARD,
    "metronome": TransportAction.METRONOME,
}
_TRANSPORT_ACTION_TO_CC = {a: cc for cc, a in _TRANSPORT_CC_TO_ACTION.items()}

log = logging.getLogger(__name__)


class RotoLogicClient(RotoSysexClient):
    def __init__(self, port: MidiPort):
        super().__init__(port, daw_id=DawId.LOGIC_PRO)

        # Logic-dialect events (device -> DAW), all optional
        self.on_plugin_mode_logic: Optional[Callable[[bool], None]] = None  # (smart)
        self.on_learn_mode: Optional[Callable[[int], None]] = None  # LearnMode value
        self.on_sweep_value: Optional[Callable[[codec.SweepValue], None]] = None
        self.on_learn_complete: Optional[Callable[[], None]] = None
        self.on_learn_restart: Optional[Callable[[], None]] = None
        self.on_switch_value_request: Optional[Callable[[int], None]] = None
        self.on_param_value_cc: Optional[Callable[[int, float], None]] = None
        self.on_param_touch: Optional[Callable[[int, bool], None]] = None
        # mixer mode announcements: (knob_mode, button_mode, send_index)
        self.on_mixer_all_mode: Optional[Callable[[int, int, int], None]] = None
        self.on_mixer_focus_mode: Optional[Callable[[], None]] = None
        # overlay announcements (device-side popup screens)
        self.on_track_select_mode: Optional[Callable[[], None]] = None
        self.on_plugin_enable_mode: Optional[Callable[[], None]] = None
        self.on_plugin_select_mode: Optional[Callable[[], None]] = None
        # device discovery ping — Logic answers it by pushing the session
        # init burst (counts + VU), which is what re-initialises the display
        self.on_ping: Optional[Callable[[], None]] = None

        # a raw DAW action id was assigned to a transport button
        self.on_custom_action: Optional[Callable[[int, bool], None]] = None

        # pending param-value MSBs awaiting their LSB, keyed by param index
        self._param_msb: dict[int, int] = {}
        # per-instance transport map so user assignments don't leak between
        # sessions/tests; values: TransportAction | int (raw DAW action id)
        self._transport_map: dict = dict(_TRANSPORT_CC_TO_ACTION)

    def set_transport_assignments(self, mapping: dict) -> None:
        """Reassign the assignable transport buttons. mapping keys are CC
        numbers as strings; values: an action name ('rewind', 'metronome',
        ...), 'action:<id>' for a raw DAW action, or 'none'."""
        for cc_key, val in mapping.items():
            try:
                cc = int(cc_key)
            except (TypeError, ValueError):
                continue
            if cc not in ASSIGNABLE_TRANSPORT_CCS:
                continue
            if isinstance(val, str) and val.startswith("action:"):
                try:
                    self._transport_map[cc] = int(val.split(":", 1)[1])
                except ValueError:
                    self._transport_map.pop(cc, None)
            elif val in _ASSIGNMENT_ACTIONS:
                self._transport_map[cc] = _ASSIGNMENT_ACTIONS[val]
            else:                                   # 'none' / unknown
                self._transport_map.pop(cc, None)

    # -- command channel: NOT wire protocol ------------------------------------
    # The 0xB6 "command channel" in config.lua is Logic-INTERNAL: Logic re-injects
    # these into its own CS engine to switch assignment-table modes; they never
    # reach the device (ground truth: zero 0xB6 bytes on the wire across a full
    # working session). Sending them corrupts the device's knob-binding state
    # (free-spinning mapped knobs) — kept as an explicit no-op so the mistake is
    # never reintroduced.
    def send_command(self, command: int, value: int = 1) -> None:
        log.debug("command-channel send suppressed (Logic-internal): %02x=%d",
                  command, value)

    # -- outbound: plugin param bus ------------------------------------------
    def send_param_value_cc(self, param_index: int, value: float) -> None:
        for msg in codec.logic_param_cc(param_index, value):
            self._port.send(msg)

    def send_param_display(self, param_index: int, text: str) -> None:
        self._port.send(codec.logic_param_value(param_index, text))

    def send_plugin_param_sweep(self, param_index: int) -> None:
        self._port.send(codec.plugin_param_sweep(param_index))

    def send_learn_restart(self) -> None:
        self._port.send(codec.plugin_learn_restart())

    # -- outbound: track / display -------------------------------------------
    def send_focus_track(self, slot: int, name: str, rgb: tuple) -> None:
        self._port.send(codec.daw_select_focus_track(slot, name, rgb))

    def send_current_track_name(self, name: str) -> None:
        self._port.send(codec.set_current_track_name(name))

    def send_current_track_color(self, rgb: tuple) -> None:
        self._port.send(codec.set_current_track_color(rgb))

    def send_track_color(self, slot: int, rgb: tuple) -> None:
        self._port.send(codec.set_track_color(slot, rgb))

    def send_logic_track_details(self, slot: int, name: str, colour: int) -> None:
        self._port.send(codec.logic_track_details(slot, name, colour))

    def send_reset_track_details(self, slot: int) -> None:
        self._port.send(codec.reset_track_details(slot))

    def send_smart_ctl_details(self, ctl_index: int, name: str, colour: int) -> None:
        self._port.send(codec.set_plugin_ctl_details(ctl_index, name, colour))

    def send_transport_led(self, action, on: bool) -> None:
        cc = _TRANSPORT_ACTION_TO_CC.get(action)
        if cc is not None:
            self._port.send(bytes((0xB0 | VALUE_MIDI_CHANNEL, cc,
                                   127 if on else 0)))

    # -- inbound ---------------------------------------------------------------
    def _on_midi(self, data: bytes) -> None:
        # bring-up visibility: inbound frames at DEBUG (-v), minus the noise
        # (realtime/clock bytes and the periodic ping)
        if data and data[0] < 0xF8 and log.isEnabledFor(logging.DEBUG) \
                and not (data[0] == 0xF0 and len(data) >= 7
                         and data[5] == Group.GENERAL
                         and data[6] == General.PING_DAW):
            log.debug("D->H %s", data.hex(" "))
        super()._on_midi(data)

    def _dispatch_sysex(self, msg: codec.SysexMessage) -> None:
        g, c, d = msg.group, msg.command, msg.data
        if g == Group.GENERAL and c == General.PING_DAW:
            # The device pings for DISCOVERY (it goes quiet once a session is
            # healthy). Logic answers each discovery ping by (re)pushing the
            # session INIT BURST — counts + VU — which is what makes the
            # device initialise its display (logic_startup.mmon). We send the
            # daw_id in DAW_PING_RESP (the device gates its dialect on it) AND
            # fire on_ping so the bridge pushes that burst.
            self._port.send(codec.daw_ping_response(self.daw_id))
            self._fire(self.on_ping)
            return
        if g == Group.MIXER:
            if c == Mixer.SET_MIXER_ALL_MODE and len(d) >= 3:
                # data: [bank?, knob_mode(0=vol,1=pan,2=send),
                #        button_mode(0=mute,1=solo,2=arm,3=inputmon), send_idx]
                self._fire(self.on_mixer_all_mode, d[1], d[2],
                           d[3] if len(d) > 3 else 0)
                return
            if c == Mixer.SET_MIXER_SELECTED_MODE:
                self._fire(self.on_mixer_focus_mode)
                return
        if g == Group.PLUGIN:
            if c == Plugin.SET_PLUGIN_MODE:
                # Logic carries the mode in data: 0 = plugin, 1 = smart
                self._fire(self.on_plugin_mode_logic, bool(d and d[0]))
                return
            if c == Plugin.SET_TRACK_SELECT_MODE:
                self._fire(self.on_track_select_mode)
                return
            if c == Plugin.SET_PLUGIN_ENABLE_MODE:
                self._fire(self.on_plugin_enable_mode)
                return
            if c == Plugin.SET_PLUGIN_SELECT_MODE:
                self._fire(self.on_plugin_select_mode)
                return
            if c == Plugin.SET_DEVICE_LEARN and d:
                self._fire(self.on_learn_mode, d[0])
                return
            if c == Plugin.PLUGIN_PARAM_SWEEP_VALUE:
                self._fire(self.on_sweep_value, codec.decode_sweep_value(d))
                return
            if c == Plugin.PLUGIN_LEARN_COMPLETE:
                self._fire(self.on_learn_complete)
                return
            if c == Plugin.PLUGIN_LEARN_RESTART:
                self._fire(self.on_learn_restart)
                return
            if c == Plugin.REQUEST_SWITCH_PARAM_VALUE and len(d) >= 2:
                self._fire(self.on_switch_value_request,
                           codec.decode_switch_param_request(d))
                return
        super()._dispatch_sysex(msg)

    def _dispatch_cc(self, data: bytes) -> None:
        status, cc, val = data[0], data[1], data[2]
        if PLUGIN_CHAN_START_STATUS <= status <= PLUGIN_CHAN_END_STATUS:
            base = ((PLUGIN_CHAN_END_STATUS & 0x0F) - (status & 0x0F)) \
                * PLUGIN_PARAMS_PER_CHANNEL
            if cc < PLUGIN_PARAMS_PER_CHANNEL:
                # value MSB; LSB follows on cc+0x20. A stale pending MSB means
                # its LSB got lost — flush at 7-bit so the move isn't dropped.
                param = base + cc
                if param in self._param_msb:
                    self._fire(self.on_param_value_cc, param,
                               codec.frac7(self._param_msb[param]))
                self._param_msb[param] = val
            elif cc < 2 * PLUGIN_PARAMS_PER_CHANNEL:
                param = base + cc - PLUGIN_LSB_CC_OFFSET
                msb = self._param_msb.pop(param, None)
                if msb is not None:
                    self._fire(self.on_param_value_cc, param,
                               codec.frac14(msb, val))
            elif PLUGIN_TOUCH_FIRST_CC <= cc \
                    < PLUGIN_TOUCH_FIRST_CC + PLUGIN_PARAMS_PER_CHANNEL:
                self._fire(self.on_param_touch,
                           base + cc - PLUGIN_TOUCH_FIRST_CC, val > 0)
            else:
                log.debug("unrouted plugin-channel CC %02x %d=%d", status, cc, val)
            return
        if (status & 0x0F) == VALUE_MIDI_CHANNEL \
                and TRANSPORT_FIRST_CC <= cc <= TRANSPORT_FIRST_CC + 9:
            # the Logic dialect owns the whole transport range — an
            # unassigned button must NOT fall through to the base class,
            # which would derive Ableton semantics from the CC offset
            target = self._transport_map.get(cc)
            if isinstance(target, TransportAction):
                self._fire(self.on_transport_button, target, val > 0)
            elif isinstance(target, int):      # raw DAW action id
                self._fire(self.on_custom_action, target, val > 0)
            return
        if (status & 0x0F) == VALUE_MIDI_CHANNEL \
                and cc in (MIX_PAGE_LEFT_CC, MIX_PAGE_RIGHT_CC):
            # Logic dialect pages the bank with these arrow CCs (a press sends
            # value>0, no release) — one window per press
            if val > 0:
                self._fire(self.on_page, -1 if cc == MIX_PAGE_LEFT_CC else +1)
            return
        super()._dispatch_cc(data)
