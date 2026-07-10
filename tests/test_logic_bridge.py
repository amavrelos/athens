"""Logic-dialect (daw_id=3) client + bridge tests — offline, loopback port.

Wire-format assertions are checked against the real captures in
reference/logic-protocol/ (logic_learn_decoded.txt): the sweep-value decode
uses the exact bytes the device sent, and the continuous-param learn asserts
the capture's message choreography (6 sweeps pulled, then LEARN_PARAM).
"""
from __future__ import annotations

import pytest

from athens.bridge.logic_bridge import LogicBridge, _Sweep
from athens.daw.source import MockSysexSource, PluginParam
from athens.roto.logic_client import RotoLogicClient
from athens.roto.sysex_client import LoopbackMidiPort
from athens.sysex import codec
from athens.sysex.constants import (
    LOGIC_COMMAND_STATUS, General, Group, LearnMode, LogicCommand, Mixer,
    Plugin,
)


def make() -> tuple:
    port = LoopbackMidiPort()
    client = RotoLogicClient(port)
    source = MockSysexSource()
    bridge = LogicBridge(client, source, synchronous=True)
    bridge.start()
    port.sent.clear()
    return port, client, source, bridge


def sysex_sent(port, group, command):
    """All sent frames matching (group, command), parsed."""
    out = []
    for f in port.sent:
        if f and f[0] == 0xF0:
            m = codec.parse_sysex(f)
            if m.group == group and m.command == command:
                out.append(m)
    return out


def inject(port, group, command, data=b""):
    port.inject(codec.build_sysex(group, command, data))


def sweep_value_frame(param: int, step: int) -> bytes:
    """Device's PLUGIN_PARAM_SWEEP_VALUE for a param, as on the wire."""
    chan_off = 0x0E - param // 32
    msb_cc = param % 32
    return codec.build_sysex(Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP_VALUE,
                             bytes((chan_off, msb_cc + 0x20, msb_cc,
                                    step, step)))


# --- codec ------------------------------------------------------------------

def test_sweep_value_decode_matches_capture():
    # capture: PLUGIN_PARAM_SWEEP_VALUE data '0d 2c 0c 00 00' names Mariana
    # param 44 (channel 0xBD, MSB CC 12)
    sv = codec.decode_sweep_value(bytes((0x0D, 0x2C, 0x0C, 0x00, 0x00)))
    assert sv.param_index == 44
    assert sv.step == 0
    assert sv.value == 0.0
    sv = codec.decode_sweep_value(bytes((0x0D, 0x2C, 0x0C, 0x7F, 0x7F)))
    assert sv.step == 0x7F
    assert sv.value == pytest.approx(1.0)


def test_param_cc_addressing():
    # param 44 -> channel 0xBD, MSB CC 12, LSB CC 44 (capture addressing)
    msgs = codec.logic_param_cc(44, 1.0)
    assert msgs == [bytes((0xBD, 12, 0x7F)), bytes((0xBD, 44, 0x7F))]
    # param 0 -> channel 0xBE; param 255 -> channel 0xB7
    assert codec.logic_param_cc(0, 0.0)[0][0] == 0xBE
    assert codec.logic_param_cc(255, 0.0)[0][0] == 0xB7


def test_focus_track_matches_capture():
    # capture: MIX/0x0a data '00 06 "Inst 34" 00.. 00 1a 01 23 00 31'
    # = slot 6, name, RGB(26, 163, 49)
    frame = codec.daw_select_focus_track(6, "Inst 34", (26, 163, 49))
    m = codec.parse_sysex(frame)
    assert (m.group, m.command) == (Group.MIXER, Mixer.DAW_SELECT_FOCUS_TRACK)
    assert m.data == bytes((0, 6)) + codec.encode_string("Inst 34") \
        + bytes((0x00, 0x1A, 0x01, 0x23, 0x00, 0x31))


def test_current_track_color_matches_capture():
    # capture: GEN/0x17 data '00 1a 01 23 00 31'
    m = codec.parse_sysex(codec.set_current_track_color((26, 163, 49)))
    assert (m.group, m.command) == (Group.GENERAL,
                                    General.SET_CURRENT_TRACK_COLOR)
    assert m.data == bytes((0x00, 0x1A, 0x01, 0x23, 0x00, 0x31))


def test_plugin_param_sweep_index_encoding():
    m = codec.parse_sysex(codec.plugin_param_sweep(44))
    assert m.data == bytes((0x00, 0x2C))          # capture: '00 2c'
    m = codec.parse_sysex(codec.plugin_param_sweep(200))
    assert m.data == bytes((0x01, 200 - 128))
    with pytest.raises(ValueError):
        codec.plugin_param_sweep(256)


def test_logic_param_value_uses_param_index():
    m = codec.parse_sysex(codec.logic_param_value(150, "9.86"))
    assert m.data[:2] == bytes((0x01, 150 - 128))
    assert codec.decode_string(m.data[2:]) == "9.86"


# --- client ------------------------------------------------------------------

def test_ping_answered_with_logic_daw_id_and_init_burst():
    # the device gates its dialect on the daw_id in DAW_PING_RESP, AND Logic
    # answers each discovery ping with the session init burst (counts + VU) —
    # logic_startup.mmon: that early push is what re-initialises the display
    port, client, _, _ = make()
    inject(port, Group.GENERAL, General.PING_DAW)
    resps = sysex_sent(port, Group.GENERAL, General.DAW_PING_RESP)
    assert resps and resps[-1].data == bytes((3,))          # daw_id = 3
    assert sysex_sent(port, Group.GENERAL, General.NUM_TRACKS)
    assert sysex_sent(port, Group.GENERAL, General.FIRST_TRACK)
    assert sysex_sent(port, Group.PLUGIN, Plugin.NUM_DEVICES)


