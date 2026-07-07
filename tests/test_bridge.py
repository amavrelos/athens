"""Regression tests for the serial-path Bridge: flash diffing, CC-overlap
validation, start ordering, and the mock backend loop. No hardware."""
from athens.bridge.bridge import Bridge, BridgeConfig
from athens.daw.mock import MockDawBackend
from athens.roto.client import RotoControl
from athens.roto.transport import LoopbackTransport

SET_KNOB = bytes((0x5A, 0x02, 0x07))
SET_SWITCH = bytes((0x5A, 0x02, 0x08))
SET_SETUP = bytes((0x5A, 0x02, 0x03))
CONFIG_FRAMES = (SET_KNOB, SET_SWITCH,
                 bytes((0x5A, 0x02, 0x04)),   # set setup name
                 bytes((0x5A, 0x02, 0x09)))   # clear control


def _wired():
    transport = LoopbackTransport()
    roto = RotoControl(transport)
    bridge = Bridge(roto, MockDawBackend())
    return transport, roto, bridge


def _config_frames(transport):
    return [f for f in transport.sent if f[:3] in CONFIG_FRAMES]


def test_identical_bank_reapplied_writes_nothing():
    transport, _, bridge = _wired()
    bridge.start()
    n_first = len(_config_frames(transport))
    assert n_first > 0
    bridge.apply_bank(bridge.daw.current_bank())    # unchanged bank
    assert len(_config_frames(transport)) == n_first   # zero new flash writes


def test_changed_slot_writes_only_that_slot():
    transport, _, bridge = _wired()
    bridge.start()
    before = len(_config_frames(transport))
    bank = bridge.daw.current_bank()
    bank.params[0].name = "Renamed"
    bridge.apply_bank(bank)
    delta = _config_frames(transport)[before:]
    assert len(delta) == 1 and delta[0][:3] == SET_KNOB


def test_start_selects_setup_before_pushing_values():
    transport, roto, bridge = _wired()
    events = []
    orig_request = transport.request
    transport.request = lambda frame, *a, **k: (events.append(("frame", bytes(frame[:3]))),
                                                orig_request(frame, *a, **k))[1]
    roto.send_value = lambda ch, cc, v, **k: events.append(("value", cc))
    bridge.start()
    select_at = next(i for i, e in enumerate(events)
                     if e == ("frame", SET_SETUP))
    first_value_at = next(i for i, e in enumerate(events) if e[0] == "value")
    assert select_at < first_value_at


def test_knob_button_cc_overlap_rejected():
    transport = LoopbackTransport()
    roto = RotoControl(transport)
    try:
        Bridge(roto, MockDawBackend(),
               BridgeConfig(base_cc=0x10, button_base_cc=0x30))  # LSB collision
    except ValueError:
        return
    assert False, "overlapping CC ranges must be rejected"


def test_control_value_routes_knob_and_button():
    _, _, bridge = _wired()
    bridge.start()
    daw = bridge.daw
    bridge._on_control_value(bridge.cfg.channel, bridge.cfg.base_cc, 0.25)
    assert abs(daw._vol["track/0/volume"] - 0.25) < 1e-6
    bridge._on_control_value(bridge.cfg.channel, bridge.cfg.button_base_cc, 1.0)
    assert daw._mute["track/0/mute"] is True
