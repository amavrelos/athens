"""Smoke test for shell.launch — it needs pywebview so it has no other
coverage; this guards its setup path against stray names / regressions (the
demo removal left a NameError on 'demo' that only showed at runtime)."""
import sys
import types
import urllib.error
import urllib.request

from athens.ui import shell


def test_launch_setup_path_has_no_stray_names(monkeypatch, tmp_path):
    # fake pywebview: create_window -> a window with .events; start() no-op so
    # launch runs its whole body instead of blocking on the native loop
    ev = types.SimpleNamespace()
    for e in ("closing", "closed"):
        setattr(ev, e, types.SimpleNamespace(__iadd__=lambda *_a: None))
    win = types.SimpleNamespace(events=ev)
    opened = []
    fake_webview = types.ModuleType("webview")
    fake_webview.create_window = lambda _title, url, **k: opened.append(url) or win
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
    # the page must load over loopback HTTP, never file:// — a file:// document
    # can't carry the query string through the Windows webview's URL parsing,
    # which is what surfaced as ERR_FILE_NOT_FOUND. The UI port trails the API
    # port, and must stay stable so the page keeps its localStorage.
    assert opened == [opened[0]] and opened[0].startswith("http://127.0.0.1:8800/")
    assert "/index.html?b=" in opened[0]
    assert "ws=ws://127.0.0.1:8799" in opened[0]


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")


def test_web_server_serves_the_ui_with_explicit_mime_types():
    port, shutdown = _serve_or_skip()
    try:
        # types MUST NOT come from the platform mimetypes db: Windows resolves
        # them through the registry, where a text/plain .css is dropped on the
        # floor by Chromium's strict style-sheet MIME check
        page = _get(port, "/index.html?b=1&ws=ws://127.0.0.1:8765&view=diag")
        assert page.status == 200
        assert page.headers["Content-Type"] == "text/html; charset=utf-8"
        assert page.headers["Cache-Control"] == "no-store"
        assert _get(port, "/app.js").headers["Content-Type"] == \
            "text/javascript; charset=utf-8"
        assert _get(port, "/app.css").headers["Content-Type"] == \
            "text/css; charset=utf-8"
    finally:
        shutdown()


def test_web_server_does_not_serve_outside_the_web_dir():
    # shell.py sits one level ABOVE the web dir, so a traversal that worked would
    # answer 200 with source — a 404 on a path that exists is the real assertion
    assert (shell.WEB_DIR.parent / "shell.py").exists()
    port, shutdown = _serve_or_skip()
    try:
        for escape in ("/../shell.py", "/..%2Fshell.py", "/%2e%2e/shell.py"):
            try:
                body = _get(port, escape).read()
                raise AssertionError(f"{escape} served {len(body)} bytes")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, escape
    finally:
        shutdown()


def test_web_server_falls_back_to_an_ephemeral_port_when_the_wanted_one_is_taken():
    held, stop_held = _serve_or_skip()         # squat on the preferred port
    try:
        port, shutdown = shell._serve_web(shell.WEB_DIR, held)
        try:
            assert port and port != held       # a window still opens
        finally:
            shutdown()
    finally:
        stop_held()


def test_web_server_binds_without_reverse_dns(monkeypatch):
    # http.server's own server_bind() calls socket.getfqdn() on the bind address.
    # That is a reverse DNS lookup, and on a machine with no resolver for
    # 127.0.0.1 it blocks past launch()'s 20s watchdog, killing the app before
    # the window opens (CI caught exactly this on macOS). Nothing may resolve.
    import socket

    def boom(*_a):
        raise AssertionError("reverse DNS lookup on the bind path")

    monkeypatch.setattr(socket, "getfqdn", boom)
    port, shutdown = shell._serve_web(shell.WEB_DIR, 0)
    try:
        assert port and _get(port, "/index.html").status == 200
    finally:
        shutdown()


def _serve_or_skip(want=0):
    port, shutdown = shell._serve_web(shell.WEB_DIR, want)
    assert port, "could not bind the loopback web server"
    return port, shutdown
