"""Pro Tools / HUI source + wire codec.

The wire values here are the spec (cross-verified against MIDIKit, the HUI-MCU
logger and mackie-hui-osc) — treat this file as the contract the ProToolsSource
must satisfy. Behaviour on real Pro Tools still needs a hardware pass, but the
byte-level encode/decode is pinned here.
"""
from athens.daw import hui
from athens.daw.protools_source import ProToolsSource
from athens.sysex.constants import TransportAction


# -- fake port: captures raw sends, lets us inject received bytes -------------
class FakePort:
    def __init__(self):
        self.sent = bytearray()
        self.on_receive = None

    def send_raw(self, data):
        self.sent += bytes(data)

    def close(self):
        pass

    def inject(self, *msgs):
        for m in msgs:
            self.on_receive(bytes(m))


def _src():
    p = FakePort()
    s = ProToolsSource(port=p)
    s.start()
    p.sent.clear()          # drop the system-reset announce
    return s, p


# =============================================================================
# encoders
# =============================================================================
def test_encode_fader():
    assert hui.fader(0, 1.0) == bytes([0xB0, 0x00, 0x7F, 0xB0, 0x20, 0x7F])
    assert hui.fader(0, 0.0) == bytes([0xB0, 0x00, 0x00, 0xB0, 0x20, 0x00])
    # channel offset lands on the right CCs
    assert hui.fader(3, 0.0)[:2] == bytes([0xB0, 0x03])
    assert hui.fader(3, 0.0)[3:5] == bytes([0xB0, 0x23])


def test_encode_switch_press():
    # MUTE on channel 2, surface->DAW: zone 0x0F=02, port 0x2F=0x42 (0x2 | on)
    assert hui.switch(2, hui.P_MUTE, True) == bytes([0xB0, 0x0F, 0x02, 0xB0, 0x2F, 0x42])
    assert hui.switch(2, hui.P_MUTE, False) == bytes([0xB0, 0x0F, 0x02, 0xB0, 0x2F, 0x02])
    # a momentary press is down-then-up
    assert hui.press(2, hui.P_MUTE) == hui.switch(2, hui.P_MUTE, True) + \
        hui.switch(2, hui.P_MUTE, False)


def test_encode_vpot_delta_direction():
    assert hui.vpot_delta(0, 1) == bytes([0xB0, 0x40, 0x41])    # cw = 0x40 bit set
    assert hui.vpot_delta(0, -1) == bytes([0xB0, 0x40, 0x01])   # ccw
    assert hui.vpot_delta(0, 0) == b""                          # no move, no message
    assert hui.vpot_delta(7, 200)[2] == 0x40 | 63               # magnitude clamps to 63


def test_encode_ping_reply():
    assert hui.ping_reply() == b"\x90\x00\x7f"


# =============================================================================
# decoder
# =============================================================================
def test_decode_fader_pair():
    d = hui.HuiDecoder()
    assert d.feed(bytes([0xB0, 0x00, 0x40])) == []          # MSB latched, no event yet
    ev = d.feed(bytes([0xB0, 0x20, 0x00]))                  # LSB -> emit
    assert ev == [("fader", 0, 0x2000 / 0x3FFF)]            # 0x40<<7 = 0x2000 ~ 0.5


def test_decode_switch_led():
    d = hui.HuiDecoder()
    d.feed(bytes([0xB0, hui.ZONE_LED, 0x02]))               # zone = channel 2
    assert d.feed(bytes([0xB0, hui.PORT_LED, 0x42])) == [("flag", 2, "muted", True)]
    d.feed(bytes([0xB0, hui.ZONE_LED, 0x02]))
    assert d.feed(bytes([0xB0, hui.PORT_LED, 0x02])) == [("flag", 2, "muted", False)]