def test_param_cc_in_14bit_pairing():
    port, client, _, bridge = make()
    seen = []
    bridge.client.on_param_value_cc = lambda p, v: seen.append((p, v))
    port.inject(bytes((0xBD, 12, 0x40)))          # param 44 MSB
    port.inject(bytes((0xBD, 44, 0x00)))          # param 44 LSB
    assert seen == [(44, pytest.approx((0x40 << 7) / 0x3FFF))]


def test_param_touch_cc_in():
    port, client, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    port.sent.clear()
    source.simulate_device_param(0, 1, 0.5, "0.0 dB")
    port.inject(bytes((0xBE, 0x40 + 1, 0x7F)))    # touch param 1
    vals = sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)
    assert vals and codec.decode_string(vals[-1].data[2:]) == "0.0 dB"


def test_no_command_channel_traffic_ever():
    """The 0xB6 'command channel' is Logic-INTERNAL (config.lua re-injects
    those CCs into Logic's own CS engine); ground truth logic_good.mmon shows
    ZERO 0xB6 bytes on the wire. Sending them corrupted the device's knob
    bindings (free-spinning mapped knobs)."""
    port, _, source, _ = make()
    inject(port, Group.GENERAL, General.PING_DAW)
    inject(port, Group.GENERAL, General.ROTO_DAW_CONNECTED)
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    inject(port, Group.MIXER, Mixer.SET_MIXER_SELECTED_MODE)
    inject(port, Group.GENERAL, General.REQUEST_TRANSPORT_STATUS)
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(3))
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_ENABLE_MODE)
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_SELECT_MODE)
    inject(port, Group.PLUGIN, Plugin.ROTO_CONTROL_SELECT_DEVICE, bytes((1,)))
    assert not [f for f in port.sent if f[0] == LOGIC_COMMAND_STATUS]


def test_mixer_all_mode_pushes_state():
    port, _, _, _ = make()
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    assert sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS)


def test_logic_transport_cc_layout():
    # Logic puts loop on +3 and punch on +4 (Ableton: session-rec/loop there)
    port, _, source, _ = make()
    port.inject(bytes((0xBF, 28 + 3, 127)))       # cycle button
    assert source.transport().loop is True
    leds = [f for f in port.sent if f[0] == 0xBF and f[1] == 31]
    assert bytes((0xBF, 31, 127)) in leds         # loop LED on Logic's CC
    port.inject(bytes((0xBF, 28 + 4, 127)))       # punch: no REAPER binding
    assert source.transport().loop is True        # unchanged, no crash


def test_transport_request_pushes_status():
    port, _, _, _ = make()
    inject(port, Group.GENERAL, General.REQUEST_TRANSPORT_STATUS)
    assert sysex_sent(port, Group.GENERAL, General.TRANSPORT_STATUS)


def test_select_track_selected():
    port, _, source, _ = make()
    fired = []
    # chain like the service does — device-initiated selection must reach
    # the UI event bus even though the source's own state already updated
    prev = source.on_selected_track_changed
    source.on_selected_track_changed = lambda: (prev and prev(), fired.append(1))
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(3))
    assert source.selected_track() == 3
    assert fired


def test_mix_touch_select_suppressed_by_default():
    # in mix-ALL the device fires SELECT_TRACK on every knob TOUCH; by default
    # that must NOT move the DAW selection (only the volume knob should move)
    port, _, source, bridge = make()
    source.set_selected_track(1)
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(3))
    assert source.selected_track() == 1        # unchanged — touch-select gated


def test_mix_touch_select_honored_when_opted_in():
    port, _, source, bridge = make()
    bridge.mix_touch_select = True             # the Settings opt-in
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(3))
    assert source.selected_track() == 3        # now honoured


def test_track_select_pick_always_honored():
    # a deliberate pick in track-select mode changes selection even with mix
    # touch-select off — it arrives with surface == "track_select", not "mix"
    port, _, source, bridge = make()
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    inject(port, Group.PLUGIN, Plugin.SET_TRACK_SELECT_MODE)
    port.sent.clear()
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(3))
    assert source.selected_track() == 3
    # non-mix surface still reflects via the focus-track trio
    assert sysex_sent(port, Group.MIXER, Mixer.DAW_SELECT_FOCUS_TRACK)


def test_mix_touch_select_streams_readout_not_focus_track():
    # opted-in: touching a mix strip streams the strip's readout
    # (GEN/PARAM_VALUES) and keeps the device on the mix screen — never a
    # focus-track, which would flip its display context (logic-touch.mmon)
    port, _, source, bridge = make()
    bridge.mix_touch_select = True
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    port.sent.clear()
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(2))
    assert source.selected_track() == 2
    assert sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)      # readout
    assert not sysex_sent(port, Group.MIXER, Mixer.DAW_SELECT_FOCUS_TRACK)


def _touch(port, knob, pressed):
    from athens.sysex.constants import TOUCH_FIRST_CC, VALUE_MIDI_CHANNEL
    port.inject(bytes((0xB0 | VALUE_MIDI_CHANNEL, TOUCH_FIRST_CC + knob,
                       127 if pressed else 0)))


