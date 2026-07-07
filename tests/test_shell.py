"""Smoke test for shell.launch — it needs pywebview so it has no other
coverage; this guards its setup path against stray names / regressions (the
demo removal left a NameError on 'demo' that only showed at runtime)."""
import sys
import types

from athens.ui import shell


def test_launch_setup_path_has_no_stray_names(monkeypatch, tmp_path):
    # fake pywebview: create_window -> a window with .events; start() no-op so
    # launch runs its whole body instead of blocking on the native loop
    ev = types.SimpleNamespace()
    for e in ("closing", "closed"):
        setattr(ev, e, types.SimpleNamespace(__iadd__=lambda *_a: None))
    win = types.SimpleNamespace(events=ev)
    fake_webview = types.ModuleType("webview")
    fake_webview.create_window = lambda *a, **k: win
    fake_webview.start = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    # stay offline: no real service / ws server / teardown observer / log file
    monkeypatch.setattr(shell, "_setup_logging", lambda: tmp_path / "athens.log")
    monkeypatch.setattr(shell, "BridgeService",
                        lambda **k: types.SimpleNamespace(start=lambda: None,
                                                          stop=lambda: None))
    monkeypatch.setattr(shell, "serve", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_library_path", lambda: tmp_path / "lib.json")
    monkeypatch.setattr(shell, "_install_terminate_observer", lambda cb: None)
    monkeypatch.setattr(shell, "_set_dock_icon", lambda: None)   # no AppKit
    monkeypatch.setattr(shell, "_enable_ctrl_c", lambda *_a: None)  # no signals

    # the cubase source is an inert scaffold -> exercises the source branch
    assert shell.launch(daw="cubase", host="127.0.0.1", port=8799) == 0
