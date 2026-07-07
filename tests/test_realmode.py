"""Real-mode session plumbing: disconnected start, attach/detach, and the
device-backed dump/deploy RPCs — using injected fakes (no hardware)."""
from athens.api.service import BridgeService
from athens.daw.source import MockSysexSource
from athens.roto.sysex_client import LoopbackMidiPort

from tests.test_backup import make_fake_device


def _real_service():
    # a mock DAW source; device attached later
    return BridgeService(source=MockSysexSource())


def test_real_mode_starts_disconnected():
    svc = _real_service()
    svc.start()
    s = svc.rpc.handle({"id": 1, "method": "get_state"})["result"]
    assert s["connected"] is False
    assert s["serial"] is False and s["attached"] is False
    assert svc.library.list() == []            # no seeding without a library


def test_attach_midi_after_start_begins_handshake():
    svc = _real_service()
    svc.start()
    port = LoopbackMidiPort()
    svc._attach_midi(port)
    # bridge started on attach -> DAW_STARTED announced
    assert port.sent and port.sent[0][:7] == bytes.fromhex("F000220302" + "0A01")
    # device completing the handshake flips the pill
    events = []
    svc.bus.subscribe("device", events.append)
    port.inject(bytes.fromhex("F000220302" + "0A0C" + "F7"))
    assert events[-1] == {"connected": True, "serial": False}


def test_dump_device_requires_serial_then_lands_in_library():
    svc = _real_service()
    svc.start()
    err = svc.rpc.handle({"id": 1, "method": "dump_device"})
    assert "error" in err                       # no serial link yet

    svc._attach_serial(make_fake_device())
    progress = []
    svc.bus.subscribe("progress", progress.append)
    r = svc.rpc.handle({"id": 2, "method": "dump_device",
                        "params": {"indices": [0, 1]}})
    assert r["result"] == {"setups": 1}
    assert progress and progress[-1] == {"stage": "setups", "done": 2, "total": 2}
    listed = svc.library.list()
    assert listed and listed[0]["name"] == "Live Set" and listed[0]["deployed"]


def test_deploy_setup_with_serial_runs_real_restore():
    svc = _real_service()
    svc.start()
    roto = make_fake_device()
    svc._attach_serial(roto)
    svc.rpc.handle({"id": 1, "method": "dump_device", "params": {"indices": [0]}})
    # edit one knob locally -> deploy -> exactly one knob write on the wire
    svc.rpc.handle({"id": 2, "method": "update_control",
                    "params": {"index": 0, "kind": "knob", "slot": 0,
                               "fields": {"name": "Cutoff 2"}}})
    writes_before = len([f for f in roto._t.sent if f[1:3] == b"\x02\x07"])
    r = svc.rpc.handle({"id": 3, "method": "deploy_setup", "params": {"index": 0}})
    assert r["result"]["written"] == 1 and r["result"]["deployed"] is True
    writes = [f for f in roto._t.sent if f[1:3] == b"\x02\x07"]
    assert len(writes) - writes_before == 1 and b"Cutoff 2" in writes[-1]
    assert svc.library.get(0)["dirty"] is False


def test_disconnect_detaches():
    svc = _real_service()
    svc.start()
    svc._attach_midi(LoopbackMidiPort())
    svc._attach_serial(make_fake_device())
    events = []
    svc.bus.subscribe("device", events.append)
    r = svc.rpc.handle({"id": 2, "method": "disconnect"})
    assert r["result"] == {"connected": False}
    assert svc.bridge is None and svc.roto is None
    assert events[-1] == {"connected": False, "serial": False}


def test_list_ports_degrades_without_backends():
    svc = _real_service()
    ports = svc.rpc.handle({"id": 1, "method": "list_ports"})["result"]
    assert set(ports) == {"serial", "midi_in", "midi_out"}
    assert all(isinstance(v, list) for v in ports.values())


def test_plugin_link_overrides_announced_hash():
    """The LINK registry: a REAPER FX linked to a device map announces the
    LINKED hash8 so mappings learned in another DAW attach."""
    import time
    from athens.sysex import codec
    from athens.sysex.constants import Group, Plugin

    svc = _real_service()
    svc.start()
    port = LoopbackMidiPort()
    svc._attach_midi(port)
    svc.rpc.handle({"id": 1, "method": "link_plugin", "params": {
        "reaper_name": "EQ Eight", "hash": "0102030405060708",
        "device_name": "EQ8 from Logic"}})
    port.inject(bytes.fromhex("F000220302" + "0B01" + "00" + "F7"))
    deadline = time.time() + 2.0
    linked = None
    while time.time() < deadline and linked is None:
        for f in list(port.sent):
            if f and f[0] == 0xF0:
                m = codec.parse_sysex(f)
                if m.group == Group.PLUGIN and m.command == Plugin.PLUGIN_DETAILS:
                    linked = m.data[1:9]
        time.sleep(0.02)
    assert linked == bytes.fromhex("0102030405060708")