def test_decode_transport_and_select():
    d = hui.HuiDecoder()
    d.feed(bytes([0xB0, hui.ZONE_LED, hui.ZONE_TRANSPORT]))
    assert d.feed(bytes([0xB0, hui.PORT_LED, 0x40 | hui.T_PLAY])) == \
        [("transport", "play", True)]
    d.feed(bytes([0xB0, hui.ZONE_LED, 0x05]))               # channel 5
    assert d.feed(bytes([0xB0, hui.PORT_LED, 0x40 | hui.P_SELECT])) == \
        [("select", 5, True)]


def test_decode_ping_and_meter():
    d = hui.HuiDecoder()
    assert d.feed(b"\x90\x00\x00") == [("ping",)]           # ping (vel 0)
    assert d.feed(b"\x80\x00\x00") == [("ping",)]           # note-off form (CoreMIDI)
    assert d.feed(hui.PING_REPLY) == []                     # our OWN reply echo (vel 127)
    #                                                         is NOT a ping — no self-storm
    assert d.feed(b"\xA0\x00\x08") == []                    # meter (poly-AT) ignored


def test_decode_name_sysex():
    d = hui.HuiDecoder()
    sx = bytes([0xF0]) + hui._SYSEX_BODY + bytes([hui.DISPLAY_SMALL, 0x02]) \
        + b"Kick" + bytes([0xF7])
    assert d.feed(sx) == [("name", 2, "Kick")]


def test_decode_undocumented_state_nibble_ignored():
    d = hui.HuiDecoder()
    d.feed(bytes([0xB0, hui.ZONE_LED, 0x03]))
    # 0x2N is Pro Tools' automation-mode artifact — must not emit a flag event
    assert d.feed(bytes([0xB0, hui.PORT_LED, 0x23])) == []


# =============================================================================
# source: Pro Tools -> Athens (feedback fires callbacks)
# =============================================================================
def test_ping_is_answered_and_marks_alive():
    s, p = _src()
    alive = []
    s.on_daw_alive = alive.append
    p.inject(b"\x90\x00\x00")
    assert bytes(p.sent) == hui.ping_reply()                # replied to the ping
    assert alive == [True]


def test_fader_feedback_updates_volume():
    s, p = _src()
    seen = []
    s.on_track_volume = lambda ch, v: seen.append((ch, round(v, 3)))
    p.inject(bytes([0xB0, 0x00, 0x40]), bytes([0xB0, 0x20, 0x00]))
    assert seen == [(0, round(0x2000 / 0x3FFF, 3))]
    assert s.tracks()[0].volume == 0x2000 / 0x3FFF


def test_mute_feedback_updates_flag():
    s, p = _src()
    seen = []
    s.on_track_flag = lambda ch, f, on: seen.append((ch, f, on))
    p.inject(bytes([0xB0, hui.ZONE_LED, 0x04]), bytes([0xB0, hui.PORT_LED, 0x42]))
    assert seen == [(4, "muted", True)]
    assert s.tracks()[4].muted is True


def test_name_feedback():
    s, p = _src()
    changed = []
    s.on_tracks_changed = lambda: changed.append(True)
    sx = bytes([0xF0]) + hui._SYSEX_BODY + bytes([hui.DISPLAY_SMALL, 0x03]) \
        + b"Bass" + bytes([0xF7])
    p.inject(sx)
    assert s.tracks()[3].name == "Bass"
    assert changed == [True]


# =============================================================================
# source: Athens -> Pro Tools (the ROTO's moves become HUI)
# =============================================================================
def test_set_volume_touches_then_sends_fader():
    s, p = _src()
    s.set_track_volume(1, 0.0)
    # fader touch (zone 1, port 0, on) precedes the fader value
    assert bytes(p.sent).startswith(hui.fader_touch(1, True) + hui.fader(1, 0.0))
    s.stop()


