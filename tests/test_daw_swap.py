"""Runtime DAW hot-swap with the persistent Cubase watcher (--daw auto).

The watcher is a Cubase source opened at startup (before the device port floods)
so the monitor can confirm Cubase on its OWN port and ADOPT it — opening a MIDI
port at runtime would deadlock CoreMIDI against the live device port. Adopting
must reuse that one object (never re-open, never stop it on swap-away).
"""
from athens.api.service import BridgeService
from athens.daw.source import MockSysexSource


class _FakeDaw(MockSysexSource):
    def __init__(self, name):
        super().__init__()
        self.DAW_NAME = name
        self.started = self.stopped = self.refreshed = 0
        self.alive = True

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def refresh_state(self):
        self.refreshed += 1

    def check_alive(self, timeout=0.6):
        return self.alive

    def feed_running(self):
        return True             # watcher's roto-bridge port is bound (no rebind)


def _svc(active, source):
    svc = BridgeService(source=source, daw="auto")
    svc._active_daw = active
    svc.bridge = None
    return svc


def test_swap_adopts_cubase_watcher_no_reopen():
    reaper, cubase = _FakeDaw("REAPER"), _FakeDaw("Cubase")
    svc = _svc("reaper", reaper)
    svc._cubase = cubase                 # watcher already open from startup

    svc._swap_daw("cubase")

    assert svc.source is cubase          # adopted the SAME object, no re-open
    assert svc._active_daw == "cubase"
    assert cubase.started == 0           # never re-started (already running)
    assert cubase.stopped == 0           # kept alive
    assert cubase.refreshed == 1         # re-announced its state on adopt
    assert reaper.stopped == 1           # the old source is torn down


def test_swap_away_from_cubase_keeps_watcher(monkeypatch):
    cubase, reaper2 = _FakeDaw("Cubase"), _FakeDaw("REAPER")
    monkeypatch.setattr("athens.daw.detect.make_source", lambda d: reaper2)
    svc = _svc("cubase", cubase)
    svc._cubase = cubase                 # cubase is BOTH active and the watcher

    svc._swap_daw("reaper")

    assert svc.source is reaper2
    assert svc._active_daw == "reaper"
    assert reaper2.started == 1
    assert cubase.stopped == 0           # watcher survives as the standby


def test_monitor_swaps_dead_reaper_to_live_cubase(monkeypatch):
    reaper, cubase = _FakeDaw("REAPER"), _FakeDaw("Cubase")
    monkeypatch.setattr("athens.daw.detect.reaper_feed_live", lambda *a: False)
    svc = _svc("reaper", reaper)
    reaper.alive = False                 # REAPER really gone: no feed, no OSC
    svc._cubase = cubase                 # live watcher

    svc._monitor_tick()

    assert svc._active_daw == "cubase"   # dead REAPER -> follow the live Cubase
    assert svc.source is cubase


def test_monitor_never_yanks_an_osc_only_live_reaper(monkeypatch):
    """Feed heartbeat cold (Lua script not loaded) but the SOURCE says live
    (OSC flowing): the monitor must stay put. Judging REAPER by the heartbeat
    file alone yanked live OSC-only sessions over to a background Cubase."""
    reaper, cubase = _FakeDaw("REAPER"), _FakeDaw("Cubase")
    monkeypatch.setattr("athens.daw.detect.reaper_feed_live", lambda *a: False)
    svc = _svc("reaper", reaper)         # reaper.alive stays True (OSC fresh)
    svc._cubase = cubase                 # Cubase live in the background

    svc._monitor_tick()

    assert svc._active_daw == "reaper"   # live session never yanked
    assert reaper.stopped == 0


def test_monitor_never_yanks_a_live_reaper(monkeypatch):
    reaper, cubase = _FakeDaw("REAPER"), _FakeDaw("Cubase")
    monkeypatch.setattr("athens.daw.detect.reaper_feed_live", lambda *a: True)
    svc = _svc("reaper", reaper)
    svc._cubase = cubase                 # Cubase is live too...

    svc._monitor_tick()

    assert svc._active_daw == "reaper"   # ...but a LIVE REAPER stays put
    assert reaper.stopped == 0


def test_monitor_dead_cubase_falls_back_to_reaper(monkeypatch):
    cubase, reaper2 = _FakeDaw("Cubase"), _FakeDaw("REAPER")
    cubase.alive = False                 # Cubase quit
    monkeypatch.setattr("athens.daw.detect.reaper_feed_live", lambda *a: True)
    monkeypatch.setattr("athens.daw.detect.make_source", lambda d: reaper2)
    svc = _svc("cubase", cubase)
    svc._cubase = cubase

    svc._monitor_tick()

    assert svc._active_daw == "reaper"
    assert svc.source is reaper2


def test_watch_reuses_active_source_when_already_cubase(monkeypatch):
    cubase = _FakeDaw("Cubase")
    svc = _svc("cubase", cubase)
    # make_source must NOT be called: the active source already IS the watcher
    monkeypatch.setattr(
        "athens.daw.detect.make_source",
        lambda d: (_ for _ in ()).throw(AssertionError("should not make one")))

    svc._start_cubase_watch()

    assert svc._cubase is cubase