class _FakeRoto:
    """Serial stand-in: hands out one knob config, captures flash writes."""

    def __init__(self, cfg):
        self._cfg = cfg
        self.written = []

    def read_plugin_knob_config(self, plugin_hash, slot):
        return self._cfg

    def config_update(self):
        import contextlib
        return contextlib.nullcontext()

    def _req(self, payload):
        self.written.append(payload)
        return b""


def test_device_map_edit_clamps_to_flash_bounds():
    # out-of-range editor input (steps=10000, min=-5, ...) must be clamped
    # before it reaches device flash — values are 14-bit, detents 0 or 2-10
    from athens.protocol.codec import PluginKnobConfig

    svc = _real_service()
    svc.start()
    cfg = PluginKnobConfig(plugin_hash=bytes(8), control_index=0,
                           name="Tune1", max_value=16383)
    svc.roto = _FakeRoto(cfg)
    r = svc.rpc.handle({"id": 1, "method": "set_device_plugin_control",
                        "params": {"hash": "00" * 8, "kind": "knob", "slot": 0,
                                   "fields": {"steps": 10000, "min": -5,
                                              "max": 99999, "colour": 999,
                                              "name": "ABCDEFGHIJKLMNOP",
                                              "step_names": []}}})
    assert r["result"]["written"] is True
    assert svc.roto.written                      # the write really went out
    assert cfg.steps == 10 and cfg.min_value == 0
    assert cfg.max_value == 16383 and cfg.colour == 127
    assert cfg.name == "ABCDEFGHIJKL"            # 12-char display budget

    # a single detent is meaningless: coerced up to the smallest legal count
    svc.rpc.handle({"id": 2, "method": "set_device_plugin_control",
                    "params": {"hash": "00" * 8, "kind": "knob", "slot": 0,
                               "fields": {"steps": 1}}})
    assert cfg.steps == 2


def test_set_device_mode_drives_the_hardware_screen():
    # "who drives who", app side: the RPC needs serial and maps view
    # vocabulary onto the spec 3.3 SET MODE enum
    from athens.protocol.constants import Mode

    svc = _real_service()
    svc.start()
    assert "error" in svc.rpc.handle(
        {"id": 1, "method": "set_device_mode", "params": {"mode": "plugin"}})

    roto = _FakeRoto(None)
    roto.modes = []
    roto.set_mode = lambda m, page=1: roto.modes.append(m)
    svc.roto = roto
    r = svc.rpc.handle({"id": 2, "method": "set_device_mode",
                        "params": {"mode": "plugin"}})
    assert r["result"] == {"mode": "plugin"}
    svc.rpc.handle({"id": 3, "method": "set_device_mode",
                    "params": {"mode": "mix"}})
    assert roto.modes == [Mode.PLUGIN, Mode.MIX]
    assert "error" in svc.rpc.handle(
        {"id": 4, "method": "set_device_mode", "params": {"mode": "banana"}})


def test_serial_leftovers_rpcs():
    # SET PLUGIN NAME / SET SETUP / CLEAR MIDI SETUP / GET CURRENT PLUGIN,
    # all serial-gated and delegating to the client with the right args
    svc = _real_service()
    svc.start()
    for method, params in (
            ("rename_device_plugin", {"hash": "00" * 8, "name": "X"}),
            ("activate_setup", {"index": 3}),
            ("clear_device_setup", {"index": 3}),
            ("get_current_device_plugin", {})):
        r = svc.rpc.handle({"id": 1, "method": method, "params": params})
        assert "error" in r, method              # serial required

    roto = _FakeRoto(None)
    roto.calls = []
    roto.set_plugin_name = lambda h, n: roto.calls.append(("name", h, n))
    roto.select_setup = lambda i: roto.calls.append(("select", i))
    roto.clear_setup = lambda i: roto.calls.append(("clear", i))
    roto.current_plugin = lambda: bytes.fromhex("0102030405060708")
    svc.roto = roto

    svc.rpc.handle({"id": 2, "method": "rename_device_plugin",
                    "params": {"hash": "00" * 8,
                               "name": "A far too long plugin name"}})
    assert roto.calls[-1] == ("name", bytes(8), "A far too lo")   # 12 chars
    svc.rpc.handle({"id": 3, "method": "activate_setup",
                    "params": {"index": 5}})
    svc.rpc.handle({"id": 4, "method": "clear_device_setup",
                    "params": {"index": 6}})
    assert ("select", 5) in roto.calls and ("clear", 6) in roto.calls
    r = svc.rpc.handle({"id": 5, "method": "get_current_device_plugin"})
    assert r["result"] == {"hash": "0102030405060708"}