def test_mix_touch_select_rejects_crosstalk_ghost():
    # hardware crosstalk (athens.log): while knob 1 is held, a fuller finger
    # trips knob 2's pad and the device fires a 2nd touch + select. The ghost
    # (begins while knob 1 is still held) must be dropped; first touch wins.
    port, _, source, bridge = make()
    bridge.mix_touch_select = True
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    _touch(port, 1, True)                                             # knob 1 down
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(1))   # real select
    _touch(port, 2, True)                                # knob 2 down (1 still held)
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(2))   # GHOST select
    assert source.selected_track() == 1        # ghost dropped, first-touched won
    # and it still works once the finger lifts: a fresh, isolated touch lands
    _touch(port, 2, False)
    _touch(port, 1, False)
    _touch(port, 3, True)
    inject(port, Group.GENERAL, General.SELECT_TRACK, codec.u14(3))
    assert source.selected_track() == 3


def test_touch_ghost_swallowed_from_on_touch_stream():
    # the ghost must not reach the app's surface highlight either: on_touch
    # fires only for the real knob, never the swallowed neighbour
    port = LoopbackMidiPort()
    client = RotoLogicClient(port)
    seen = []
    client.on_touch = lambda knob, pressed: seen.append((knob, pressed))
    _touch(port, 1, True)      # real knob 1 down
    _touch(port, 2, True)      # ghost knob 2 down (1 held) -> swallowed
    _touch(port, 2, False)     # ghost knob 2 up            -> swallowed
    _touch(port, 1, False)     # real knob 1 up
    assert seen == [(1, True), (1, False)]


def test_mix_mode_reactivates_after_plugin():
    # plugin mode then back to mix: knob writes volumes again
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    port.inject(bytes((0xBF, 12, 0x20)))
    port.inject(bytes((0xBF, 44, 0x00)))
    assert source.tracks()[0].volume == pytest.approx((0x20 << 7) / 0x3FFF)


def test_mixer_push_order_and_no_focus_message():
    # strip push: colours+volumes, then the ABLETON-style name flow (the one
    # the device actually renders in the Logic dialect — hw-proven), then
    # LEDs; no DAW_SELECT_FOCUS_TRACK (it flips the device's display context)
    port, _, _, _ = make()
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    assert not sysex_sent(port, Group.MIXER, Mixer.DAW_SELECT_FOCUS_TRACK)
    assert not sysex_sent(port, Group.GENERAL, General.SET_TRACK_DETAILS)
    assert sysex_sent(port, Group.GENERAL, General.NUM_TRACKS)
    leds = [f for f in port.sent if f[0] == 0xBF and 20 <= f[1] < 28]
    assert len(leds) == 8 and all(f[2] == 127 for f in leds)   # none muted
    names = sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS)
    assert len(names) == 8
    assert codec.decode_string(names[0].data[2:15]) == "Kick"
    assert sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS_END)


def test_short_projects_blank_the_unused_strips():
    port = LoopbackMidiPort()
    source = MockSysexSource()
    source._tracks = source._tracks[:3]           # 3-track project
    bridge = LogicBridge(RotoLogicClient(port), source, synchronous=True)
    bridge.start()
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    names = sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS)
    assert len(names) == 3
    resets = sysex_sent(port, Group.GENERAL, General.RESET_TRACK_DETAILS)
    assert [m.data[1] for m in resets] == [3, 4, 5, 6, 7]


def test_reaper_source_clamps_to_feed_track_count():
    # REAPER's OSC pads the bank with "Track N" feedback for slots beyond the
    # project; the ReaScript's live.json "tracks" count is the ground truth
    from athens.daw.reaper_source import ReaperSysexSource
    src = ReaperSysexSource(enable_fx_feed=False)
    for i in range(64):
        src._on_name(f"/track/{i + 1}/name", f"Track {i + 1}")
        src._on_volume(f"/track/{i + 1}/volume", 0.7)
    assert len(src.tracks()) == 64                # no count known yet
    src._on_feed_track_count(3)
    tracks = src.tracks()
    assert len(tracks) == 3
    assert [t.name for t in tracks] == ["Track 1", "Track 2", "Track 3"]


def test_mute_button_toggles_and_lights():
    port, _, source, _ = make()
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    port.sent.clear()
    port.inject(bytes((0xBF, 20, 127)))           # button 0 press
    port.inject(bytes((0xBF, 20, 0)))             # release (ignored)
    assert source.tracks()[0].muted is True
    assert bytes((0xBF, 20, 0)) in port.sent      # LED off = muted
    # DAW-side unmute lights it again
    port.sent.clear()
    source.simulate_flag(0, "muted", False)
    assert bytes((0xBF, 20, 127)) in port.sent


def test_solo_bank_buttons_and_leds():
    port, _, source, _ = make()
    # buttons=1 -> solo bank; solo LEDs light when ACTIVE (inverse of mute)
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 1, 0)))
    port.sent.clear()
    port.inject(bytes((0xBF, 21, 127)))           # button 1 press
    assert source.tracks()[1].soloed is True
    assert bytes((0xBF, 21, 127)) in port.sent    # LED on = soloed
    # a mute change while the solo bank is up must NOT touch LEDs
    port.sent.clear()
    source.simulate_flag(2, "muted", True)
    assert not [f for f in port.sent if f[0] == 0xBF and 20 <= f[1] < 28]


