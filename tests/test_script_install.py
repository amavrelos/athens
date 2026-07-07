"""Auto-install of the DAW companion scripts — offline, all writes to tmp."""
from __future__ import annotations

from athens.daw import script_install as si


def test_write_if_changed_install_update_skip(tmp_path):
    dest = tmp_path / "sub" / "script.js"
    assert si._write_if_changed(b"one", dest) == "installed"   # missing -> write
    assert dest.read_bytes() == b"one"
    assert si._write_if_changed(b"one", dest) is None          # unchanged -> skip
    assert si._write_if_changed(b"two", dest) == "updated"     # differs -> rewrite
    assert dest.read_bytes() == b"two"


def test_sync_cubase_installs_then_idempotent(tmp_path, monkeypatch):
    root = tmp_path / "Steinberg" / "Cubase" / "MIDI Remote" / "Driver Scripts"
    root.mkdir(parents=True)
    monkeypatch.setattr(si, "_steinberg_driver_roots", lambda: [root])
    monkeypatch.setattr(si, "_bundled",
                        lambda sub, name: b"// script" if sub == "cubase" else None)
    assert si.sync_cubase() == ["Cubase script installed"]
    dest = root / "Local" / si.CUBASE_VENDOR / si.CUBASE_DEVICE / si.CUBASE_SCRIPT
    assert dest.read_bytes() == b"// script"
    assert si.sync_cubase() == []                              # already current


def test_sync_reaper_installs_to_scripts(tmp_path, monkeypatch):
    resource = tmp_path / "REAPER"
    resource.mkdir()
    monkeypatch.setattr(si, "reaper_resource_dir", lambda: resource)
    monkeypatch.setattr(si, "_bundled",
                        lambda sub, name: b"-- lua" if sub == "reaper" else None)
    assert si.sync_reaper() == ["REAPER script installed",
                                "REAPER toggle installed"]
    assert (resource / "Scripts" / si.REAPER_SCRIPT).read_bytes() == b"-- lua"
    # the one-click start/stop toggle deploys through this SAME (override-aware)
    # path — it used to live in a second installer that ignored Locate
    assert (resource / "Scripts" / si.REAPER_TOGGLE).read_bytes() == b"-- lua"


def test_sync_reaper_skips_when_reaper_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "reaper_resource_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(si, "_bundled", lambda sub, name: b"-- lua")
    assert si.sync_reaper() == []                              # no resource dir


def test_sync_mode_gating(monkeypatch):
    calls = []
    monkeypatch.setattr(si, "sync_reaper", lambda force=False: calls.append("r") or ["r"])
    monkeypatch.setattr(si, "sync_cubase", lambda force=False: calls.append("c") or ["c"])
    assert si.sync("reaper") == ["r"] and calls == ["r"]      # reaper only
    calls.clear()
    assert si.sync("cubase") == ["c"] and calls == ["c"]      # cubase only
    calls.clear()
    assert si.sync("auto") == ["r", "c"] and calls == ["r", "c"]   # both
    calls.clear()
    assert si.sync(None) == ["r"]                             # default = reaper


def test_reinstall_force_rewrites_identical_file(tmp_path, monkeypatch):
    # the Reinstall/repair button: sync(..., force=True) re-writes even a
    # byte-identical script (plain sync would skip it as "already current")
    resource = tmp_path / "REAPER"
    resource.mkdir()
    monkeypatch.setattr(si, "reaper_resource_dir", lambda: resource)
    monkeypatch.setattr(si, "_bundled",
                        lambda sub, name: b"-- lua" if sub == "reaper" else None)
    assert si.sync("reaper")                                  # first: installs
    assert si.sync("reaper") == []                            # unchanged -> skip
    assert si.sync("reaper", force=True) == ["REAPER script reinstalled",
                                             "REAPER toggle reinstalled"]


def test_override_persists_and_resolver_uses_it(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_config_dir", lambda: tmp_path / "cfg")
    located = tmp_path / "weird" / "Driver Scripts"       # auto-discovery misses it
    located.mkdir(parents=True)
    si.set_override("cubase", str(located))
    assert si._overrides()["cubase"] == str(located)
    assert si._steinberg_driver_roots() == [located]      # resolver honors override
    si.set_override("cubase", None)                        # clear it
    assert "cubase" not in si._overrides()


def test_override_descends_into_host_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_config_dir", lambda: tmp_path / "cfg")
    driver = tmp_path / "SomeHost" / "MIDI Remote" / "Driver Scripts"
    driver.mkdir(parents=True)
    si.set_override("cubase", str(tmp_path / "SomeHost"))  # user picked the host dir
    assert si._steinberg_driver_roots() == [driver]        # descended one level


def test_reaper_override_used_for_portable_install(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_config_dir", lambda: tmp_path / "cfg")
    portable = tmp_path / "PortableReaper"
    portable.mkdir()
    monkeypatch.setattr(si, "_bundled",
                        lambda sub, name: b"-- lua" if sub == "reaper" else None)
    si.set_override("reaper", str(portable))
    assert si.sync_reaper() == ["REAPER script installed",
                                "REAPER toggle installed"]
    assert (portable / "Scripts" / si.REAPER_SCRIPT).read_bytes() == b"-- lua"
    assert (portable / "Scripts" / si.REAPER_TOGGLE).read_bytes() == b"-- lua"


def test_status_reports_located_and_found(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_config_dir", lambda: tmp_path / "cfg")
    driver = tmp_path / "DS"
    driver.mkdir()
    si.set_override("cubase", str(driver))
    st = si.status()
    assert st["cubase"]["located"] and st["cubase"]["found"]
    assert st["cubase"]["path"] == str(driver)


def test_real_bundled_scripts_present():
    # the resolver must locate the scripts Athens actually ships (guards the
    # repo layout / PyInstaller --add-data paths from silently drifting)
    assert si._bundled("cubase", si.CUBASE_SCRIPT) is not None
    assert si._bundled("reaper", si.REAPER_SCRIPT) is not None
    assert si._bundled("reaper", si.REAPER_TOGGLE) is not None
