"""ReaperFxFeed (file IPC with the roto_fx_feed.lua ReaScript) and its
integration into ReaperSysexSource: the chain snapshot becomes the plugin-mode
device/param inventory, touches feed learn mode, watched values stream, and
watch.txt round-trips. No REAPER involved: files are written by the tests and
the feed callbacks are driven synchronously."""
import json
import os
import time

from athens.daw.fx_feed import ReaperFxFeed
from athens.daw.reaper_source import ReaperSysexSource

# strictly-increasing fake mtimes for re-written feed files: the feed re-reads
# only when st_mtime_ns changes, and Windows file times are coarse enough that
# two writes in the same test can share an mtime (CI flake). Anchored to NOW —
# the feed also gates data files as stale when their mtime is old vs wall clock
_MT = [int(time.time())]


def _write(path, text):
    path.write_text(text)
    _MT[0] += 1
    os.utime(path, (_MT[0], _MT[0]))


def _chain(seq=1, track=0, fx=None, focused_fx=0):
    if fx is None:
        fx = [
            {"name": "VST3: Diva (u-he)", "enabled": True,
             "params": [{"name": "Cutoff", "v": 0.25, "d": "310 Hz"},
                        {"name": "Res", "v": 0.5, "d": "50%"}]},
            {"name": "ReaComp", "enabled": False,
             "params": [{"name": "Thresh", "v": 0.7, "d": "-6 dB"}]},
        ]
    return {"seq": seq, "track": {"index": track, "name": "Bass", "guid": "{g}"},
            "focused_fx": focused_fx, "fx": fx}


# --- ReaperFxFeed file plumbing ------------------------------------------------

def test_feed_fires_chain_touched_and_values(tmp_path):
    feed = ReaperFxFeed(tmp_path)
    chains, touches, values = [], [], []
    feed.on_chain = chains.append
    feed.on_touched = touches.append
    feed.on_values = values.append

    _write(tmp_path / "chain.json", json.dumps(_chain()))
    feed.poll_once()
    assert len(chains) == 1 and chains[0]["fx"][0]["name"].startswith("VST3")

    _write(tmp_path / "live.json", json.dumps({
        "seq": 1,
        "touched": {"seq": 1, "fx": 0, "param": 0, "v": 0.3, "d": "400 Hz"},
        "values": [[0, 0, 0.3, "400 Hz"]]}))
    feed.poll_once()
    assert touches == [{"seq": 1, "fx": 0, "param": 0, "v": 0.3, "d": "400 Hz"}]
    assert values == [[[0, 0, 0.3, "400 Hz"]]]

    # values move on, touched seq stays -> only on_values fires again
    _write(tmp_path / "live.json", json.dumps({
        "seq": 2,
        "touched": {"seq": 1, "fx": 0, "param": 0, "v": 0.3, "d": "400 Hz"},
        "values": [[0, 0, 0.35, "420 Hz"]]}))
    feed.poll_once()
    assert len(touches) == 1
    assert len(values) == 2


def test_feed_ignores_stale_and_malformed(tmp_path):
    feed = ReaperFxFeed(tmp_path)
    chains = []
    feed.on_chain = chains.append
    _write(tmp_path / "chain.json", json.dumps(_chain(seq=5)))
    feed.poll_once()
    feed.poll_once()                       # unchanged file -> no refire
    assert len(chains) == 1
    _write(tmp_path / "chain.json", '{"seq": 6, "fx": [truncated')
    feed.poll_once()                       # mid-write torn file -> skipped
    assert len(chains) == 1


def test_watch_file_roundtrip(tmp_path):
    feed = ReaperFxFeed(tmp_path)
    feed.write_watch([(0, 12), (2, 5)])
    assert (tmp_path / "watch.txt").read_text() == "0 12\n2 5\n"
    feed.write_watch([])
    assert (tmp_path / "watch.txt").read_text() == ""


def test_learn_file_signals_arm(tmp_path):
    feed = ReaperFxFeed(tmp_path)
    feed.set_learn(True)
    assert (tmp_path / "learn.txt").exists()
    feed.set_learn(False)
    assert not (tmp_path / "learn.txt").exists()
    feed.set_learn(False)                              # idempotent when absent


# --- ReaperSysexSource integration ---------------------------------------------

def _source(tmp_path):
    # never start()ed: feed callbacks are driven directly, no OSC needed
    return ReaperSysexSource(fx_feed_dir=tmp_path)


