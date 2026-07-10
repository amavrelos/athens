"""ReaperSysexSource — feeds the native SysEx bridge from a real REAPER.

Transport: REAPER's built-in OSC control surface, both directions.

REAPER setup (Preferences > Control/OSC/web > Add > OSC (Open Sound Control)):
  * Mode: "Configure device IP+local port"
  * Device IP + Device port  = where REAPER SENDS feedback  -> our RECV socket
  * Local IP + Local listen port = where REAPER RECEIVES     -> our SEND target
  IMPORTANT: those two are the reverse of what "device/local" intuitively
  suggests. With defaults: set Device port 8000 (we listen on 8000) and
  Local listen port 9000 (we send to <Local IP>:9000). NOTE REAPER's Local IP
  may be the LAN address (e.g. 192.168.1.21), not 127.0.0.1 — send there, and
  bind our receiver to 0.0.0.0 so loopback sends are caught either way.
  Track/FX-param counts live in the pattern-config .ReaperOSC file, not this
  dialog; the stock Default gives 8 tracks (matches the 8 knobs).

Addresses used (all in the stock Default.ReaperOSC):
    /track/N/name (s)                       -> track names
    /track/N/volume (f 0..1)                <-> mixer knobs / motors
    /track/N/select (f)                     <-> selection follow
    /play /record /repeat (f)               -> transport status
    /play /stop /record /repeat /rewind /forward  <- transport actions
    /track/N/fx/M/name (s)                  -> FX list of a track
    /track/N/fx/M/fxparam/P/name (s)        -> plugin param names
    /track/N/fx/M/fxparam/P/value (f | s)   <-> plugin param values + display

Plugin (VST-follow) mode additionally needs the *full* FX chain with all
param names — REAPER's OSC only ever volunteers one FX at a time — so the
authoritative device/param inventory comes from the companion ReaScript via
`ReaperFxFeed` (see fx_feed.py + reaper/roto_fx_feed.lua). OSC stays the
inbound path (set param/volume) and a supplementary value stream.

Echoes of our own sends are consumed by an EchoGate so motors never fight the
user's hand. Track colours aren't in REAPER's default OSC — a slot palette is
used until the ReaScript colour push lands.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from ..sysex.constants import TransportAction
from .echo import EchoGate
from .fx_feed import ReaperFxFeed
from .source import DeviceInfo, PluginParam, SysexDawSource, TrackInfo, TransportState

log = logging.getLogger(__name__)

# Matches the documented REAPER surface config above (reaper.ini:
# `csurf_0=OSC "roto-reaper" 3 9000 "127.0.0.1" 8000 ...` = REAPER listens on
# 9000 and sends feedback to 8000): we send to 9000, we listen on 8000.
DEFAULT_OSC_SEND = ("127.0.0.1", 9000)
DEFAULT_OSC_RECV = ("0.0.0.0", 8000)

_TRACKS_DEBOUNCE = 0.25   # coalesce REAPER's name bursts into one push
# grace after start for the ReaScript feed to report the real track count
# before we'd fall back to trusting OSC's (padded) bank — kills the phantom
# flash on connect; after it, a still-silent feed means it isn't running
_FEED_GRACE_S = 3.0
# default OSC bank size before the feed reports the real count — the ROTO's
# 8 knobs. NEVER over-request: REAPER throttles OSC output to ~1 msg/130ms, so
# a padded bank is seconds of drip-fed feedback that stalls track selection.
_DEFAULT_BANK = 8
# REAPER liveness: any inbound OSC keeps the DAW "alive"; this long with NO OSC
# and no feed heartbeat == REAPER is gone (its VU/feedback stream stops the
# instant it quits). Matches the feed's own gone timeout.
_OSC_GONE_S = 4.0
# after start(), give REAPER this long to say anything before total silence is
# read as "not running" — covers a slow launch + OSC's throttled first dump.
# Without it, a source that just started would blank before REAPER could speak.
_LIVE_GRACE_S = 5.0


def _parts(address: str) -> list:
    return address.strip("/").split("/")


class ReaperSysexSource(SysexDawSource):
    DAW_NAME = "REAPER"

    def __init__(self, osc_send=DEFAULT_OSC_SEND, osc_recv=DEFAULT_OSC_RECV,
                 fx_feed_dir=None, enable_fx_feed: bool = True,
                 daw_initiated_learn: bool = True):
        super().__init__()
        # DAW-initiated learn (grab a param in REAPER -> device learns it to the
        # armed control) is how a NEW mapping is created — without it the device
        # can only re-confirm maps already in flash. The feed reports a "touched"
        # param only on identity change (not value jitter), so it won't flood.
        self._daw_initiated_learn = daw_initiated_learn
        self._osc_send = osc_send
        self._osc_recv = osc_recv
        self._client = None
        self._server = None
        self._lock = threading.Lock()
        self._echo = EchoGate()
        self._names: dict[int, str] = {}          # 0-based
        self._volumes: dict[int, float] = {}
        self._pans: dict[int, float] = {}
        self._sends: dict[tuple, float] = {}      # (track, send) -> level
        self._flags: dict[tuple, bool] = {}       # (track, flag) -> on
        self._vu: dict[int, list] = {}            # track -> [left, right]
        self._selected = 0
        self._selected_device = 0
        self._transport = TransportState()
        self._fx: dict[int, dict] = {}            # track -> {fx_i: {"name", "params"}}
        self._plugin_track = 0                     # focused-FX track (plugin mode)
        self._touched: Optional[Tuple[int, int]] = None   # last (fx, param) grabbed
        self._tracks_timer: Optional[threading.Timer] = None
        self._feed: Optional[ReaperFxFeed] = None
        self._track_count: Optional[int] = None    # ReaScript ground truth
        self._feed_active = False                  # feed has delivered data
        self._feed_start = 0.0                      # when the feed started (grace)
        self._requested_count: Optional[int] = None  # last /device/track/count sent
        # -- liveness: combined feed-heartbeat OR recent OSC --
        self._last_osc = 0.0                        # monotonic of last inbound OSC
        self._feed_alive = False                    # feed heartbeat edge state
        self._alive_pub = True                      # published alive (optimistic
        #                                             at start, matches service)
        self._live_start = 0.0                      # start() time, for the grace
        self._live_stop = threading.Event()
        self._live_timer: Optional[threading.Timer] = None
        self._alive_lock = threading.Lock()         # serialises the alive edge
        if enable_fx_feed:
            self._feed = ReaperFxFeed(fx_feed_dir)
            self._feed.on_chain = self._on_feed_chain
            self._feed.on_touched = self._on_feed_touched
            self._feed.on_values = self._on_feed_values
            self._feed.on_track_count = self._on_feed_track_count
            self._feed.on_daw_alive = self._on_feed_daw_alive
            self._feed.on_script_version = \
                lambda v: self.on_script_version and self.on_script_version(v)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._server is not None:
            return          # idempotent: the service starts the source at app
        #                     launch AND the bridge start()s it again on attach
        # liveness poller: one, even if python-osc is missing (then start() is
        # not _server-idempotent). Grace starts now so a silent REAPER isn't
        # declared gone before it has had a chance to speak.
        if self._live_timer is None:
            self._live_start = time.monotonic()
            self._live_stop.clear()
            self._schedule_live_poll()
        # the ReaScript FX feed works even without python-osc, so start it first
        if self._feed is not None:
            self._feed_start = time.monotonic()
            self._feed.start()
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
            from pythonosc.udp_client import SimpleUDPClient
        except ImportError:
            log.warning("python-osc not installed; ReaperSysexSource inert. "
                        "pip install -e '.[osc]' to enable the REAPER feed.")
            return
        self._client = SimpleUDPClient(*self._osc_send)
        d = Dispatcher()

        # any inbound OSC (even an address we don't map — /time, /tempo, VU)
        # means REAPER is alive right now, so tap the liveness mtime on the way
        # to the real handler. One wrapper here beats a bump in every handler.
        def live(fn):
            def wrapped(addr, *a):
                self._seen_osc()
                return fn(addr, *a)
            return wrapped

        d.map("/track/*/name", live(self._on_name))
        d.map("/track/*/volume", live(self._on_volume))
        d.map("/track/*/pan", live(self._on_pan))
        d.map("/track/*/send/*/volume", live(self._on_send))
        d.map("/track/*/mute", live(lambda a, *v: self._on_flag("muted", a, *v)))
        d.map("/track/*/solo", live(lambda a, *v: self._on_flag("soloed", a, *v)))
        d.map("/track/*/recarm", live(lambda a, *v: self._on_flag("armed", a, *v)))
        d.map("/track/*/monitor", live(lambda a, *v: self._on_flag("monitoring", a, *v)))
        d.map("/track/*/vu", live(self._on_vu))
        d.map("/track/*/vu/L", live(lambda a, *v: self._on_vu_side(0, a, *v)))
        d.map("/track/*/vu/R", live(lambda a, *v: self._on_vu_side(1, a, *v)))
        d.map("/track/*/select", live(self._on_select))
        # closures, NOT map() extras: python-osc wraps extras in a list
        d.map("/play", live(lambda a, *v: self._on_transport("playing", *v)))
        d.map("/record", live(lambda a, *v: self._on_transport("recording", *v)))
        d.map("/repeat", live(lambda a, *v: self._on_transport("loop", *v)))
        d.map("/track/*/fx/*/name", live(self._on_fx_name))
        d.map("/track/*/fx/*/fxparam/*/name", live(self._on_fxparam_name))
        d.map("/track/*/fx/*/fxparam/*/value", live(self._on_fxparam_value))
        d.set_default_handler(live(lambda a, *v: None))
        self._server = ThreadingOSCUDPServer(self._osc_recv, d)
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="reaper-osc").start()
        log.info("ReaperSysexSource: send -> %s, feedback <- %s",
                 self._osc_send, self._osc_recv)
        self.refresh_state()

    def refresh_state(self) -> None:
        """Make REAPER re-send the surface state. Request ONLY the tracks that
        exist (the feed's count), never a padded bank (see _DEFAULT_BANK on the
        OSC throttle). Setting the track count only dumps state when the value
        CHANGES, so we DEDUPE it (repeated refreshes on connect must not
        re-flood) and always fire 'refresh all surfaces'."""
        if self._client is None:
            return
        # real project size once the feed knows it; the visible bank until then
        target = self._track_count if self._track_count is not None \
            else _DEFAULT_BANK
        if target != self._requested_count:
            self._requested_count = target
            self._client.send_message("/device/track/count", target)
        self._client.send_message("/action", 41743)

    def stop(self) -> None:
        self._live_stop.set()
        if self._live_timer is not None:
            self._live_timer.cancel()
            self._live_timer = None
        if self._feed is not None:
            self._feed.stop()
        if self._tracks_timer is not None:
            self._tracks_timer.cancel()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- SysexDawSource: mixer ----------------------------------------------
    def tracks(self) -> List[TrackInfo]:
        """Real tracks only. REAPER's OSC surface pads its bank (device track
        count) with feedback for slots beyond the project, so OSC alone can't
        tell the project's size — the ReaScript feed reports the true count
        (live.json 'tracks') and clamps the list.

        DETERMINISTIC: with the feed enabled but not yet reporting, the OSC
        bank is untrustworthy (padding). Return [] during the start-up grace
        window so a phantom bank never flashes on connect. Once the feed
        reports a count we clamp to it; if the grace expires with the feed
        still silent it isn't running, so we fall back to OSC and the UI shows
        the 'feed off' warning."""
        with self._lock:
            if self._feed is not None and self._track_count is None \
                    and time.monotonic() - self._feed_start < _FEED_GRACE_S:
                return []
            if not self._names and not self._volumes:
                return []
            n = max(list(self._names) + list(self._volumes)) + 1
            if self._track_count is not None:
                n = min(n, self._track_count)
            # REAPER's real name — do NOT fabricate "Track N" for an unnamed
            # track: that made unnamed-real and phantom-padded indistinguishable.
            # Empty name stays empty.
            return [TrackInfo(index=i,
                              name=self._names.get(i, ""),
                              colour=(i % 12) or 1,
                              volume=self._volumes.get(i, 0.0),
                              pan=self._pans.get(i, 0.5),
                              muted=self._flags.get((i, "muted"), False),
                              soloed=self._flags.get((i, "soloed"), False),
                              armed=self._flags.get((i, "armed"), False),
                              monitoring=self._flags.get((i, "monitoring"), False))
                    for i in range(n)]

    def _on_feed_track_count(self, count: int) -> None:
        self._feed_active = True
        with self._lock:
            changed = self._track_count != count
            self._track_count = count
        if changed:
            # size the OSC bank to the real project now that we know it (drops
            # the padded default -> less throttled feedback), then re-render
            self.refresh_state()
            self._schedule_tracks_changed()

    def feed_running(self) -> bool:
        """True once the ReaScript feed has delivered anything. When it is
        NOT running, REAPER's OSC bank padding can't be clamped to the real
        project size (OSC has no project-track-count message) — the UI warns
        so the user knows to load roto_fx_feed.lua."""
        return getattr(self, "_feed_active", False)

    # -- liveness: REAPER is alive if the feed beats OR OSC is flowing ----------
    def _seen_osc(self) -> None:
        """Any inbound OSC = REAPER is speaking right now. Also lights the alive
        edge immediately so a returning REAPER re-appears without waiting for
        the next poll."""
        self._last_osc = time.monotonic()
        if not self._alive_pub:
            self._apply_alive(True)

    def _osc_fresh(self) -> bool:
        return (self._last_osc > 0.0
                and time.monotonic() - self._last_osc < _OSC_GONE_S)

    def check_alive(self, timeout: float = 0.0) -> bool:
        """Is REAPER answering right now — feed heartbeat OR fresh OSC. Free
        (timestamp reads, no I/O), so the DAW auto-monitor can consult the
        ACTIVE source instead of only the heartbeat file: an OSC-only setup
        (Lua feed not loaded) must read as live or the monitor would yank the
        session (its promise: never swap away from a live DAW)."""
        return self._feed_alive or self._osc_fresh()

    def _apply_alive(self, alive: bool) -> None:
        """The single owner of the published REAPER-alive state. On the gone
        edge it forgets the whole session (tracks()/devices() empty and the
        bridge, via on_daw_alive False, blanks the device); fires on_daw_alive
        only on a real change. Serialised so the poll and the feed edge can't
        double-fire."""
        with self._alive_lock:
            if alive == self._alive_pub:
                return
            self._alive_pub = alive
            # side effects INSIDE the lock: flag flip, session wipe and edge
            # delivery form one atomic edge. Outside it, a gone-edge (poll
            # timer thread) and an alive-edge (OSC handler thread) could
            # interleave into "session wiped + last edge False while
            # _alive_pub is True" — a blanked device no OSC packet can heal
            # (_seen_osc only fires the edge on a False->True transition).
            # Ordering is _alive_lock -> _lock (via _clear_session); no caller
            # holds _lock when entering here (the OSC live() tap runs BEFORE
            # its handler takes _lock), so this cannot deadlock. The callbacks
            # are queue-puts / bus publishes — non-blocking.
            if not alive:
                self._clear_session()
            if self.on_daw_alive:
                self.on_daw_alive(alive)

    def _clear_session(self) -> None:
        with self._lock:
            self._names.clear(); self._volumes.clear(); self._pans.clear()
            self._sends.clear(); self._flags.clear(); self._vu.clear()
            self._fx.clear(); self._track_count = 0
            self._touched = None

    def _schedule_live_poll(self) -> None:
        self._live_timer = threading.Timer(1.5, self._live_poll)
        self._live_timer.daemon = True
        self._live_timer.start()

    def _live_poll(self) -> None:
        """Reconcile alive from the free signals every 1.5 s. A silent REAPER
        (no feed heartbeat AND no OSC — it quit, or was never there) flips to
        gone once the start-up grace passes. This is exactly what the feed's own
        'never declare gone before the first beat' guard can't catch for an
        OSC-only setup: the phantom-alive REAPER with stale tracks."""
        try:
            if time.monotonic() - self._live_start > _LIVE_GRACE_S:
                self._apply_alive(self._feed_alive or self._osc_fresh())
        except Exception:  # noqa: BLE001 - a poll glitch must not kill the timer
            log.debug("reaper live-poll tick failed", exc_info=True)
        finally:
            if not self._live_stop.is_set():
                self._schedule_live_poll()

    def _on_feed_daw_alive(self, alive: bool) -> None:
        """Feed heartbeat edge. Feed alive => REAPER alive; feed gone only
        blanks if OSC has ALSO fallen silent (REAPER itself is gone), so a
        stopped feed script under a still-open REAPER keeps the mixer live."""
        self._feed_alive = alive
        self._apply_alive(alive or self._osc_fresh())

    def selected_track(self) -> int:
        return self._selected

    def transport(self) -> TransportState:
        return self._transport

    def set_selected_track(self, index: int) -> None:
        with self._lock:
            changed = self._selected != index
            self._selected = index
        if self._client:
            # exclusive select: OSC /select is a per-track boolean that ADDS
            # to REAPER's selection; the accumulated multi-selection makes
            # REAPER apply any fader move to ALL selected tracks ("one knob
            # moves every fader"). 40297 = unselect all.
            self._client.send_message("/action", 40297)
            self._client.send_message(f"/track/{index + 1}/select", 1.0)
        if changed and self.on_selected_track_changed:
            # fire NOW: REAPER's later /select feedback matches the already-
            # stored index, so the change-guard in _on_select won't re-fire —
            # without this, device/app-initiated selection never reaches the UI
            self.on_selected_track_changed()

    def set_track_volume(self, index: int, value: float) -> None:
        with self._lock:
            self._volumes[index] = value
        if self._client:
            self._echo.sent(f"vol/{index}", value)
            self._client.send_message(f"/track/{index + 1}/volume", float(value))

    def set_track_pan(self, index: int, value: float) -> None:
        with self._lock:
            self._pans[index] = value
        if self._client:
            self._echo.sent(f"pan/{index}", value)
            self._client.send_message(f"/track/{index + 1}/pan", float(value))

    def set_track_send(self, index: int, send: int, value: float) -> None:
        with self._lock:
            self._sends[(index, send)] = value
        if self._client:
            self._echo.sent(f"send/{index}/{send}", value)
            self._client.send_message(
                f"/track/{index + 1}/send/{send + 1}/volume", float(value))

    def track_send(self, index: int, send: int) -> float:
        with self._lock:
            return self._sends.get((index, send), 0.0)

    _FLAG_OSC = {"muted": "mute", "soloed": "solo",
                 "armed": "recarm", "monitoring": "monitor"}

    def set_track_flag(self, index: int, flag: str, on: bool) -> None:
        with self._lock:
            self._flags[(index, flag)] = on
        if self._client:
            self._echo.sent(f"{flag}/{index}", 1.0 if on else 0.0)
            self._client.send_message(
                f"/track/{index + 1}/{self._FLAG_OSC[flag]}", 1.0 if on else 0.0)

    _TRANSPORT_OSC = {
        TransportAction.PLAY: "/play",
        TransportAction.STOP: "/stop",
        TransportAction.RECORD: "/record",
        TransportAction.LOOP: "/repeat",
        TransportAction.REWIND: "/rewind",
        TransportAction.FASTFORWARD: "/forward",
    }

    # The ROTO's Logic transport screen has a 'punch' button REAPER has no
    # OSC default for — bind it to a REAPER action instead. 40364 = "Options:
    # Toggle metronome"; change to taste (any action id works).
    PUNCH_ACTION_ID = 40364
    METRONOME_ACTION_ID = 40364

    def run_action(self, action_id: int) -> None:
        if self._client is not None:
            self._client.send_message("/action", int(action_id))

    def transport_action(self, action: TransportAction, pressed: bool) -> None:
        if self._client is None:
            return
        if action in (TransportAction.PUNCH_IN, TransportAction.METRONOME):
            if pressed:
                self._client.send_message(
                    "/action",
                    self.METRONOME_ACTION_ID
                    if action == TransportAction.METRONOME
                    else self.PUNCH_ACTION_ID)
            return
        address = self._TRANSPORT_OSC.get(action)
        if address is None:
            # session-record / re-enable-automation have no REAPER OSC
            # address (Ableton concepts); wire /action ids if wanted later
            return
        if action in (TransportAction.REWIND, TransportAction.FASTFORWARD):
            # b/rewind semantics: scrub while held -> forward press AND release
            self._client.send_message(address, 1.0 if pressed else 0.0)
        elif pressed:
            self._client.send_message(address, 1.0)   # t/: trigger toggles

    # -- SysexDawSource: plugin mode ------------------------------------------
    def devices(self) -> List[DeviceInfo]:
        with self._lock:
            fx = self._fx.get(self._plugin_track, {})
            return [DeviceInfo(index=i, name=fx[i].get("name", f"FX {i + 1}"),
                               enabled=fx[i].get("enabled", True))
                    for i in sorted(fx)]

    def selected_device(self) -> int:
        return self._selected_device

    def set_selected_device(self, index: int) -> None:
        # the bridge resolves mappings against this; without the override it
        # would stay 0 and every FX past the first would map wrong
        with self._lock:
            self._selected_device = index

    def focus_device(self, index: int) -> None:
        """Open the FX's floating window via the ReaScript — GetFocusedFX then
        reports it and the plugin context follows (the same path a mouse
        click takes)."""
        self.set_selected_device(index)
        if self._feed is not None:
            self._feed.write_command(f"focus {index}")

    def set_device_enabled(self, index: int, enabled: bool) -> None:
        """TrackFX_SetEnabled via the ReaScript (authoritative; OSC's bypass
        polarity is ambiguous)."""
        with self._lock:
            slot = self._fx.get(self._plugin_track, {}).get(index)
            if slot is not None:
                slot["enabled"] = enabled
        if self._feed is not None:
            self._feed.write_command(f"enable {index} {1 if enabled else 0}")

    def device_params(self, device_index: int) -> List[PluginParam]:
        with self._lock:
            params = self._fx.get(self._plugin_track, {}).get(device_index,
                                                          {}).get("params", {})
            return [PluginParam(index=p,
                                name=params[p].get("name", f"Param {p + 1}"),
                                value=params[p].get("value", 0.0),
                                display=params[p].get("display", ""),
                                steps=params[p].get("steps", 0))
                    for p in sorted(params)]

    def set_device_param(self, device_index: int, param_index: int,
                         value: float) -> None:
        with self._lock:
            track = self._plugin_track
            param = self._fx.setdefault(track, {}) \
                .setdefault(device_index, {"params": {}})["params"] \
                .setdefault(param_index, {})
            param["value"] = value
        if self._client:
            self._echo.sent(f"fx/{device_index}/{param_index}", value)
            self._client.send_message(
                f"/track/{track + 1}/fx/{device_index + 1}"
                f"/fxparam/{param_index + 1}/value", float(value))

    def set_watched_params(self, params: List[Tuple[int, int]]) -> None:
        if self._feed is not None:
            self._feed.write_watch(params)

    def set_learn_armed(self, armed: bool) -> None:
        if self._feed is not None:
            self._feed.set_learn(armed)

    # -- ReaScript FX feed (feed thread) ---------------------------------------
    def _on_feed_chain(self, snapshot: dict) -> None:
        """Full FX/param inventory of the FOCUSED FX's track, from
        roto_fx_feed.lua. The snapshot always describes whichever plugin window
        the user last opened (focused-FX follow), plus which FX in the chain is
        focused; that track becomes the plugin-mode context, decoupled from the
        mixer's selected track."""
        self._feed_active = True
        track = int(snapshot.get("track", {}).get("index", -1))
        if track < 0:
            return
        focused = int(snapshot.get("focused_fx", 0))
        model: dict[int, dict] = {}
        for i, fx in enumerate(snapshot.get("fx", [])):
            model[i] = {
                "name": str(fx.get("name", f"FX {i + 1}")),
                "enabled": bool(fx.get("enabled", True)),
                "params": {
                    p: {"name": str(prm.get("name", f"Param {p + 1}")),
                        "value": float(prm.get("v", 0.0)),
                        "display": str(prm.get("d", "")),
                        "steps": int(prm.get("q", 0))}
                    for p, prm in enumerate(fx.get("params", []))
                },
            }
        with self._lock:
            old = self._fx.get(track)
            structure_changed = self._structure_sig(old) != self._structure_sig(model)
            self._fx[track] = model
            focus_changed = (track != self._plugin_track
                             or (focused >= 0 and focused != self._selected_device))
            self._plugin_track = track
            if focused >= 0:
                self._selected_device = focused
        if focus_changed and self.on_plugin_focus_changed:
            self.on_plugin_focus_changed()        # new plugin window -> reset + push
        elif structure_changed and self.on_devices_changed:
            self.on_devices_changed()

    @staticmethod
    def _structure_sig(model: Optional[dict]) -> tuple:
        if not model:
            return ()
        return tuple((model[i].get("name"), model[i].get("enabled"),
                      len(model[i].get("params", {})))
                     for i in sorted(model))

    def _on_feed_touched(self, touched: dict) -> None:
        """The user grabbed a param in REAPER (selected track only, per Lua)."""
        try:
            fx, param = int(touched["fx"]), int(touched["param"])
            value = float(touched.get("v", 0.0))
        except (KeyError, TypeError, ValueError):
            return
        display = str(touched.get("d", ""))
        self._store_param(self._plugin_track, fx, param, value, display,
                          name=touched.get("n"))
        self._touched = (fx, param)      # remember for learn-on-arm
        if self._echo.is_echo(f"fx/{fx}/{param}", value):
            return                       # our own knob write bounced back
        if self._daw_initiated_learn and self.on_param_touched:
            self.on_param_touched(fx, param)
        if self.on_device_param_value:
            self.on_device_param_value(fx, param, value, display)

    def current_touched_param(self):
        """No stale offer on the feed path: REAPER's last-touched is unreliable
        for plugin-native UIs (it lands on placeholder 'reserved' slots on some
        plugins), so offering it when learn arms just mis-binds. Move-detection
        (set_learn_armed -> the ReaScript watches for the param you actually
        MOVE) is the sole learn source here; return None so the bridge doesn't
        pre-offer anything."""
        return None

    def _on_feed_values(self, values: list) -> None:
        """Current values of all watched params; diff + forward the changes."""
        for item in values:
            try:
                fx, param, value = int(item[0]), int(item[1]), float(item[2])
            except (IndexError, TypeError, ValueError):
                continue
            display = str(item[3]) if len(item) > 3 else ""
            if not self._store_param(self._plugin_track, fx, param, value, display):
                continue                 # unchanged
            if self._echo.is_echo(f"fx/{fx}/{param}", value):
                continue
            if self.on_device_param_value:
                self.on_device_param_value(fx, param, value, display)

    def _store_param(self, track: int, fx: int, param: int, value: float,
                     display: str, name: Optional[str] = None) -> bool:
        """Update the model; True if the value/display actually changed. A
        touched param beyond chain.json's inventory cap joins the model here,
        carrying its real name so the learn identity hash is right."""
        with self._lock:
            slot = self._fx.setdefault(track, {}) \
                .setdefault(fx, {"params": {}})["params"].setdefault(param, {})
            changed = (abs(slot.get("value", -1.0) - value) > 1e-6
                       or slot.get("display") != display)
            slot["value"], slot["display"] = value, display
            if name:
                slot["name"] = str(name)
            else:
                slot.setdefault("name", f"Param {param + 1}")
        return changed

    # -- OSC handlers (server threads) ------------------------------------------
    def _on_name(self, address: str, *args) -> None:
        p = _parts(address)
        if len(p) != 3 or not p[1].isdigit() or not args:
            return
        i = int(p[1]) - 1
        name = str(args[0])
        with self._lock:
            if self._names.get(i) == name:
                return
            self._names[i] = name
        self._schedule_tracks_changed()

    def _on_volume(self, address: str, *args) -> None:
        p = _parts(address)
        if len(p) != 3 or not p[1].isdigit() or not args:
            return
        i = int(p[1]) - 1
        value = float(args[0])
        log.debug("REAPER volume feedback: track %d = %.3f", i, value)
        with self._lock:
            known = i in self._volumes
            self._volumes[i] = value
        if not known:
            self._schedule_tracks_changed()   # new track appeared
        if self._echo.is_echo(f"vol/{i}", value):
            return
        if self.on_track_volume:
            self.on_track_volume(i, value)

    def _on_flag(self, flag: str, address: str, *args) -> None:
        p = _parts(address)
        if len(p) != 3 or not p[1].isdigit() or not args:
            return
        i = int(p[1]) - 1
        on = float(args[0]) > 0        # monitor arrives as 0/1/2
        with self._lock:
            changed = self._flags.get((i, flag)) != on
            self._flags[(i, flag)] = on
        if not changed or self._echo.is_echo(f"{flag}/{i}", 1.0 if on else 0.0):
            return
        if self.on_track_flag:
            self.on_track_flag(i, flag, on)

    def _on_pan(self, address: str, *args) -> None:
        p = _parts(address)
        if len(p) != 3 or not p[1].isdigit() or not args:
            return
        i = int(p[1]) - 1
        value = float(args[0])
        with self._lock:
            self._pans[i] = value
        if self._echo.is_echo(f"pan/{i}", value):
            return
        if self.on_track_pan:
            self.on_track_pan(i, value)

    def _on_vu(self, address: str, *args) -> None:
        """Mono VU: both meter columns get the same level."""
        p = _parts(address)
        if len(p) != 3 or not p[1].isdigit() or not args:
            return
        i = int(p[1]) - 1
        level = float(args[0])
        if self.on_track_vu:
            self.on_track_vu(i, level, level)

    def _on_vu_side(self, side: int, address: str, *args) -> None:
        """Stereo VU: pair L with the last-seen R (and vice versa)."""
        p = _parts(address)   # track N vu L|R
        if len(p) != 4 or not p[1].isdigit() or not args:
            return
        i = int(p[1]) - 1
        level = float(args[0])
        with self._lock:
            lr = self._vu.setdefault(i, [0.0, 0.0])
            lr[side] = level
            left, right = lr
        if self.on_track_vu:
            self.on_track_vu(i, left, right)

    def _on_send(self, address: str, *args) -> None:
        p = _parts(address)   # track N send M volume
        if len(p) != 5 or not (p[1].isdigit() and p[3].isdigit()) or not args:
            return
        i, send = int(p[1]) - 1, int(p[3]) - 1
        value = float(args[0])
        with self._lock:
            self._sends[(i, send)] = value
        if self._echo.is_echo(f"send/{i}/{send}", value):
            return
        if self.on_track_send:
            self.on_track_send(i, send, value)

    def _on_select(self, address: str, *args) -> None:
        p = _parts(address)
        if len(p) != 3 or not p[1].isdigit() or not args:
            return
        if round(float(args[0])):
            with self._lock:
                changed = self._selected != int(p[1]) - 1
                self._selected = int(p[1]) - 1
            # this is the MIXER's selected track; plugin mode follows the focused
            # FX instead (on_plugin_focus_changed), so no device push here
            if changed:
                log.debug("OSC: selected track %d (received)", int(p[1]))
                if self.on_selected_track_changed:
                    self.on_selected_track_changed()

    def _on_transport(self, field: str, *args) -> None:
        on = bool(round(float(args[0]))) if args else False
        setattr(self._transport, field, on)
        if self.on_transport_changed:
            self.on_transport_changed()

    def _on_fx_name(self, address: str, *args) -> None:
        # when the ReaScript feed is present it is the sole owner of plugin
        # data (chain, param names, and values); OSC fx handlers would only
        # duplicate it — and duplicating the *value* path defeats the single
        # echo entry, re-driving the motor against the user's hand
        if self._feed is not None:
            return
        p = _parts(address)   # track N fx M name
        if len(p) != 5 or not (p[1].isdigit() and p[3].isdigit()) or not args:
            return
        track, fx = int(p[1]) - 1, int(p[3]) - 1
        name = str(args[0])
        fire = False
        with self._lock:
            slot = self._fx.setdefault(track, {}).setdefault(fx, {"params": {}})
            if slot.get("name") != name:
                slot["name"] = name
                fire = track == self._selected
        if fire and self.on_devices_changed:
            self.on_devices_changed()

    def _fxparam_slot(self, address: str):
        p = _parts(address)   # track N fx M fxparam P (name|value)
        if len(p) != 7 or not (p[1].isdigit() and p[3].isdigit()
                               and p[5].isdigit()):
            return None
        return int(p[1]) - 1, int(p[3]) - 1, int(p[5]) - 1

    def _on_fxparam_name(self, address: str, *args) -> None:
        if self._feed is not None:
            return                     # feed owns the param inventory
        loc = self._fxparam_slot(address)
        if loc is None or not args:
            return
        track, fx, param = loc
        with self._lock:
            self._fx.setdefault(track, {}).setdefault(fx, {"params": {}}) \
                ["params"].setdefault(param, {})["name"] = str(args[0])

    def _on_fxparam_value(self, address: str, *args) -> None:
        if self._feed is not None:
            return                     # feed owns the value stream (+ echo gate)
        loc = self._fxparam_slot(address)
        if loc is None or not args:
            return
        track, fx, param = loc
        with self._lock:
            slot = self._fx.setdefault(track, {}) \
                .setdefault(fx, {"params": {}})["params"].setdefault(param, {})
            if isinstance(args[0], str):
                slot["display"] = args[0]        # /value str form
                value = slot.get("value", 0.0)
            else:
                value = float(args[0])
                slot["value"] = value
            display = slot.get("display", "")
        if isinstance(args[0], str):
            pass                                  # display-only update
        elif self._echo.is_echo(f"fx/{fx}/{param}", value):
            return
        if track == self._selected and self.on_device_param_value:
            self.on_device_param_value(fx, param, value, display)

    def _schedule_tracks_changed(self) -> None:
        if self.on_tracks_changed is None:
            return
        if self._tracks_timer is not None:
            self._tracks_timer.cancel()
        self._tracks_timer = threading.Timer(
            _TRACKS_DEBOUNCE,
            lambda: self.on_tracks_changed and self.on_tracks_changed())
        self._tracks_timer.daemon = True
        self._tracks_timer.start()


def install_reascript() -> Optional[str]:
    """Deploy the feed + toggle Lua via script_install.sync_reaper — THE one
    override-aware installer. Kept as an entry point because make_source() runs
    before the service's own sync. Returns the feed's destination path, or None."""
    try:
        from .script_install import REAPER_SCRIPT, reaper_scripts_dir, sync_reaper
        notes = sync_reaper()
        if notes:
            log.info("feed scripts: %s", "; ".join(notes))
        scripts = reaper_scripts_dir()
        dest = (scripts / REAPER_SCRIPT) if scripts is not None else None
        return str(dest) if dest is not None and dest.is_file() else None
    except Exception as exc:  # noqa: BLE001 — best-effort convenience
        log.debug("could not install feed scripts: %s", exc)
        return None
