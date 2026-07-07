"""Tests for the UI API: pure RPC core, event bus, and the BridgeService
wiring — all offline, no websockets/pywebview needed."""
from athens.api.rpc import EventBus, JsonRpcApi, RpcError
from athens.api.service import BridgeService
from athens.daw.source import MockSysexSource
from athens.roto.sysex_client import LoopbackMidiPort
from athens.sysex import codec
from athens.sysex.constants import General, Group


# --- rpc core -----------------------------------------------------------------

def test_rpc_dispatch_and_result():
    api = JsonRpcApi()
    api.register("add", lambda a, b: a + b)
    resp = api.handle({"id": 1, "method": "add", "params": {"a": 2, "b": 3}})
    assert resp["result"] == 5 and resp["id"] == 1


def test_rpc_unknown_method_and_bad_params():
    api = JsonRpcApi()
    api.register("f", lambda a: a)
    assert api.handle({"id": 1, "method": "nope"})["error"]["code"] == -32601
    assert api.handle({"id": 2, "method": "f",
                       "params": {"wrong": 1}})["error"]["code"] == -32602


def test_rpc_error_passthrough_never_raises():
    api = JsonRpcApi()

    def boom():
        raise RpcError("custom", code=-1)
    api.register("boom", boom)
    api.register("crash", lambda: 1 / 0)
    assert api.handle({"id": 1, "method": "boom"})["error"]["code"] == -1
    assert "error" in api.handle({"id": 2, "method": "crash"})


def test_bus_subscribe_publish_unsubscribe():
    bus = EventBus()
    seen = []
    unsub = bus.subscribe("t", seen.append)
    bus.publish("t", 1)
    unsub()
    bus.publish("t", 2)
    assert seen == [1]


def test_bus_survives_bad_listener():
    bus = EventBus()
    seen = []
    bus.subscribe("t", lambda d: 1 / 0)
    bus.subscribe("t", seen.append)
    bus.publish("t", "x")
    assert seen == ["x"]


# --- service wiring: real service + mock source + a loopback device -----------

def _service():
    svc = BridgeService(source=MockSysexSource())
    svc.start()                                  # taps the source (Live is live)
    svc._attach_midi(LoopbackMidiPort())         # attach a device -> client taps
    svc.port.inject(codec.build_sysex(Group.GENERAL, General.ROTO_DAW_CONNECTED))
    return svc


def test_get_state_snapshot():
    svc = _service()
    state = svc.rpc.handle({"id": 1, "method": "get_state"})["result"]
    assert state["connected"] is True          # device CONNECTED handshake
    assert state["count"] == 12 and len(state["tracks"]) == 12
    assert state["tracks"][3]["name"] == "Bass"
    assert len(state["devices"]) == 3


def test_select_track_updates_and_validates():
    svc = _service()
    ok = svc.rpc.handle({"id": 1, "method": "select_track",
                         "params": {"index": 5}})
    assert ok["result"]["index"] == 5
    assert svc.source.selected_track() == 5
    bad = svc.rpc.handle({"id": 2, "method": "select_track",
                          "params": {"index": 99}})
    assert "error" in bad


def test_events_flow_from_source_to_bus():
    svc = _service()
    got = {"transport": [], "tracks": [], "param": []}
    for topic, sink in got.items():
        svc.bus.subscribe(topic, sink.append)
    svc.source.simulate_play(True)
    svc.source.simulate_rename(0, "Kick 2")
    svc.source.simulate_device_param(1, 1, 0.9, "10:1")
    assert got["transport"] and got["transport"][0]["playing"] is True
    assert got["tracks"] and got["tracks"][0]["tracks"][0]["name"] == "Kick 2"
    assert got["param"] == [{"device": 1, "param": 1, "value": 0.9,
                             "display": "10:1"}]


def test_touch_cc_publishes_touch_topic_not_value():
    svc = _service()
    touches, values = [], []
    svc.bus.subscribe("touch", touches.append)
    svc.bus.subscribe("value", values.append)
    svc.port.inject(bytes((0xBF, 52 + 3, 127)))   # knob 3 touched
    svc.port.inject(bytes((0xBF, 12 + 3, 64)))    # knob 3 turned: 14-bit MSB...
    svc.port.inject(bytes((0xBF, 44 + 3, 16)))    # ...and its LSB on CC+32
    assert touches == [{"knob": 3, "touched": True}]
    assert values == [{"cc": 15, "value": ((64 << 7) | 16) / 0x3FFF}]


def test_outbound_frames_published_for_diagnostics():
    svc = _service()
    frames = []
    svc.bus.subscribe("frame", frames.append)
    # a selection push is synchronous -> outbound focus-track sysex on the wire
    svc.rpc.handle({"id": 1, "method": "select_track", "params": {"index": 2}})
    kinds = {f["kind"] for f in frames}
    assert "sysex" in kinds and all(f["dir"] == "out" for f in frames)
