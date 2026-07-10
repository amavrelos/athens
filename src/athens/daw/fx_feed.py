"""File-IPC feed from the REAPER companion ReaScript (reaper/roto_fx_feed.lua).

REAPER's OSC surface cannot enumerate a track's FX chain with full parameter
names — plugin (VST-follow) mode and the learn handshake need exactly that.
The Lua script runs inside REAPER, snapshots the selected track's chain via
the TrackFX_* API, and exchanges small files with us. All writes on both
sides are atomic (write tmp file, rename into place):

    <dir>/chain.json   Lua -> py   selected track + full FX/param inventory,
                                   rewritten when the chain's structure changes
    <dir>/live.json    Lua -> py   last-touched param + watched param values,
                                   rewritten while values move (fast, tiny)
    <dir>/watch.txt    py -> Lua   "fx param" lines: params to stream because
                                   they sit on hardware controls

Default dir: <REAPER resource path>/roto-reaper — the Lua script derives the
same location via reaper.GetResourcePath(). ExtState was rejected as IPC: it
lives inside REAPER's process, unreadable from here; sockets were rejected
because stock ReaScript (Lua, no js_ReaScriptAPI dependency) has none.

Files are state, not queues: each file holds the latest complete state and a
`seq` counter, so a missed intermediate write never corrupts anything — the
next poll sees the newest state.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)

CHAIN_FILE = "chain.json"
LIVE_FILE = "live.json"
WATCH_FILE = "watch.txt"
LEARN_FILE = "learn.txt"
CMD_FILE = "cmd.txt"
HEARTBEAT_FILE = "heartbeat"

DEFAULT_POLL_HZ = 20.0
# REAPER is declared gone this long after its heartbeat mtime stops advancing
# (the Lua feed beats ~1/s, so this rides out a few missed beats)
DAW_GONE_TIMEOUT_S = 4.0
# with no heartbeat (old ReaScript), a data file is trusted as live only if it
# was written this recently — keeps a stale file from a dead session out
DATA_FRESH_WINDOW_S = 10.0


def reaper_resource_dir() -> Path:
    """REAPER's per-user resource folder for the current platform (parent of
    Scripts/, roto-reaper/, ...)."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/REAPER"
    if os.name == "nt":  # pragma: no cover
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "REAPER"
    return Path.home() / ".config/REAPER"  # pragma: no cover


def default_feed_dir() -> Path:
    """<REAPER resource path>/roto-reaper for the current platform."""
    return reaper_resource_dir() / "roto-reaper"


