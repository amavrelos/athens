"""System adapter — the Mac as a 'DAW'. All OS calls stubbed; no polling."""
from athens.daw.system_source import MacController, SystemSource
from athens.sysex.constants import TransportAction


class _FakeCursor:
    available = True

    def __init__(self):
        self.deltas = []

    def adjust(self, delta):
        self.deltas.append(delta)


def make(volume="50", brightness=None):
    calls = []

    def run(cmd):
        calls.append(cmd)
        if "get volume settings" in cmd[-1]:
            return volume
        return ""

    mac = MacController(run=run, enable_hid=False)
    mac.brightness_cli = brightness           # None = not installed
    src = SystemSource(controller=mac, cursor=_FakeCursor(), poll=False)
    return src, calls


def test_volume_param_and_strip_drive_the_os():
    src, calls = make()
    assert src.tracks()[0].name == "Sys Vol"
    assert src.tracks()[0].volume == 0.5      # readback at construction
    src.set_device_param(0, SystemSource.PARAM_VOLUME, 0.8)
    assert ["osascript", "-e", "set volume output volume 80"] in calls
    src.set_track_volume(0, 0.25)             # mix strip, same control
    assert ["osascript", "-e", "set volume output volume 25"] in calls
    p = src.device_params(0)[SystemSource.PARAM_VOLUME]
    assert p.display == "25 %"


def test_brightness_only_when_cli_present():
    src, _ = make()
    assert len(src.tracks()) == 1             # no Display strip without CLI
    assert src.device_params(0)[1].display == "n/a"


def test_cursor_knob_sends_relative_deltas():
    src, _ = make()
    src.set_device_param(0, SystemSource.PARAM_CURSOR, 0.6)
    src.set_device_param(0, SystemSource.PARAM_CURSOR, 0.7)
    assert src.cursor.deltas == [
        0.6 - 0.5, 0.7 - 0.6]                  # deltas from last position
    src._do_recenter()                         # the spring-return
    assert src.device_params(0)[SystemSource.PARAM_CURSOR].value == 0.5


def test_transport_maps_to_media_keys():
    src, calls = make()
    src.transport_action(TransportAction.PLAY, True)
    src.transport_action(TransportAction.FASTFORWARD, True)
    src.transport_action(TransportAction.REWIND, False)   # release: ignored
    probes = [c for c in calls if "is running" in c[-1]]
    assert len(probes) >= 2                    # playpause + next attempted


def test_custom_controls_run_with_value_substitution(tmp_path):
    cfg = tmp_path / "system-controls.json"
    cfg.write_text('[{"name": "Zoom", "kind": "shell",'
                   ' "set": "echo {value}"}]')
    calls = []
    mac = MacController(run=lambda cmd: calls.append(cmd) or "50",
                        enable_hid=False)
    mac.brightness_cli = None
    src = SystemSource(controller=mac, cursor=_FakeCursor(),
                       controls_path=cfg, poll=False)
    params = src.device_params(0)
    assert params[3].name == "Zoom"
    src.set_device_param(0, 3, 0.4)
    assert ["sh", "-c", "echo 40"] in calls


def test_request_accessibility_opens_settings_pane(monkeypatch):
    # the grant flow must open the settings pane (reliable from any thread),
    # not depend on the one-time consent dialog — and never crash on the
    # best-effort register
    import athens.daw.system_source as ss
    opened = []
    monkeypatch.setattr(ss, "_register_post_event_access", lambda: None)
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda cmd, **kw: opened.append(cmd))
    status = ss.request_accessibility()
    assert opened and opened[0][0] == "open"
    assert "Privacy_Accessibility" in opened[0][1]
    assert status["opened_settings"] is True
    assert "identity" in status              # UI needs the app identity


def test_permissions_probe_is_read_only(monkeypatch):
    # the plain probe must never open a pane or request access
    import athens.daw.system_source as ss
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("probe opened a subprocess")))
    out = ss.system_permissions()
    assert "accessibility" in out and "identity" in out


def test_app_identity_derives_bundle_name(monkeypatch):
    # when frozen, the TCC entry name must come from the .app so a rename
    # (Athens) shows correctly; when not, it's the bare executable
    import athens.daw.system_source as ss

    monkeypatch.setattr(ss.sys, "frozen", True, raising=False)
    monkeypatch.setattr(ss.sys, "executable",
                        "/Applications/Athens.app/Contents/MacOS/Athens",
                        raising=False)
    idn = ss._app_identity()
    assert idn["bundle"] is True and idn["name"] == "Athens"
    # compare as Path — on Windows str(Path(...)) renders backslashes
    from pathlib import Path
    assert Path(idn["path"]) == \
        Path("/Applications/Athens.app/Contents/MacOS/Athens")

    monkeypatch.setattr(ss.sys, "frozen", False, raising=False)
    monkeypatch.setattr(ss.sys, "executable", "/venv/bin/python", raising=False)
    assert ss._app_identity()["name"] == "python"
    assert ss._app_identity()["bundle"] is False


def test_cursor_spring_return_notifies_the_device():
    # the recenter must push the value back to the app/motor (0.5) so the
    # absolute knob visibly springs to centre after acting as a relative encoder
    src, _ = make()
    events = []
    src.on_device_param_value = lambda dev, p, v, disp: events.append((p, v))
    src.set_device_param(0, SystemSource.PARAM_CURSOR, 0.8)
    src._do_recenter()
    assert (SystemSource.PARAM_CURSOR, 0.5) in events


def test_custom_applescript_branch_runs_osascript(tmp_path):
    cfg = tmp_path / "system-controls.json"
    cfg.write_text('[{"name": "Keynote", "kind": "applescript",'
                   ' "set": "set x to {value}"}]')
    calls = []
    mac = MacController(run=lambda cmd: calls.append(cmd) or "", enable_hid=False)
    mac.brightness_cli = None
    src = SystemSource(controller=mac, cursor=_FakeCursor(),
                       controls_path=cfg, poll=False)
    src.set_device_param(0, 3, 0.75)
    assert ["osascript", "-e", "set x to 75"] in calls
