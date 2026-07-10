"""REAPER honest liveness (combined feed-heartbeat OR recent OSC).

The phantom-REAPER bug: REAPER open with OSC but the Lua feed not loaded gives
tracks over OSC, then REAPER quits — the feed never beat, so its 'never declare
gone before the first beat' guard left the DAW stuck 'alive' with stale tracks.
The OSC-liveness poll is what catches that.
"""
from athens.daw import reaper_source as rs


def _src(monkeypatch, t0):
    """A feed-less source with a controllable monotonic clock, driven manually
    (no Timer threads: _live_stop is pre-set so _live_poll won't reschedule)."""
    clock = {"t": t0}
    monkeypatch.setattr(rs.time, "monotonic", lambda: clock["t"])
    src = rs.ReaperSysexSource(enable_fx_feed=False)
    src._live_stop.set()
    src._live_start = clock["t"]
    return src, clock


def test_osc_only_reaper_quit_clears_phantom(monkeypatch):
    src, clock = _src(monkeypatch, 1000.0)
    edges = []
    src.on_daw_alive = lambda a: edges.append(a)

    # REAPER open, OSC arriving: tracks appear, stays alive (optimistic True)
    src._seen_osc()
    src._names[0] = "Drums"; src._volumes[0] = 0.5
    src._live_poll()                     # within grace + fresh OSC
    assert edges == []                   # no spurious edge
    assert len(src.tracks()) == 1

    # REAPER quits: OSC goes silent. Past grace + past the OSC-gone window ->
    # a single 'gone' edge, and the phantom tracks are forgotten.
    clock["t"] += 100.0
    src._live_poll()
    assert edges == [False]
    assert src.tracks() == []

    # REAPER returns: first OSC re-lights it immediately (no wait for the poll)
    src._seen_osc()
    assert edges == [False, True]


def test_feed_heartbeat_keeps_alive_without_osc(monkeypatch):
    """The feed path still works: a beating heartbeat holds 'alive' with zero
    OSC, and only when BOTH feed and OSC are silent does it blank — so a stopped
    feed under a still-open (OSC-live) REAPER would NOT wrongly blank."""
    src, clock = _src(monkeypatch, 500.0)
    edges = []
    src.on_daw_alive = lambda a: edges.append(a)

    src._on_feed_daw_alive(True)         # heartbeat; no OSC at all
    clock["t"] += 100.0
    src._live_poll()
    assert edges == []                   # feed alone keeps it alive

    src._on_feed_daw_alive(False)        # heartbeat gone AND osc never seen
    assert edges == [False]


def test_grace_suppresses_early_gone(monkeypatch):
    """A source that just started must not blank before REAPER can speak."""
    src, clock = _src(monkeypatch, 0.0)
    edges = []
    src.on_daw_alive = lambda a: edges.append(a)

    clock["t"] += rs._LIVE_GRACE_S - 0.5   # still inside the grace
    src._live_poll()
    assert edges == []                     # silence tolerated during grace
