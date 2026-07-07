"""SetupLibrary + its RPC surface: edit/dirty/deploy flow, persistence,
import/export round-trip."""
import json
import tempfile
from pathlib import Path

from athens.api.service import BridgeService
from athens.library import SetupLibrary, seed_demo


def test_edit_marks_dirty_deploy_clears():
    lib = SetupLibrary()
    lib.update_control(3, "knob", 0, {"name": "Cutoff", "param": 74})
    assert lib.get(3)["dirty"] is True and lib.get(3)["deployed"] is False
    lib.deploy(3)
    s = lib.get(3)
    assert s["dirty"] is False and s["deployed"] is True
    assert s["knobs"]["0"]["name"] == "Cutoff"


def test_name_truncated_to_12_and_unknown_fields_rejected():
    lib = SetupLibrary()
    lib.update_control(0, "knob", 0, {"name": "A very long knob name"})
    assert lib.get(0)["knobs"]["0"]["name"] == "A very long "
    try:
        lib.update_control(0, "knob", 1, {"bogus": 1})
    except ValueError:
        return
    assert False, "unknown fields must be rejected"


def test_clear_control_removes_slot():
    lib = SetupLibrary()
    lib.update_control(0, "switch", 5, {"name": "Mute"})
    lib.update_control(0, "switch", 5, None)
    assert "5" not in lib.get(0)["switches"]


def test_persistence_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "lib.json"
        lib = SetupLibrary(path)
        lib.set_name(2, "Persisted")
        lib.update_control(2, "knob", 1, {"name": "Res", "param": 71})
        lib2 = SetupLibrary(path)
        s = lib2.get(2)
        assert s["name"] == "Persisted" and s["knobs"]["1"]["param"] == 71


def test_export_import_round_trip():
    lib = SetupLibrary()
    seed_demo(lib)
    exported = lib.export_setup(0)
    assert exported["type"] == "MIDI" and len(exported["knobs"]) == 8
    assert json.loads(json.dumps(exported)) == exported   # JSON-clean

    other = SetupLibrary()
    target = other.import_setup(exported, index=7)
    assert target == 7
    s = other.get(7)
    assert s["name"] == "REAPER Mix"
    assert s["knobs"]["0"]["name"] == "Kick" and len(s["switches"]) == 16
    assert s["dirty"] is True                       # imported = not yet deployed


def test_rpc_surface_and_setups_event():
    svc = BridgeService()
    seed_demo(svc.library)
    svc.start()
    events = []
    svc.bus.subscribe("setups", events.append)

    setups = svc.rpc.handle({"id": 1, "method": "list_setups"})["result"]
    assert any(s["name"] == "REAPER Mix" and s["deployed"] for s in setups)

    r = svc.rpc.handle({"id": 2, "method": "update_control",
                        "params": {"index": 1, "kind": "knob", "slot": 4,
                                   "fields": {"name": "Drive", "param": 19}}})
    assert r["result"]["name"] == "Drive"
    assert events and events[-1]["index"] == 1

    s = svc.rpc.handle({"id": 3, "method": "get_setup",
                        "params": {"index": 1}})["result"]
    assert s["dirty"] is True and s["knobs"]["4"]["param"] == 19

    svc.rpc.handle({"id": 4, "method": "deploy_setup", "params": {"index": 1}})
    s = svc.rpc.handle({"id": 5, "method": "get_setup",
                        "params": {"index": 1}})["result"]
    assert s["dirty"] is False and s["deployed"] is True


def test_rpc_rejects_bad_kind_and_range():
    svc = BridgeService()
    seed_demo(svc.library)
    bad = svc.rpc.handle({"id": 1, "method": "update_control",
                          "params": {"index": 0, "kind": "fader", "slot": 0,
                                     "fields": {}}})
    assert "error" in bad
    bad = svc.rpc.handle({"id": 2, "method": "get_setup", "params": {"index": 99}})
    assert "error" in bad