def test_set_flag_only_presses_to_change():
    s, p = _src()
    s.set_track_flag(2, "muted", True)                      # off -> on: one press
    assert bytes(p.sent) == hui.press(2, hui.P_MUTE)
    p.sent.clear()
    s.set_track_flag(2, "muted", True)                      # already on: no-op
    assert bytes(p.sent) == b""


def test_monitoring_flag_has_no_hui_switch():
    s, p = _src()
    s.set_track_flag(0, "monitoring", True)                 # HUI has no per-strip monitor
    assert bytes(p.sent) == b""


def test_transport_and_bank():
    s, p = _src()
    s.transport_action(TransportAction.PLAY, True)
    assert bytes(p.sent) == hui.press(hui.ZONE_TRANSPORT, hui.T_PLAY)
    p.sent.clear()
    s.page(+1)
    assert bytes(p.sent) == hui.press(hui.ZONE_BANK, hui.BANK_RIGHT)
    s.stop()


# =============================================================================
# the IAC bus is a LOOPBACK: everything we transmit is echoed back at us
# =============================================================================
def test_loopback_volume_echo_is_benign():
    s, p = _src()
    seen = []
    s.on_track_volume = lambda ch, v: seen.append((ch, v))
    s.set_track_volume(2, 0.5)
    sent = bytes(p.sent)
    pair = hui.fader(2, 0.5)                 # the bus echoes our own fader pair
    p.inject(pair[:3], pair[3:])
    # the echo settles on the value we sent — and triggers NO further sends
    assert round(s.tracks()[2].volume, 3) == 0.5
    assert seen and round(seen[-1][1], 3) == 0.5
    assert bytes(p.sent) == sent
    s.stop()


def test_pan_echo_does_not_corrupt_gesture_baseline():
    s, p = _src()
    s.set_track_pan(0, 0.5)            # anchors the baseline (0 steps at centre)
    p.sent.clear()
    s.set_track_pan(0, 0.6)            # +4 steps at _PAN_STEPS=40
    assert bytes(p.sent) == hui.vpot_delta(0, 4)
    p.sent.clear()
    # Pro Tools' coarse (~11-step) LED-ring echo lands MID-GESTURE: it must
    # update the displayed pan but NOT the outgoing delta baseline
    p.inject(bytes([0xB0, hui.VPOT_LED + 0, 0x07]))
    assert s.tracks()[0].pan == 0.6    # display follows the ring echo
    s.set_track_pan(0, 0.7)            # still exactly +4 forward steps
    assert bytes(p.sent) == hui.vpot_delta(0, 4)
    s.stop()


def test_release_identity_guard():
    # Timer.cancel() cannot stop a callback that already began — a stale
    # release firing after a re-arm must be a NO-OP, not a touch-off
    s, p = _src()
    s.set_track_volume(3, 0.5)
    t1 = s._touch[3]
    s.set_track_volume(3, 0.6)          # re-arm replaces the timer
    t2 = s._touch[3]
    p.sent.clear()
    s._release(3, t1)                   # the superseded timer fires anyway
    assert bytes(p.sent) == b""         # fader must stay touched
    s._release(3, t2)                   # the current timer releases for real
    assert bytes(p.sent) == hui.fader_touch(3, False)
    s.stop()


def test_decode_name_sysex_multi_group():
    # Logic-style packing: several 5-byte <disp><4 chars> groups in ONE SysEx
    d = hui.HuiDecoder()
    sx = bytes([0xF0]) + hui._SYSEX_BODY + bytes([hui.DISPLAY_SMALL]) \
        + bytes([0x00]) + b"Kick" + bytes([0x01]) + b"Snar" + bytes([0xF7])
    assert d.feed(sx) == [("name", 0, "Kick"), ("name", 1, "Snar")]


def test_stop_forgets_port_for_revive():
    # the service detach path stops then re-starts the SAME source instance:
    # stop() must drop the closed port so start() opens a fresh one
    s, p = _src()
    s.stop()
    assert s.feed_running() is False
    assert s._port is None