def test_chain_becomes_device_and_param_inventory(tmp_path):
    src = _source(tmp_path)
    src._on_feed_chain(_chain())
    devs = src.devices()
    assert [d.name for d in devs] == ["VST3: Diva (u-he)", "ReaComp"]
    assert devs[1].enabled is False
    params = src.device_params(0)
    assert [p.name for p in params] == ["Cutoff", "Res"]
    assert abs(params[0].value - 0.25) < 1e-6
    assert params[0].display == "310 Hz"


def test_devices_changed_fires_on_structure_not_values(tmp_path):
    src = _source(tmp_path)
    fired = []
    src.on_devices_changed = lambda: fired.append(1)
    src._on_feed_chain(_chain(seq=1))
    assert len(fired) == 1
    values_only = _chain(seq=2)
    values_only["fx"][0]["params"][0]["v"] = 0.9       # same structure
    src._on_feed_chain(values_only)
    assert len(fired) == 1
    src._on_feed_chain(_chain(seq=3, fx=[{"name": "New", "enabled": True,
                                          "params": []}]))
    assert len(fired) == 2


def test_focus_change_switches_plugin_track_and_fires_focus_callback(tmp_path):
    # plugin mode follows the focused FX's track, not mixer selection: a chain
    # on a new track fires on_plugin_focus_changed and switches the device list
    src = _source(tmp_path)
    focus, devs = [], []
    src.on_plugin_focus_changed = lambda: focus.append(1)
    src.on_devices_changed = lambda: devs.append(1)
    src._on_feed_chain(_chain(track=3, focused_fx=1))
    assert focus == [1]                                # focus change, not devices
    assert devs == []
    assert [d.name for d in src.devices()] == ["VST3: Diva (u-he)", "ReaComp"]
    assert src.selected_device() == 1                  # follows focused_fx


def test_touch_fires_learn_and_value_by_default(tmp_path):
    # DAW-initiated learn is ON by default (needed to create new maps); the
    # feed's identity-change-only touch reporting keeps modulation from flooding
    src = _source(tmp_path)
    src._on_feed_chain(_chain())
    touched, vals = [], []
    src.on_param_touched = lambda fx, p: touched.append((fx, p))
    src.on_device_param_value = lambda *a: vals.append(a)
    src._on_feed_touched({"fx": 0, "param": 1, "v": 0.6, "d": "60%"})
    assert touched == [(0, 1)]
    assert vals == [(0, 1, 0.6, "60%")]
    assert abs(src.device_params(0)[1].value - 0.6) < 1e-6


def test_daw_learn_can_be_disabled(tmp_path):
    src = ReaperSysexSource(fx_feed_dir=tmp_path, daw_initiated_learn=False)
    src._on_feed_chain(_chain())
    touched, vals = [], []
    src.on_param_touched = lambda fx, p: touched.append((fx, p))
    src.on_device_param_value = lambda *a: vals.append(a)
    src._on_feed_touched({"fx": 0, "param": 1, "v": 0.6, "d": "60%"})
    assert touched == []                               # learn suppressed
    assert vals == [(0, 1, 0.6, "60%")]                # value still follows


def test_watched_values_diff_before_firing(tmp_path):
    src = _source(tmp_path)
    src._on_feed_chain(_chain())
    vals = []
    src.on_device_param_value = lambda *a: vals.append(a)
    src._on_feed_values([[0, 0, 0.9, "900 Hz"]])
    src._on_feed_values([[0, 0, 0.9, "900 Hz"]])       # unchanged -> once
    assert vals == [(0, 0, 0.9, "900 Hz")]


def test_own_write_echo_is_suppressed(tmp_path):
    src = _source(tmp_path)
    src._on_feed_chain(_chain())
    vals, touched = [], []
    src.on_device_param_value = lambda *a: vals.append(a)
    src.on_param_touched = lambda *a: touched.append(a)
    src._echo.sent("fx/0/0", 0.42)                     # as set_device_param does
    src._on_feed_touched({"fx": 0, "param": 0, "v": 0.42, "d": "42%"})
    assert not vals and not touched


def test_set_watched_params_writes_watch_file(tmp_path):
    src = _source(tmp_path)
    src.set_watched_params([(0, 3), (1, 0)])
    assert (tmp_path / "watch.txt").read_text() == "0 3\n1 0\n"


