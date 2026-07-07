"""Handshake + mixer-flow tests for the SysEx client/bridge, via LoopbackMidiPort.

No hardware: we inject the device-side SysEx messages and assert the client and
bridge react with the right outbound frames.
"""
from athens.daw.source import MockSysexSource
from athens.roto.sysex_client import LoopbackMidiPort, RotoSysexClient
from athens.sysex import codec
from athens.sysex.constants import General, Group, Mixer


def gc(frame: bytes):
    m = codec.parse_sysex(frame)
    return (m.group, m.command)


def frames(port, group, command):
    return [f for f in port.sent
            if f and f[0] == 0xF0 and gc(f) == (group, command)]


def dev(group, command, data=b""):
    """A device -> DAW message to inject."""
    return codec.build_sysex(group, command, data)


# --- handshake -------------------------------------------------------------

def test_start_announces_daw():
    port = LoopbackMidiPort()
    RotoSysexClient(port).start()
    assert port.sent[-1] == codec.daw_started()


def test_client_auto_answers_ping():
    port = LoopbackMidiPort()
    RotoSysexClient(port)
    port.inject(dev(Group.GENERAL, General.PING_DAW))
    assert codec.daw_ping_response() in port.sent


def test_connected_callback_fires():
    port = LoopbackMidiPort()
    c = RotoSysexClient(port)
    seen = []
    c.on_connected = lambda: seen.append(True)
    port.inject(dev(Group.GENERAL, General.ROTO_DAW_CONNECTED))
    assert seen == [True]

# --- plugin mode (VST-follow) ----------------------------------------------

def test_control_mapped_dispatches_decoded():
    from athens.sysex.constants import Plugin
    port = LoopbackMidiPort()
    client = RotoSysexClient(port)
    got = []
    client.on_control_mapped = lambda cm: got.append(cm)
    # knob 4 -> param 300, hash 010203040506, not macro
    port.inject(dev(Group.PLUGIN, Plugin.CONTROL_MAPPED,
                    codec.u14(300) + bytes.fromhex("010203040506") + bytes((0, 4, 0))))
    assert len(got) == 1
    assert got[0].param_index == 300 and got[0].control_index == 4


def test_send_learn_param_round_trips_through_client():
    from athens.sysex.constants import Group as G, Plugin
    port = LoopbackMidiPort()
    client = RotoSysexClient(port)
    client.send_learn_param(5, "Cutoff", 1.0, codec.param_hash("Cutoff"))
    m = codec.parse_sysex(port.sent[-1])
    assert (m.group, m.command) == (int(G.PLUGIN), int(Plugin.LEARN_PARAM))
    assert codec.parse_u14(m.data) == 5
    assert codec.decode_string(m.data[13:26]) == "Cutoff"


# --- value-channel CC routing ------------------------------------------------

def _cc_client():
    port = LoopbackMidiPort()
    client = RotoSysexClient(port)
    return port, client


def test_encoder_cc_pair_fires_one_14bit_value():
    from athens.sysex.constants import ENCODER_FIRST_CC
    port, client = _cc_client()
    got = []
    client.on_value = lambda cc, v: got.append((cc, v))
    port.inject(bytes((0xBF, ENCODER_FIRST_CC + 1, 0x40)))        # MSB alone: wait
    assert got == []
    port.inject(bytes((0xBF, ENCODER_FIRST_CC + 1 + 32, 0x21)))   # LSB completes
    assert got == [(ENCODER_FIRST_CC + 1, ((0x40 << 7) | 0x21) / 0x3FFF)]


def test_unpaired_msb_flushes_at_7bit_when_next_msb_arrives():
    from athens.sysex.constants import ENCODER_FIRST_CC
    port, client = _cc_client()
    got = []
    client.on_value = lambda cc, v: got.append((cc, v))
    port.inject(bytes((0xBF, ENCODER_FIRST_CC, 100)))   # LSB lost
    port.inject(bytes((0xBF, ENCODER_FIRST_CC, 101)))   # next MSB flushes it
    assert got == [(ENCODER_FIRST_CC, 100 / 127)]


def test_transport_button_cc_dispatch():
    from athens.sysex.constants import TRANSPORT_FIRST_CC, TransportAction
    port, client = _cc_client()
    got = []
    client.on_transport_button = lambda a, p: got.append((a, p))
    port.inject(bytes((0xBF, TRANSPORT_FIRST_CC + 0, 127)))
    port.inject(bytes((0xBF, TRANSPORT_FIRST_CC + 9, 127)))
    port.inject(bytes((0xBF, TRANSPORT_FIRST_CC + 9, 0)))
    assert got == [(TransportAction.PLAY, True),
                   (TransportAction.FASTFORWARD, True),
                   (TransportAction.FASTFORWARD, False)]


def test_touch_and_button_cc_dispatch():
    from athens.sysex.constants import BUTTON_FIRST_CC, TOUCH_FIRST_CC
    port, client = _cc_client()
    touches, buttons = [], []
    client.on_touch = lambda i, p: touches.append((i, p))
    client.on_button = lambda i, p: buttons.append((i, p))
    port.inject(bytes((0xBF, TOUCH_FIRST_CC + 4, 127)))
    port.inject(bytes((0xBF, TOUCH_FIRST_CC + 4, 0)))
    port.inject(bytes((0xBF, BUTTON_FIRST_CC + 7, 127)))
    assert touches == [(4, True), (4, False)]
    assert buttons == [(7, True)]


def test_send_transport_led():
    from athens.sysex.constants import TRANSPORT_FIRST_CC, TransportAction
    port, client = _cc_client()
    client.send_transport_led(TransportAction.LOOP, True)
    client.send_transport_led(TransportAction.LOOP, False)
    assert port.sent == [bytes((0xBF, TRANSPORT_FIRST_CC + 4, 127)),
                         bytes((0xBF, TRANSPORT_FIRST_CC + 4, 0))]
