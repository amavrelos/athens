"""Launch the roto-reaper desktop app: service + WS API + pywebview window."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from ..api.service import BridgeService
from ..api.ws import serve

log = logging.getLogger(__name__)


APP_NAME = "Athens"          # the product/app name (bundle, window, TCC entry)


def log_dir() -> Path:
    """Where Athens writes its log file, per platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Logs/Athens"
    if sys.platform.startswith("win"):        # pragma: no cover
        import os
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Athens/logs"
    return Path.home() / ".local/state/athens"   # pragma: no cover


def log_path() -> Path:
    return log_dir() / "athens.log"


def _setup_logging() -> Path:
    """Give the packaged .app a real log FILE — its stderr is discarded by macOS
    and shell.launch() bypasses cli's basicConfig, so without this every log line
    is lost. Root stays at INFO (python-osc floods DEBUG with every unmatched OSC
    address); our `athens` tree runs at DEBUG so device frames (D->H) and gesture
    handlers are captured. ATHENS_LOG_LEVEL=INFO quiets our tree; the file rotates
    so it can't grow without bound."""
    import os
    from logging.handlers import RotatingFileHandler

    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "athens.log"
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Add each handler only if absent, so this composes with cli.main()'s
    # basicConfig (running `roto-reaper -v ui` from a terminal) without
    # double-printing every line or stacking file handlers on a re-launch.
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        sh = logging.StreamHandler()          # useful under `athens run` / -v
        sh.setFormatter(fmt)
        root.addHandler(sh)
    ours = os.environ.get("ATHENS_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("athens").setLevel(getattr(logging, ours, logging.DEBUG))
    return path


def _install_terminate_observer(callback):
    """Run `callback` when the macOS app is about to terminate (Cmd+Q, Quit
    menu, dock Quit) — the one exit path that skips both the window-close
    events and Python's `finally`. Returns the observer token (keep a
    reference so it isn't GC'd). No-op off macOS / without pyobjc."""
    try:
        from Foundation import NSNotificationCenter
        nc = NSNotificationCenter.defaultCenter()
        return nc.addObserverForName_object_queue_usingBlock_(
            "NSApplicationWillTerminateNotification", None, None,
            lambda _note: callback())
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        log.debug("could not install terminate observer: %s", exc)
        return None


def _web_dir() -> Path:
    if getattr(sys, "frozen", False):    # PyInstaller .app bundle
        return Path(sys._MEIPASS) / "athens/ui/web"
    return Path(__file__).parent / "web"


WEB_DIR = _web_dir()
DEFAULT_WINDOW = (1200, 760)
MIN_WINDOW = (960, 640)


def _library_path():
    """Persistent library location — the library is the archive (firmware
    updates can wipe the device's stored maps)."""
    from ..config import config_dir
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "library.json"


def _set_dock_icon() -> None:
    """The CLI runs as the python process, so CMD+Tab and the Dock show python's
    icon (the packaged .app carries Athens.icns and is unaffected). Point the
    running app's icon at the bundled PNG so dev sessions look right too.
    macOS-only, best-effort — a missing pyobjc/AppKit just skips it."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        # athens.png is already inset to macOS' ~80% icon proportions (padded by
        # scripts/pad_iconset, same as the .icns), so set it as-is.
        img = NSImage.alloc().initWithContentsOfFile_(str(WEB_DIR / "athens.png"))
        if img is not None and img.isValid():
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception as exc:      # noqa: BLE001 - pyobjc absent / headless
        log.debug("could not set Dock icon: %s", exc)


def _enable_ctrl_c(stop) -> None:
    """pywebview's native Cocoa loop blocks the main thread, so Python never
    gets to run its SIGINT handler — Ctrl-C in the launching terminal does
    nothing. Install handlers that quit cleanly (stop() blanks the device;
    NSApp.terminate_ also fires our willTerminate observer), plus a periodic
    no-op timer so the run loop hands control back to Python often enough to
    actually deliver the signal. macOS-only, best-effort."""
    if sys.platform != "darwin":
        return
    try:
        import signal
        from AppKit import NSApplication
        from Foundation import NSTimer

        def _quit(*_a):
            # Hard-exit backstop: if graceful teardown ever wedges (e.g. a
            # CoreMIDI close that won't return), SIGALRM's DEFAULT action
            # terminates the process at the OS level — it fires even while the
            # GIL is held, so the app ALWAYS exits within a few seconds instead
            # of hanging unkillably. Cancelled implicitly when the process exits.
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(4)
            try:
                stop()
            finally:
                NSApplication.sharedApplication().terminate_(None)
        signal.signal(signal.SIGINT, _quit)
        signal.signal(signal.SIGTERM, _quit)
        # ~3x/sec tick back into the interpreter so a pending Ctrl-C is
        # delivered promptly instead of stalling until the window closes
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            0.3, True, lambda _t: None)
    except Exception as exc:      # noqa: BLE001 - pyobjc absent / not main thread
        log.debug("Ctrl-C handler not installed: %s", exc)


class _LocateApi:
    """Exposed to the page as pywebview.api.* — the native folder picker behind
    the 'Locate' buttons (a file:// page on the WS API can't open a native
    dialog). Runs in-process, so it drives the service directly."""

    def __init__(self, service):
        self._service = service
        self.window = None            # set right after the window is created

    def pick_daw_folder(self, daw):
        """Open a native folder picker; on choose, point that DAW's script
        install at it and drop the script in. Returns status + notes."""
        if self.window is None:
            return {"error": "no window"}
        import webview
        picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return {"cancelled": True}
        folder = picked[0] if isinstance(picked, (list, tuple)) else picked
        result = self._service.set_script_override(daw, folder)
        result["picked"] = folder
        return result

    def clear_daw_folder(self, daw):
        """Forget a Located folder and fall back to auto-discovery."""
        return self._service.set_script_override(daw, None)

    def get_script_paths(self):
        from athens.daw import script_install
        return script_install.status()


def launch(host: str = "127.0.0.1", port: int = 8765,
           view: str = None, daw: str = "auto") -> int:
    _log_file = _setup_logging()
    # Windowed builds discard stderr, so an unhandled exception in a background
    # thread (WS API bind, auto-connect, liveness pollers) would vanish without a
    # trace. Route both excepthooks into the app log.
    def _log_thread_crash(args):
        log.critical("unhandled exception in thread %r",
                     getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _log_thread_crash
    _orig_excepthook = sys.excepthook
    def _log_crash(exc_type, exc, tb):
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        _orig_excepthook(exc_type, exc, tb)
    sys.excepthook = _log_crash
    daw_mode = daw                    # remember "auto" so the runtime monitor runs
    if daw == "auto":
        from ..daw.detect import detect_daw
        daw = detect_daw()
    log.info("=== Athens starting (daw=%s) — logging to %s ===",
             daw, _log_file)
    try:
        import webview  # pywebview; lazy so the package works without the ui extra
    except ImportError:
        print('pywebview not installed — run:  pip install -e ".[ui]"')
        return 2

    # startup-hang watchdog: if we wedge before the window is up (e.g. a MIDI
    # deadlock inside service.start, before the Ctrl-C net is armed), dump every
    # thread's stack and bail rather than hang un-interruptibly. Cancelled once
    # startup completes, just below.
    import faulthandler
    faulthandler.dump_traceback_later(20, exit=True)

    from ..daw.detect import make_source
    source = make_source(daw)
    service = BridgeService(source=source, daw=daw_mode,
                            library_path=_library_path(), auto_connect=True)
    service.start()
    threading.Thread(target=serve, args=(service, host, port),
                     daemon=True, name="roto-api").start()

    # Blank the device on exit so it never keeps showing a ghost session.
    # Python signal handlers do NOT run while blocked in pywebview's native
    # Cocoa loop, and neither the `finally` below nor the window-close events
    # fire on Cmd+Q (that calls NSApplication terminate:, tearing down the app,
    # not the window). The reliable catch-all is the NSApplicationWillTerminate
    # notification — it fires synchronously on the main thread for Cmd+Q, the
    # Quit menu, and app-level termination, before the process exits. finally +
    # atexit + window events stay as backstops; service.stop() is idempotent.
    import atexit
    atexit.register(service.stop)
    _observers = _install_terminate_observer(service.stop)

    index = WEB_DIR / "index.html"
    # per-launch cache-buster: WKWebView caches file:// documents by URL, so a
    # same-URL relaunch can serve a STALE page. A unique query forces a fresh load.
    import time
    bust = int(time.time())
    url = index.as_uri() + f"?b={bust}&ws=ws://{host}:{port}"
    if view:
        url += f"&view={view}"
    locate_api = _LocateApi(service)
    window = webview.create_window(
        APP_NAME, url,
        width=DEFAULT_WINDOW[0], height=DEFAULT_WINDOW[1],
        min_size=MIN_WINDOW,
        js_api=locate_api,
    )
    locate_api.window = window
    # primary teardown: fires on the GUI thread when the user closes the
    # window, even when webview.start() never returns to the finally
    for _evt in ("closing", "closed"):
        try:
            getattr(window.events, _evt).__iadd__(lambda *_a: service.stop())
        except Exception:            # older pywebview / event unavailable
            log.debug("pywebview '%s' event unavailable", _evt)
    log.info("UI window opening (api ws://%s:%d)", host, port)
    _enable_ctrl_c(service.stop)  # Ctrl-C in the terminal quits cleanly
    _set_dock_icon()             # dev/CLI runs as python; give it Athens' icon
    faulthandler.cancel_dump_traceback_later()   # window is up — startup survived
    try:
        webview.start()          # blocks until the window closes
    finally:
        service.stop()
    return 0