class ReaperFxFeed:
    """Polls the ReaScript's files and fires callbacks on fresh data."""

    def __init__(self, directory: Optional[os.PathLike] = None,
                 poll_hz: float = DEFAULT_POLL_HZ,
                 gone_timeout_s: float = DAW_GONE_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 fresh_window_s: float = DATA_FRESH_WINDOW_S):
        self.dir = Path(directory) if directory is not None else default_feed_dir()
        self._interval = 1.0 / poll_hz
        self._gone_timeout = gone_timeout_s
        self._clock = clock
        self._wall = wall_clock              # for file-mtime freshness (wall)
        self._fresh_window = fresh_window_s
        # chain snapshot: {"seq", "track": {"index","name","guid"}, "fx": [...]}
        self.on_chain: Optional[Callable[[dict], None]] = None
        # touch event: {"fx", "param", "v", "d"}
        self.on_touched: Optional[Callable[[dict], None]] = None
        # watched values, full current set: [[fx, param, v, d], ...]
        self.on_values: Optional[Callable[[list], None]] = None
        # project track count (REAPER's OSC can't distinguish real tracks
        # from bank padding; the ReaScript reports the true count)
        self.on_track_count: Optional[Callable[[int], None]] = None
        # version the LOADED feed script stamped into live.json — for the
        # loaded-vs-bundled "reload the ReaScript" check
        self.on_script_version: Optional[Callable[[str], None]] = None
        self._script_version: Optional[str] = None   # None until first live.json;
        #   "" = a pre-version feed (no "version" key) -> still reported as older
        # REAPER liveness edge: True when the heartbeat (re)appears, False
        # when it goes stale. Only fires once the feed has EVER beaten, so a
        # user running OSC-only (no ReaScript) is never wrongly declared gone.
        self.on_daw_alive: Optional[Callable[[bool], None]] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stamps: dict[str, tuple] = {}     # file -> (mtime_ns, size)
        self._chain_seq = -1
        self._live_seq = -1
        self._touched_seq = -1
        self._hb_mtime: Optional[int] = None    # last heartbeat mtime seen
        self._hb_last_beat = 0.0                # clock() when it last advanced
        self._hb_ever = False                   # feed has beaten at least once
        self._alive = False                     # believed DAW-alive state
        self._mtimes: dict[str, float] = {}     # file -> last wall mtime (s)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return          # idempotent (source may be started twice)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="reaper-fx-feed")
        self._thread.start()
        log.info("ReaperFxFeed: watching %s", self.dir)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._wipe()

    def _wipe(self) -> None:
        """Clean exit: remove the IPC files so nothing stale is left behind — a
        chain.json lingering from a past REAPER session can put a ghost session
        on the device (stale reads are also gated by mtime, but not leaving the
        file is cleaner). Best-effort: if REAPER's reascript is still running it
        recreates its own files within a beat, but Athens' side (watch/learn/cmd)
        stays gone."""
        for name in (CHAIN_FILE, LIVE_FILE, HEARTBEAT_FILE,
                     WATCH_FILE, LEARN_FILE, CMD_FILE):
            for p in (self.dir / name, self.dir / (name + ".tmp")):
                try:
                    p.unlink()
                except OSError:
                    pass

    # -- py -> Lua ------------------------------------------------------------
    def write_watch(self, params: List[Tuple[int, int]]) -> None:
        """Publish the (fx, param) pairs the Lua script should stream."""
        tmp = self.dir / (WATCH_FILE + ".tmp")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text("".join(f"{fx} {param}\n" for fx, param in params))
        tmp.replace(self.dir / WATCH_FILE)

    def set_learn(self, armed: bool) -> None:
        """Signal the Lua to run move-detection (offer the param the user moves)
        while learn is armed. Presence of learn.txt = armed."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / LEARN_FILE
        if armed:
            path.write_text("1")
        else:
            try:
                path.unlink()
            except OSError:
                pass

    def write_command(self, command: str) -> None:
        """One command line for the Lua to apply and delete: 'focus <fx>' or
        'enable <fx> <0|1>'. Last write wins if the previous one wasn't
        consumed yet — acceptable for single user gestures."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (CMD_FILE + ".tmp")
        tmp.write_text(command + "\n")
        tmp.replace(self.dir / CMD_FILE)

    # -- poll loop -------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.poll_once()

    def poll_once(self) -> None:
        """One poll pass (public so tests can drive the feed synchronously)."""
        self._poll_heartbeat()
        chain = self._read_if_changed(CHAIN_FILE)
        # IGNORE STALE FILES: a chain.json left on disk from a previous REAPER
        # session must NOT be replayed onto the device (a stale one puts a ghost
        # session on the ROTO). Deliver only when the DAW is actually alive —
        # heartbeat fresh, or (for the old ReaScript that has no heartbeat) the
        # file itself written within the freshness window.
        if chain is not None and self._data_live(CHAIN_FILE) \
                and chain.get("seq", 0) != self._chain_seq:
            self._chain_seq = chain.get("seq", 0)
            self._fire(self.on_chain, chain)
        live = self._read_if_changed(LIVE_FILE)
        if live is not None and live.get("seq", 0) != self._live_seq:
            self._live_seq = live.get("seq", 0)
            # the track COUNT is STRUCTURAL — always apply it, even from an
            # older live.json: it clamps REAPER's OSC bank-padding to the real
            # project size, and a dead REAPER sends no OSC names so a stale
            # count can't resurrect a ghost. Only the live param data (touched
            # / watched values) is gated by freshness below.
            count = live.get("tracks")
            if isinstance(count, int):
                self._fire(self.on_track_count, count)
            ver = live.get("version")
            ver = ver if isinstance(ver, str) else ""   # missing key -> older feed
            if ver != self._script_version:
                self._script_version = ver
                self._fire(self.on_script_version, ver)
            if self._data_live(LIVE_FILE):        # live param data: gate stale
                touched = live.get("touched")
                if touched and touched.get("seq", 0) != self._touched_seq:
                    self._touched_seq = touched.get("seq", 0)
                    self._fire(self.on_touched, touched)
                values = live.get("values")
                if values:
                    self._fire(self.on_values, values)

    def _poll_heartbeat(self) -> None:
        """Track the Lua feed's heartbeat and fire on_daw_alive edges. The
        file's mtime advancing == REAPER+script alive; a stale mtime past the
        timeout == gone. Never declares 'gone' before the first ever beat, so
        an OSC-only setup (no ReaScript) is left alone."""
        try:
            mtime = (self.dir / HEARTBEAT_FILE).stat().st_mtime_ns
        except OSError:
            mtime = None
        now = self._clock()
        if mtime is not None and mtime != self._hb_mtime:
            self._hb_mtime = mtime
            self._hb_last_beat = now
            self._hb_ever = True
            if not self._alive:
                self._alive = True
                # a file gated out as stale before the DAW came alive won't
                # differ on the next poll, so drop the stamps to re-read and
                # deliver the now-live snapshot
                self._stamps.clear()
                self._fire(self.on_daw_alive, True)
        elif (self._alive and self._hb_ever
                and now - self._hb_last_beat > self._gone_timeout):
            self._alive = False
            self._fire(self.on_daw_alive, False)

    def _data_live(self, name: str) -> bool:
        """Is this data file trustworthy right now? Yes if the heartbeat says
        the DAW is alive, or (no heartbeat / old ReaScript) the file was
        written within the freshness window. A stale file from a dead session
        is neither."""
        if self._alive:
            return True
        return self._wall() - self._mtimes.get(name, 0.0) < self._fresh_window

    def _read_if_changed(self, name: str) -> Optional[dict]:
        path = self.dir / name
        try:
            st = path.stat()
        except OSError:
            return None
        self._mtimes[name] = st.st_mtime
        stamp = (st.st_mtime_ns, st.st_size)
        if self._stamps.get(name) == stamp:
            return None
        try:
            # errors="replace": a plugin/param name with a stray non-UTF-8 byte
            # must not make json.loads throw and discard the whole snapshot
            # (the file's signature wouldn't change, so it'd never be retried)
            data = json.loads(path.read_text(errors="replace"))
        except (OSError, ValueError):
            return None            # mid-write or malformed: retry next poll
        self._stamps[name] = stamp
        return data if isinstance(data, dict) else None

    @staticmethod
    def _fire(cb, *args) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:          # feed thread must survive a bad callback
            log.exception("fx-feed callback failed")