def test_reaper_source_tracks_selected_device(tmp_path):
    # regression: the base class defaults selected_device to 0 / no-op; without
    # the override every FX past the first resolves against FX #0
    src = _source(tmp_path)
    assert src.selected_device() == 0
    src.set_selected_device(2)
    assert src.selected_device() == 2


def test_feed_owns_plugin_data_osc_fxparam_ignored(tmp_path):
    # with the feed active the OSC fxparam handlers must no-op, else a mapped
    # param gets feedback on two paths but only one echo entry -> motor fight
    src = _source(tmp_path)
    src._on_feed_chain(_chain())
    vals = []
    src.on_device_param_value = lambda *a: vals.append(a)
    src._on_fxparam_value("/track/1/fx/1/fxparam/1/value", 0.99)
    assert vals == []                                  # OSC path suppressed
    assert abs(src.device_params(0)[0].value - 0.25) < 1e-6   # model untouched


def test_beyond_cap_touched_param_joins_model_with_real_name(tmp_path):
    # giant plugins overflow chain.json's inventory cap; a touch beyond the
    # cap must still enter the model with its real name (learn hash identity)
    src = _source(tmp_path)
    src._on_feed_chain(_chain())
    src._on_feed_touched({"fx": 0, "param": 1936, "v": 0.52, "d": "+1",
                          "n": "Arp Range"})
    params = {p.index: p for p in src.device_params(0)}
    assert params[1936].name == "Arp Range"
    assert abs(params[1936].value - 0.52) < 1e-6


# --- DAW liveness (heartbeat) --------------------------------------------------

def test_heartbeat_edges_fire_daw_alive(tmp_path):
    # a controllable clock, with heartbeat mtimes stamped FROM that clock —
    # real file times are too coarse on Windows (consecutive writes can share
    # an mtime, and the poller compares st_mtime_ns to the last one seen)
    import os
    clk = [1000.0]
    feed = ReaperFxFeed(tmp_path, gone_timeout_s=4.0, clock=lambda: clk[0])
    edges = []
    feed.on_daw_alive = edges.append
    hb = tmp_path / "heartbeat"

    def beat(text):
        hb.write_text(text)
        os.utime(hb, (clk[0], clk[0]))

    feed.poll_once()                       # no heartbeat yet -> silence
    assert edges == []

    beat("1"); feed.poll_once()            # first beat -> alive True
    assert edges == [True]

    clk[0] += 1.0; beat("2"); feed.poll_once()   # still beating
    assert edges == [True]                 # no re-fire

    clk[0] += 10.0; feed.poll_once()       # mtime stale past timeout -> gone
    assert edges == [True, False]

    clk[0] += 1.0; beat("3"); feed.poll_once()   # beats again -> back
    assert edges == [True, False, True]


def test_daw_gone_clears_reaper_session(tmp_path):
    # when REAPER's heartbeat stops, the source forgets its session and
    # signals the bridge (on_daw_alive False) so the device gets blanked
    src = ReaperSysexSource(fx_feed_dir=tmp_path, enable_fx_feed=True)
    src._names[0] = "Bass"; src._volumes[0] = 0.8
    src._fx[0] = {0: {"name": "Diva", "params": []}}
    src._track_count = 4
    alive_edges = []
    src.on_daw_alive = alive_edges.append
    assert src.tracks()                    # has a session
    src._on_feed_daw_alive(False)
    assert src.tracks() == [] and src.devices() == []
    assert alive_edges == [False]


def test_stale_chain_file_is_not_replayed(tmp_path):
    # a chain.json left from a dead REAPER session (old mtime, no heartbeat)
    # must NOT be delivered onto the device
    import json as _json
    import os as _os
    (tmp_path / "chain.json").write_text(_json.dumps(_chain(seq=9)))
    old = 1000.0
    _os.utime(tmp_path / "chain.json", (old, old))   # ancient mtime
    fired = []
    feed = ReaperFxFeed(tmp_path, wall_clock=lambda: old + 3600)  # 1h later
    feed.on_chain = fired.append
    feed.poll_once()
    assert fired == []                     # stale -> ignored

    # a fresh write (active REAPER) IS delivered
    (tmp_path / "chain.json").write_text(_json.dumps(_chain(seq=10)))
    _os.utime(tmp_path / "chain.json", (old + 3600, old + 3600))  # "now"
    feed.poll_once()
    assert len(fired) == 1 and fired[0]["seq"] == 10