def test_learn_reference_pairs_with_learned_slot():
    # the bridge reports the DAW's full param identity on learn completion;
    # the serial LEARNED event supplies the slot — the pair lands in the
    # param-refs registry keyed by control
    svc = _real_service()
    svc.start()
    svc._stash_learn_ref({"hash": "aa" * 8, "fx_name": "VST3: Diva",
                          "param_index": 86, "param_name": "Tune 1 Coarse",
                          "value": 0.5, "display": "0.00 st"})
    events = []
    svc.bus.subscribe("device_map_changed", events.append)
    svc._on_device_map_learned(bytes.fromhex("aa" * 8), 0, 3)
    assert svc._param_refs["aa" * 8]["knob:3"]["param_name"] == "Tune 1 Coarse"
    assert svc._pending_learn_ref is None
    assert events and events[-1]["slot"] == 3


def test_settings_rpc_applies_transport_assignments():
    svc = _real_service()
    svc.start()
    r = svc.rpc.handle({"id": 1, "method": "get_settings"})["result"]
    assert r["transport"]["33"] == "rewind"      # defaults present
    applied = []

    class _C:
        def set_transport_assignments(self, m):
            applied.append(dict(m))
    svc.client = _C()
    svc.rpc.handle({"id": 2, "method": "set_settings", "params": {
        "patch": {"transport": {"33": "action:41040", "35": "none"}}}})
    assert applied and applied[-1]["33"] == "action:41040"
    # merged view: overrides + untouched defaults
    r = svc.rpc.handle({"id": 3, "method": "get_settings"})["result"]
    assert r["transport"]["33"] == "action:41040"
    assert r["transport"]["34"] == "fastforward"


def test_system_permission_checks_are_opt_in(monkeypatch):
    # DAW-only users must never be probed; the toggle (or --daw system)
    # switches the checks on. The read-only probe and the grant flow are
    # separate: probing never opens a dialog or a settings pane.
    import athens.daw.system_source as ss
    probes, grants = [], []
    monkeypatch.setattr(ss, "system_permissions",
                        lambda: probes.append(1) or
                        {"pyobjc": True, "accessibility": False,
                         "brightness_cli": False, "identity": {"bundle": True}})
    monkeypatch.setattr(ss, "request_accessibility",
                        lambda: grants.append(1) or
                        {"pyobjc": True, "accessibility": False,
                         "opened_settings": True})
    svc = _real_service()
    svc.start()
    s = svc.rpc.handle({"id": 1, "method": "get_settings"})["result"]
    assert s["system"] == {"enabled": False}
    assert probes == [] and grants == []      # nothing touched macOS
    assert "error" in svc.rpc.handle(         # grant refused while disabled
        {"id": 2, "method": "request_system_permission"})
    assert grants == []

    svc.rpc.handle({"id": 3, "method": "set_settings",
                    "params": {"patch": {"system_control": True}}})
    assert probes == [1]                       # startup-style read-only check
    s = svc.rpc.handle({"id": 4, "method": "get_settings"})["result"]
    assert s["system"]["enabled"] is True
    assert s["system"]["accessibility"] is False
    r = svc.rpc.handle({"id": 5, "method": "request_system_permission"})
    assert r["result"]["opened_settings"] is True
    assert grants == [1]                       # the pane was opened, once


def test_relaunch_only_when_frozen(monkeypatch):
    # relaunch is a packaged-app action; the dev CLI must be told to do it
    # by hand (never hard-exit a developer's shell session)
    svc = _real_service()
    svc.start()
    import sys as _sys
    monkeypatch.setattr(_sys, "frozen", False, raising=False)
    r = svc.rpc.handle({"id": 1, "method": "relaunch_app"})
    assert "error" in r and "packaged" in r["error"]["message"].lower()