def test_pan_and_send_knob_banks():
    port, _, source, _ = make()
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 1, 0, 0)))
    port.inject(bytes((0xBF, 12, 0x7F)))          # knob 0 in pan bank
    port.inject(bytes((0xBF, 44, 0x7F)))
    assert source.tracks()[0].pan == pytest.approx(1.0)
    assert source.tracks()[0].volume == pytest.approx(0.75)   # untouched
    # send bank: knob writes send 2 of the track
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 2, 0, 2)))
    port.inject(bytes((0xBF, 13, 0x40)))
    port.inject(bytes((0xBF, 45, 0x00)))
    assert source.track_send(1, 2) == pytest.approx((0x40 << 7) / 0x3FFF)


# --- bridge: handshake + populate ---------------------------------------------

def test_connected_acks_global_and_pushes_mixer():
    port, _, _, _ = make()
    inject(port, Group.GENERAL, General.ROTO_DAW_CONNECTED)
    assert sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS)
    assert sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS_END)
    assert sysex_sent(port, Group.GENERAL, General.SET_TRACK_COLOR)
    assert sysex_sent(port, Group.GENERAL, General.TRANSPORT_STATUS)
    # Logic's connect prelude zeroes the surface (LEDs + meters)
    assert bytes((0xBF, 20, 0)) in port.sent
    assert bytes((0xBF, 65, 0)) in port.sent
    # focus-track is selection-event-driven, never part of the strip push
    assert not sysex_sent(port, Group.MIXER, Mixer.DAW_SELECT_FOCUS_TRACK)


def test_plugin_mode_refloods_on_every_announce():
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    details = sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_DETAILS)
    assert len(details) == 1
    assert codec.decode_string(details[0].data[10:23]) == "EQ Eight"
    assert details[0].data[-2] == 0               # rack_kind 0, as Logic sends
    # all 4 mock params pushed as value CC pairs on channel 0xBE
    ccs = [f for f in port.sent if f[0] == 0xBE]
    assert len(ccs) == 8
    # repeated announcements RE-FLOOD, exactly as Logic does: the device
    # announces the screen ~100ms before it can take value CCs, so the flood
    # gated to entering-only landed in the void and knobs kept the previous
    # screen's positions (HW-confirmed on REAPER and Cubase)
    port.sent.clear()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    assert sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_DETAILS)
    assert len([f for f in port.sent if f[0] == 0xBE]) == 8
    # RE-ENTERING plugin mode from mix also re-floods: the device drops
    # its param values on screen changes; without the flood mapped knobs have
    # no position anchor (capture-diffed vs Logic — the free-spin root cause)
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    port.sent.clear()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    assert len([f for f in port.sent if f[0] == 0xBE]) == 8
    # watch covers every param of the selected device
    assert source.watched == [(0, 0), (0, 1), (0, 2), (0, 3)]


def test_smart_mode_pushes_names_and_values():
    port, _, _, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x01")
    names = sysex_sent(port, Group.PLUGIN, Plugin.SET_PLUGIN_CTL_DETAILS)
    assert codec.decode_string(names[0].data[2:15]) == "Freq A"
    assert sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)


# --- bridge: sweep learn ---------------------------------------------------------

class SweepSource(MockSysexSource):
    """Mock whose param 1 read-back follows set_device_param immediately,
    with a configurable display formatter (continuous vs stepped)."""

    def __init__(self, formatter):
        super().__init__()
        self._fmt = formatter

    def set_device_param(self, device_index, param_index, value):
        super().set_device_param(device_index, param_index, value)
        params = self._params.get(device_index, [])
        if 0 <= param_index < len(params):
            params[param_index].display = self._fmt(value)


def run_learn(source, port, param=1):
    """Arm learn + move param; device then drives the standard ramp until the
    bridge stops pulling. Returns the number of sweep pulls sent."""
    inject(port, Group.PLUGIN, Plugin.SET_DEVICE_LEARN,
           bytes((int(LearnMode.ENABLED),)))
    port.sent.clear()
    source.simulate_param_touch(0, param)
    assert sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP)
    pulls = 0
    # device ramp: sync 0, 7f, then 0 and +2 steps, terminated by 0x7f
    step_iter = iter([0x00, 0x7F] + list(range(0x00, 0x80, 2)) + [0x7F])
    pending = 3                       # device sends 0, 7f, 0 unprompted
    for step in step_iter:
        if pending == 0:
            n = len(sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP))
            port.inject(sweep_value_frame(param, step))
            if len(sysex_sent(port, Group.PLUGIN,
                              Plugin.PLUGIN_PARAM_SWEEP)) == n:
                break                 # bridge stopped pulling (learn sent)
            pulls += 1
        else:
            pending -= 1
            port.inject(sweep_value_frame(param, step))
    return pulls


