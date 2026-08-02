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


# -- missing roto-bridge pair: a first-class UI condition ---------------------
# Without the pair the Cubase probe can't run, detect_daw silently falls back
# to its default, and the watcher can never adopt Cubase — on Windows (no
# built-in virtual MIDI) that was invisible outside debug logs.

def test_missing_bridge_port_is_surfaced(monkeypatch):
    monkeypatch.setattr("athens.daw.detect.bridge_port_missing", lambda: True)
    svc = _svc("reaper", _FakeDaw("REAPER"))     # no Cubase watcher bound
    events, notices = [], []
    svc.bus.subscribe("bridge_port", events.append)
    svc.bus.subscribe("notice", notices.append)

    svc._check_bridge_port()

    assert svc._bridge_port_missing is True
    assert events and events[0]["missing"] and events[0]["hint"]
    assert notices                       # the live one-shot nudge fired too
    st = svc.rpc.handle({"id": 1, "method": "get_state"})["result"]
    assert st["bridge_port_missing"] is True     # state, so a late-joining UI
    assert st["bridge_port_hint"]                # still sees the remedy


def test_bridge_port_condition_clears_when_pair_appears(monkeypatch):
    monkeypatch.setattr("athens.daw.detect.bridge_port_missing", lambda: True)
    svc = _svc("reaper", _FakeDaw("REAPER"))
    svc._check_bridge_port()
    events = []
    svc.bus.subscribe("bridge_port", events.append)

    monkeypatch.setattr("athens.daw.detect.bridge_port_missing", lambda: False)
    svc._check_bridge_port()             # the user started loopMIDI / IAC

    assert svc._bridge_port_missing is False
    assert events and events[-1]["missing"] is False


def test_bound_watcher_short_circuits_the_port_check(monkeypatch):
    """A bound Cubase watcher PROVES the pair exists — the check must not even
    enumerate (and must never report missing while the port is open)."""
    monkeypatch.setattr(
        "athens.daw.detect.bridge_port_missing",
        lambda: (_ for _ in ()).throw(AssertionError("must not enumerate")))
    svc = _svc("reaper", _FakeDaw("REAPER"))
    svc._cubase = _FakeDaw("Cubase")     # feed_running() -> True: pair is bound

    svc._check_bridge_port()

    assert svc._bridge_port_missing is False


# -- fallback default vs a real detection -------------------------------------

def test_fallback_default_is_not_presented_as_detected(monkeypatch):
    """detect_daw fell back (nothing announced itself): the UI must not claim
    "REAPER detected". A later liveness edge — the DAW really answered — flips
    it, carried on the daw event."""
    monkeypatch.setattr("athens.daw.detect.last_detect_fell_back", True)
    svc = _svc("reaper", _FakeDaw("REAPER"))
    assert svc._daw_detected is False
    st = svc.rpc.handle({"id": 1, "method": "get_state"})["result"]
    assert st["daw_detected"] is False

    events = []
    svc.bus.subscribe("daw", events.append)
    svc._publish_daw_alive(True)         # the feed came up — a real detection

    assert svc._daw_detected is True
    assert events[0]["detected"] is True


def test_swap_marks_the_new_daw_detected(monkeypatch):
    monkeypatch.setattr("athens.daw.detect.last_detect_fell_back", True)
    reaper, cubase = _FakeDaw("REAPER"), _FakeDaw("Cubase")
    svc = _svc("reaper", reaper)
    svc._cubase = cubase
    assert svc._daw_detected is False    # fallback default, nothing live yet

    svc._swap_daw("cubase")              # the monitor CONFIRMED a live Cubase

    assert svc._daw_detected is True


# -- mute-OSC REAPER: the feed beats but the OSC surface never spoke ----------
# The two channels are separate one-time setups; with only the ReaScript done
# the app looks healthy (green off the heartbeat) while the mixer stays empty
# and every knob write is dropped — a whole-integration hole with no signal.

class _MuteOscReaper(_FakeDaw):
    def __init__(self):
        super().__init__("REAPER")
        self.silent = True
        self.osc_bind_error = ""

    def osc_silent(self):
        return self.silent


def test_mute_osc_reaper_is_surfaced_and_clears():
    src = _MuteOscReaper()
    svc = _svc("reaper", src)
    events, notices = [], []
    svc.bus.subscribe("reaper_osc", events.append)
    svc.bus.subscribe("notice", notices.append)

    svc._check_reaper_osc()

    assert svc._reaper_osc_missing is True
    assert events and events[0]["missing"]
    assert "Control/OSC/web" in events[0]["hint"]     # the actionable remedy
    assert notices                       # the live one-shot nudge fired too
    st = svc.rpc.handle({"id": 1, "method": "get_state"})["result"]
    assert st["reaper_osc_missing"] is True           # state, so a late-joining
    assert st["reaper_osc_hint"]                      # UI still sees the remedy

    src.silent = False                   # the first packet arrived
    svc._check_reaper_osc()
    assert svc._reaper_osc_missing is False
    assert events[-1]["missing"] is False


def test_mute_osc_bind_error_names_the_port_fight():
    # when WE failed to bind UDP 8000, "add the OSC surface" is the wrong
    # remedy — the hint must name the port squatter problem instead
    src = _MuteOscReaper()
    src.osc_bind_error = "[WinError 10048] only one usage of each socket"
    svc = _svc("reaper", src)
    svc._check_reaper_osc()
    assert "10048" in svc._reaper_osc_hint
    assert "Control/OSC/web" not in svc._reaper_osc_hint


def test_mute_osc_check_ignores_non_reaper():
    src = _MuteOscReaper()
    svc = _svc("cubase", src)            # active DAW isn't REAPER: not its story
    svc._check_reaper_osc()
    assert svc._reaper_osc_missing is False


def test_explicit_mode_tick_runs_diagnostics_but_never_swaps(monkeypatch):
    """--daw reaper: the monitor still pulses the standing diagnostics (it is
    the app's only periodic service-side check — the mute-OSC hole exists in
    every mode), but an explicit choice must never hot-swap the source."""
    from athens.api.service import BridgeService
    src = _MuteOscReaper()
    svc = BridgeService(source=src, daw="reaper")
    svc._active_daw = "reaper"
    monkeypatch.setattr("athens.daw.detect.reaper_feed_live", lambda: True)

    svc._monitor_tick()

    assert svc._reaper_osc_missing is True            # diagnostics ran
    assert svc.source is src and svc._active_daw == "reaper"   # no swap


# -- Locate on a portable REAPER heals the RUNNING feed too -------------------

def test_reaper_locate_retargets_the_running_feed(monkeypatch, tmp_path):
    """Settings ▸ Locate (portable REAPER): the scripts land in the new folder
    AND the active source's feed follows the new IPC dir — without the
    retarget it kept polling the old, forever-dead dir until an app relaunch
    (REAPER running, script running, Athens 'closed': the silent dead end)."""
    from athens.daw import script_install as si
    monkeypatch.setattr(si, "_config_dir", lambda: tmp_path / "cfg")
    portable = tmp_path / "PortableReaper"
    portable.mkdir()

    class _Src(_FakeDaw):
        def __init__(self):
            super().__init__("REAPER")
            self.retargets = 0

        def retarget_feed(self, directory=None):
            self.retargets += 1

    src = _Src()
    svc = _svc("reaper", src)
    svc.set_script_override("reaper", str(portable))

    assert src.retargets == 1
    assert (portable / "Scripts" / si.REAPER_SCRIPT).is_file()
    from athens.daw.fx_feed import default_feed_dir
    assert default_feed_dir() == portable / "roto-reaper"   # detection follows