def test_continuous_param_learn_matches_capture():
    # capture choreography: values 0,7f,0 then +2 ramp; the DAW pulls 5 times
    # after sync and sends LEARN_PARAM on the 6th ramp value (0x0a)
    source = SweepSource(lambda v: f"{v * 100:.1f}%")
    port = LoopbackMidiPort()
    bridge = LogicBridge(RotoLogicClient(port), source, synchronous=True)
    bridge.start()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")

    run_learn(source, port)
    learns = sysex_sent(port, Group.PLUGIN, Plugin.LEARN_PARAM)
    assert len(learns) == 1
    data = learns[0].data
    assert codec.parse_u14(data) == 1
    assert data[2:8] == codec.param_hash("Gain A")
    assert data[10] == 0                          # quantised_steps: continuous
    assert codec.decode_string(data[13:26]) == "Gain A"
    # capture-exact choreography: 6 PLUGIN_PARAM_SWEEPs in total — the move
    # trigger, then pulls for ramp 0x00(running) 02 04 06 08; LEARN_PARAM
    # replaces the pull on the 6th ramp value (0x0a)
    pulls = sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP)
    assert len(pulls) == 6

    # completion restores the pre-sweep value both sides
    before = port.sent[:]
    inject(port, Group.PLUGIN, Plugin.PLUGIN_LEARN_COMPLETE)
    assert source.device_params(0)[1].value == pytest.approx(0.5)
    restore = [f for f in port.sent[len(before):] if f[0] == 0xBE]
    assert len(restore) == 2
    # device then drops learn; bridge disarms cleanly
    inject(port, Group.PLUGIN, Plugin.SET_DEVICE_LEARN, b"\x00")
    assert source.watched == [(0, 0), (0, 1), (0, 2), (0, 3)]


def test_quantised_param_learn_counts_steps():
    # 4-position switch: display flips every 32 ramp steps -> deltas > 4 keep
    # the small-step counter reset, the ramp runs to 0x7f, LEARN_PARAM carries
    # the distinct-step count + zero placeholder strings
    source = SweepSource(lambda v: f"pos {min(3, int(v * 4))}")
    port = LoopbackMidiPort()
    bridge = LogicBridge(RotoLogicClient(port), source, synchronous=True)
    bridge.start()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")

    run_learn(source, port)
    learns = sysex_sent(port, Group.PLUGIN, Plugin.LEARN_PARAM)
    assert len(learns) == 1
    data = learns[0].data
    steps = data[10]
    assert steps == 4                             # pos 0..3 + wrap to pos 0
    # placeholder step strings ride after the name: 13 zero bytes each
    # (logic_failed.mmon: qsteps=5 -> exactly 65 tail zeros)
    assert data[26:] == b"\x00" * (13 * steps)


def test_learn_needs_arm_and_selected_device():
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    port.sent.clear()
    source.simulate_param_touch(0, 1)             # learn not armed
    assert not sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP)
    inject(port, Group.PLUGIN, Plugin.SET_DEVICE_LEARN, b"\x01")
    port.sent.clear()
    source.simulate_param_touch(1, 0)             # other device
    assert not sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP)


def test_switch_value_request_answered():
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    source.simulate_device_param(0, 2, 1.0, "On")
    port.sent.clear()
    inject(port, Group.PLUGIN, Plugin.REQUEST_SWITCH_PARAM_VALUE,
           bytes((0x00, 0x02)))
    vals = sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)
    assert vals and codec.parse_u14(vals[0].data) == 2
    assert codec.decode_string(vals[0].data[2:]) == "On"


# --- night batch: meters, overlays, focus strip -------------------------------

def test_vu_meters_forwarded_in_mix_mode():
    port, _, source, _ = make()
    source.on_track_vu(1, 0.5, 1.0)               # bridge-wired callback
    meters = [f for f in port.sent if f[0] == 0xBF and 65 <= f[1] < 81]
    assert bytes((0xBF, 65 + 2, 64)) in meters    # strip 1 L
    assert bytes((0xBF, 65 + 3, 127)) in meters   # strip 1 R
    # plugin mode suppresses meter traffic
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    port.sent.clear()
    source.on_track_vu(1, 0.5, 1.0)
    assert not [f for f in port.sent if f[0] == 0xBF and 65 <= f[1] < 81]


def test_plugin_enable_overlay_toggles_fx():
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_ENABLE_MODE)
    details = sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_DETAILS)
    assert len(details) == 16                     # two pages (overlay.mmon)
    assert codec.decode_string(details[3].data[10:23]) == ""
    # empty slots carry the all-zero no-plugin marker with en=1
    assert details[3].data[1:9] == bytes(8) and details[3].data[9] == 1
    port.sent.clear()
    port.inject(bytes((0xBF, 20 + 1, 127)))       # button 2 -> toggle FX 1
    assert source.devices()[1].enabled is False
    assert bytes((0xBF, 21, 0)) in port.sent      # LED follows


def test_plugin_select_overlay_focuses_fx():
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_SELECT_MODE)
    assert sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_DETAILS)
    port.sent.clear()
    inject(port, Group.PLUGIN, Plugin.ROTO_CONTROL_SELECT_DEVICE, bytes((2,)))
    assert source.selected_device() == 2


def test_focus_strip_layout():
    # choreography per logic-focus.mmon: 0x11 labels for used slots, 0x12
    # resets for the rest, NO 0x7 flow / END, focus track + value readouts
    port, _, source, _ = make()
    source.set_selected_track(1)
    source.set_track_send(1, 0, 0.9)
    inject(port, Group.MIXER, Mixer.SET_MIXER_SELECTED_MODE)
    names = sysex_sent(port, Group.GENERAL, General.SET_TRACK_DETAILS)
    labels = [codec.decode_string(m.data[2:15]) for m in names]
    assert labels[:3] == ["Volume", "Pan", "Send 1"]
    resets = sysex_sent(port, Group.GENERAL, General.RESET_TRACK_DETAILS)
    assert len(resets) == 8 - len(labels)          # unused slots blanked
    assert not sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS)
    assert not sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS_END)
    assert sysex_sent(port, Group.MIXER, Mixer.DAW_SELECT_FOCUS_TRACK)
    readouts = sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)
    assert len(readouts) == len(labels)            # one per used slot
    # knob 0 in focus mode drives the SELECTED track's volume
    port.inject(bytes((0xBF, 12, 0x7F)))
    port.inject(bytes((0xBF, 44, 0x7F)))
    assert source.tracks()[1].volume == pytest.approx(1.0)
    # knob 2 drives send 1
    port.inject(bytes((0xBF, 14, 0x00)))
    port.inject(bytes((0xBF, 46, 0x00)))
    assert source.track_send(1, 0) == pytest.approx(0.0)


def test_learn_prefers_daw_step_count():
    source = SweepSource(lambda v: f"{v * 100:.1f}%")   # display looks continuous
    source._params[0][1].steps = 4                # ...but REAPER says 4 steps
    port = LoopbackMidiPort()
    bridge = LogicBridge(RotoLogicClient(port), source, synchronous=True)
    bridge.start()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    run_learn(source, port)
    learns = sysex_sent(port, Group.PLUGIN, Plugin.LEARN_PARAM)
    assert learns[0].data[10] == 4                # authoritative step count
    # 13-byte zero placeholder per step (logic_failed.mmon ground truth)
    assert learns[0].data[26:] == b"\x00" * (13 * 4)


def test_punch_button_fires_action():
    from athens.daw.reaper_source import ReaperSysexSource
    from athens.sysex.constants import TransportAction
    sent = []

    class FakeOsc:
        def send_message(self, addr, val):
            sent.append((addr, val))

    src = ReaperSysexSource(enable_fx_feed=False)
    src._client = FakeOsc()
    src.transport_action(TransportAction.PUNCH_IN, True)
    src.transport_action(TransportAction.PUNCH_IN, False)   # release: nothing
    assert sent == [("/action", ReaperSysexSource.PUNCH_ACTION_ID)]


# --- bridge: param traffic --------------------------------------------------------

def test_device_param_cc_sets_daw_param_and_echoes():
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    port.sent.clear()
    port.inject(bytes((0xBE, 1, 0x7F)))
    port.inject(bytes((0xBE, 1 + 0x20, 0x7F)))
    assert source.device_params(0)[1].value == pytest.approx(1.0)
    # the confirmation echo: value CCs back on the param bus (the knob's
    # position authority — without it the device drops its end-stops)
    echo = [f for f in port.sent if f[0] == 0xBE and f[1] in (1, 0x21)]
    assert len(echo) == 2
    assert sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)


def test_daw_param_change_reflected_to_device():
    port, _, source, _ = make()
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    port.sent.clear()
    source.simulate_device_param(0, 3, 0.25, "850 Hz")
    ccs = [f for f in port.sent if f[0] == 0xBE and f[1] in (3, 3 + 0x20)]
    assert len(ccs) == 2
    vals = sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)
    assert vals and codec.decode_string(vals[0].data[2:]) == "850 Hz"
    # repeated display is de-duplicated
    port.sent.clear()
    source.simulate_device_param(0, 3, 0.26, "850 Hz")
    assert not sysex_sent(port, Group.GENERAL, General.PARAM_VALUES)


def test_mix_knob_only_drives_volume_outside_plugin_mode():
    port, _, source, _ = make()
    port.inject(bytes((0xBF, 12, 0x40)))          # encoder 0 MSB
    port.inject(bytes((0xBF, 44, 0x00)))          # encoder 0 LSB
    assert source.tracks()[0].volume == pytest.approx((0x40 << 7) / 0x3FFF)
    inject(port, Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00")
    port.inject(bytes((0xBF, 12, 0x00)))
    port.inject(bytes((0xBF, 44, 0x00)))
    assert source.tracks()[0].volume != 0.0       # ignored in plugin mode


def test_unlabeled_transport_buttons_map_to_actions():
    # the fw transport grid's unlabeled Logic-dialect buttons (CC 33/34/35,
    # logic_transport.mmon) get rewind / fast-forward / metronome
    from athens.sysex.constants import TransportAction
    port = LoopbackMidiPort()
    client = RotoLogicClient(port)
    got = []
    client.on_transport_button = lambda a, p: got.append((a, p))
    for cc in (33, 34, 35):
        port.inject(bytes((0xBF, cc, 127)))
        port.inject(bytes((0xBF, cc, 0)))
    assert [a for a, p in got if p] == [
        TransportAction.REWIND, TransportAction.FASTFORWARD,
        TransportAction.METRONOME]


def test_transport_assignments_reroute_buttons():
    from athens.sysex.constants import TransportAction
    port = LoopbackMidiPort()
    client = RotoLogicClient(port)
    got = []
    client.on_transport_button = lambda a, p: got.append(("t", a, p))
    client.on_custom_action = lambda i, p: got.append(("c", i, p))
    client.set_transport_assignments(
        {"33": "action:41040", "34": "loop", "35": "none", "28": "stop"})
    for cc in (28, 33, 34, 35):
        port.inject(bytes((0xBF, cc, 127)))
    # 28 is NOT assignable (stays play); 33 -> raw action; 34 -> loop;
    # 35 -> unassigned (nothing)
    assert got == [("t", TransportAction.PLAY, True),
                   ("c", 41040, True),
                   ("t", TransportAction.LOOP, True)]


def test_daw_gone_blanks_the_device():
    # source signals the DAW vanished -> bridge sends Logic's EXACT shutdown
    # zeroing (logic_good_full.txt tail): NUM_SENDS/TRACKS/DEVICES 0, nothing
    # else. NO track details/END/reset — the capture proves Logic sends none.
    port, _, source, bridge = make()
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    port.sent.clear()
    source.on_daw_alive(False)             # the bridge wired this in __init__
    nt = sysex_sent(port, Group.GENERAL, General.NUM_TRACKS)
    nd = sysex_sent(port, Group.PLUGIN, Plugin.NUM_DEVICES)
    assert nt and nt[-1].data == bytes((0, 0))        # tracks zeroed
    assert nd and nd[-1].data == bytes((0,))          # devices zeroed
    assert not sysex_sent(port, Group.GENERAL, General.TRACK_DETAILS)
    assert not sysex_sent(port, Group.GENERAL, General.RESET_TRACK_DETAILS)
    # the 'no plugin' placeholder that clears a ghost PLUGIN screen (Logic
    # sends this empty PLUGIN_DETAILS at both connect and shutdown)
    pd = sysex_sent(port, Group.PLUGIN, Plugin.PLUGIN_DETAILS)
    assert pd and pd[-1].data[1:9] == bytes(8)        # slot 0, hash all-zeros


def test_frac14_frac7_normalization():
    # the shared 14-bit / 7-bit reassembly the encoder + param decoders use
    assert codec.frac14(0, 0) == 0.0
    assert codec.frac14(0x7F, 0x7F) == 1.0
    assert abs(codec.frac14(0x40, 0x00) - 0.5) < 0.01
    assert codec.frac7(0) == 0.0
    assert codec.frac7(127) == 1.0


def test_paged_first_track_clamps_to_page_boundaries():
    from athens.bridge.common import paged_first_track
    # 12 tracks, 8/page -> windows start at 0 and 8, never 11
    assert paged_first_track(0, +1, 12) == 8
    assert paged_first_track(8, +1, 12) == 8        # clamp at last window
    assert paged_first_track(8, -1, 12) == 0
    assert paged_first_track(0, -1, 12) == 0        # clamp at start
    assert paged_first_track(0, +1, 5) == 0         # < a page: no move


def test_logic_page_arrows_move_bank_and_notify_app():
    # Logic dialect pages with CC 60/61 on ch16 (hardware: 'bf 3c/3d 02'); a
    # press (value>0) moves one window and notifies the app (on_bank_changed)
    port, _, source, bridge = make()
    fired = []
    bridge.on_bank_changed = lambda: fired.append(bridge.first_track)
    inject(port, Group.MIXER, Mixer.SET_MIXER_ALL_MODE, bytes((0, 0, 0, 0)))
    assert len(source.tracks()) > 8                  # enough tracks to page
    port.inject(bytes((0xBF, 0x3D, 2)))          # right arrow press
    assert bridge.first_track == 8
    port.inject(bytes((0xBF, 0x3C, 2)))          # left arrow press
    assert bridge.first_track == 0
    assert fired == [8, 0]                           # app told on each move


# --- per-param calibration (sweep-derived step snapping) --------------------

def test_calibration_snaps_unevenly_stepped_param():
    """A stepped param whose display changes at uneven values: the device's
    even detents miss the crushed middle; the snap reaches every step."""
    _port, _client, _source, bridge = make()
    s = _Sweep(device=0, param=0, name="Type", restore_value=0.0,
               curve=[[0.0, "Off"], [0.40, "Low"], [0.42, "Mid"], [0.95, "High"]])
    bridge._build_calibration(s, quantised=4)
    assert bridge._calibration[(0, 0)] == pytest.approx([0.0, 0.4, 0.42, 0.95])
    assert bridge._snap(0, 0, 0.05) == pytest.approx(0.0)    # Off
    assert bridge._snap(0, 0, 0.43) == pytest.approx(0.42)   # Mid — crushed step
    assert bridge._snap(0, 0, 0.50) == pytest.approx(0.42)
    assert bridge._snap(0, 0, 0.90) == pytest.approx(0.95)   # High reachable


def test_calibration_skips_continuous_param():
    """Continuous/tapered params (quantised 0) get no entry — taper is left
    intact and values pass through unchanged."""
    _port, _client, _source, bridge = make()
    s = _Sweep(device=0, param=1, name="Gain", restore_value=0.5,
               curve=[[i / 20, str(i)] for i in range(20)])
    bridge._build_calibration(s, quantised=0)
    assert (0, 1) not in bridge._calibration
    assert bridge._snap(0, 1, 0.37) == 0.37                  # identity


def test_on_device_param_applies_calibration():
    """The device->DAW path sets the DAW to the snapped step, not the raw
    device value."""
    _port, _client, source, bridge = make()
    bridge._calibration[(0, 0)] = [0.0, 0.4, 0.42, 0.95]     # device 0 selected
    bridge._on_device_param(0, 0.50)                         # device sends 0.50
    assert source.device_params(0)[0].value == pytest.approx(0.42)


# -- param-identity normalisation (cross-DAW routing) -------------------------
def _plugin_source(monkeypatch, params):
    """A single-plugin source exposing `params`, recording param writes."""
    port, client, source, bridge = make()
    monkeypatch.setattr(source, "device_params", lambda dev: params)
    monkeypatch.setattr(source, "selected_device", lambda: 0)
    writes = []
    monkeypatch.setattr(source, "set_device_param",
                        lambda dev, p, v: writes.append((dev, p, v)))
    return bridge, writes


def test_identity_normalises_cross_daw_index(monkeypatch):
    """Knob learned in REAPER (index 11 = OSC2_mix) drives the SAME param where
    this DAW numbers it 20 — resolved by the param name-hash, not the index."""
    params = [PluginParam(0, "Mastertune", 0.0),
              PluginParam(1, "OSC2_octave", 0.0),
              PluginParam(20, "OSC2_mix", 0.0)]
    bridge, writes = _plugin_source(monkeypatch, params)
    bridge._param_ids = {11: codec.param_hash("OSC2_mix")}   # learned at index 11

    bridge._on_device_param(11, 0.5)

    assert writes and writes[-1][1] == 20        # routed to 20, not 11


def test_identity_is_a_noop_when_index_aligns(monkeypatch):
    """The learn DAW (or an index-aligned one) must NOT re-route — the param
    already at the device index carries the right identity."""
    params = [PluginParam(11, "OSC2_mix", 0.0)]
    bridge, writes = _plugin_source(monkeypatch, params)
    bridge._param_ids = {11: codec.param_hash("OSC2_mix")}

    bridge._on_device_param(11, 0.5)

    assert writes[-1][1] == 11                    # unchanged (fast path)


def test_identity_falls_back_to_raw_index(monkeypatch):
    """No learned identity (or the param isn't exposed here) -> raw index, i.e.
    exactly the old behaviour. Nothing regresses for unlearned knobs."""
    params = [PluginParam(7, "Cutoff", 0.0)]
    bridge, writes = _plugin_source(monkeypatch, params)
    bridge._param_ids = {}                        # nothing learned

    bridge._on_device_param(7, 0.5)

    assert writes[-1][1] == 7


def test_all_zero_value_table_is_never_flooded():
    """A >8-param table with EVERY value 0.0 is placeholders or a degenerate
    DA read — flooding it anchors every mapped knob to 0 (the knobs-at-zero /
    knobs-stuck bug). Skip it; one real value makes the flood flow again."""
    port, client, source, bridge = make()
    zeros = [PluginParam(i, f"P{i}", 0.0) for i in range(20)]
    port.sent.clear()
    bridge._push_all_param_values(zeros)
    assert port.sent == []                       # nothing anchored to zero

    zeros[3] = PluginParam(3, "P3", 0.4)         # one genuine value -> real table
    bridge._push_all_param_values(zeros)
    assert port.sent != []


def test_mixer_push_never_touches_the_plugin_screen():
    """A tracks_changed draining while the PLUGIN screen is up must not send
    encoder-position CCs — they clobber the mapped knobs' anchors (the
    knobs-stuck-at-their-mix-positions bug on plugin entry)."""
    port, client, source, bridge = make()
    bridge._on_plugin_mode(False)          # device is on the plugin screen
    port.sent.clear()

    bridge._push_mixer()                   # e.g. a queued tracks_changed

    assert port.sent == []                 # NOTHING sent while plugin is up

    bridge._on_mixer_all_mode(0, 0, 0)     # back to mix: pushes flow again
    assert port.sent != []


def test_service_builds_identity_map_from_refs():
    """The service turns persisted learn refs into {device_index -> name-hash}
    for BOTH control kinds — filtering to knob: left every learned BUTTON
    un-routable (it fell back to its raw learn-DAW index and toggled whatever
    the current DAW keeps there: the OSC_key_sync-toggles-the-sub bug)."""
    from athens.api.service import BridgeService
    from athens.daw.source import MockSysexSource
    svc = BridgeService(source=MockSysexSource())
    svc._param_refs = {"aabbcc": {
        "knob:0": {"param_index": 11, "param_name": "OSC2_mix"},
        "knob:1": {"param_index": 3, "param_name": "OSC2_octave"},
        "switch:2": {"param_index": 149, "param_name": "OSC1_on/off"},
    }}
    m = svc._param_identity_for_plugin(bytes.fromhex("aabbcc"))
    assert m == {11: codec.param_hash("OSC2_mix"),
                 3: codec.param_hash("OSC2_octave"),
                 149: codec.param_hash("OSC1_on/off")}


def test_service_identity_falls_back_to_device_map():
    """No persisted refs -> read the identities straight from the DEVICE's
    stored map (MH = the hash6 sent at learn time). Cached; empty-refs plugins
    learned before Athens still route cross-DAW."""
    from athens.api.service import BridgeService
    from athens.daw.source import MockSysexSource

    class _Cfg:                                     # knob config stub
        def __init__(self, mi, mh):
            self.mapped_param_index = mi
            self.mapped_param_hash = mh

    class _Roto:
        def read_plugin_knob_config(self, h, i):
            return _Cfg(146, b"\x01\x02\x03\x04\x05\x06") if i == 0 else None

        def read_plugin_switch_config(self, h, i):
            return ({"mapped_param_index": 149,
                     "mapped_param_hash": b"\x0a\x0b\x0c\x0d\x0e\x0f"}
                    if i == 1 else None)

    svc = BridgeService(source=MockSysexSource())
    svc._param_refs = {}
    svc.roto = _Roto()
    m = svc._param_identity_for_plugin(bytes.fromhex("16485d67605b5e5a"))
    assert m == {146: b"\x01\x02\x03\x04\x05\x06",
                 149: b"\x0a\x0b\x0c\x0d\x0e\x0f"}
    # cached: a second call must not re-read serial
    svc.roto = None
    assert svc._param_identity_for_plugin(
        bytes.fromhex("16485d67605b5e5a")) == m
