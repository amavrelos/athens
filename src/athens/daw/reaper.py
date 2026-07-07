"""REAPER backend (mixer layout, MIDI mode) over REAPER's native OSC surface.

Setup in REAPER: Preferences > Control/OSC/web > Add > OSC (Open Sound Control).
  * Mode: "Configure device IP+local port"
  * Device port      = our send port  (REAPER listens here; we send to it)  -> 8000
  * Local listen port= our recv port  (REAPER sends feedback here)          -> 9000
  * Set "device track count" to 8 so /track/1../track/8 form a bank that
    follows selection; move the bank with /device/track/bank/+ | -.

Default `Default.ReaperOSC` addresses we use:
    /track/N/volume  (n, 0..1)  bidirectional  -> knob
    /track/N/mute    (b, 0/1)   bidirectional  -> button
    /track/N/solo    (b, 0/1)   bidirectional  -> button
    /track/N/name    (s)        feedback       -> knob/button label

Layout: 8 knobs = 8 track volumes; 16 buttons = 8 mutes (0-7) + 8 solos (8-15).

Value vs. label split: a volume/mute/solo *state* change drives the knob motor /
button LED only; a *name* change (and only an actual change) triggers a
debounced knob-caption refresh (a serial write). Echoes of our own sends are
consumed by an EchoGate so feedback never re-drives the motor mid-turn.

NOTE python-osc pitfall: extras passed to dispatcher.map() are delivered to the
handler wrapped in a list, so per-address context is bound with closures here.

REAPER's default OSC doesn't emit track colour, so colours are assigned per slot.
Needs the `osc` extra (python-osc). Without it the backend stays inert.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .backend import KNOBS_PER_PAGE, Bank, DawBackend, Param, SwitchSpec
from .echo import EchoGate

log = logging.getLogger(__name__)

DEFAULT_OSC_SEND = ("127.0.0.1", 8000)   # REAPER receives here
DEFAULT_OSC_RECV = ("127.0.0.1", 9000)   # REAPER sends feedback here

_REFRESH_DELAY = 0.3    # coalesce label refreshes into one bank rewrite


def track_index(address: str) -> Optional[int]:
    """'/track/3/volume' -> 3 ; None if not a per-track address."""
    parts = address.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "track" and parts[1].isdigit():
        return int(parts[1])
    return None


def volume_key(track: int) -> str:
    return f"track/{track}/volume"


def switch_key(track: int, kind: str) -> str:
    return f"track/{track}/{kind}"


class ReaperBackend(DawBackend):
    def __init__(self, osc_send=DEFAULT_OSC_SEND, osc_recv=DEFAULT_OSC_RECV,
                 track_count: int = KNOBS_PER_PAGE):
        super().__init__()
        self._osc_send = osc_send
        self._osc_recv = osc_recv
        self._track_count = track_count
        self._client = None
        self._server = None
        self._refresh_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._echo = EchoGate()
        self._tracks: dict[int, dict] = {
            n: {"name": f"Track {n}", "volume": 0.0, "mute": False, "solo": False}
            for n in range(1, track_count + 1)
        }

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
            from pythonosc.udp_client import SimpleUDPClient
        except ImportError:
            log.warning("python-osc not installed; ReaperBackend is inert. "
                        "Install the 'osc' extra to enable the mixer layout.")
            return

        self._client = SimpleUDPClient(*self._osc_send)
        dispatcher = Dispatcher()
        dispatcher.map("/track/*/volume", self._on_volume)
        # closures, NOT map() extras: python-osc wraps extras in a list
        dispatcher.map("/track/*/mute",
                       lambda addr, *a: self._on_switch(addr, "mute", *a))
        dispatcher.map("/track/*/solo",
                       lambda addr, *a: self._on_switch(addr, "solo", *a))
        dispatcher.map("/track/*/name", self._on_name)
        dispatcher.set_default_handler(self._on_other)

        self._server = ThreadingOSCUDPServer(self._osc_recv, dispatcher)
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="reaper-osc").start()
        log.info("ReaperBackend OSC: send -> %s, feedback <- %s",
                 self._osc_send, self._osc_recv)

    def stop(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- bridge-facing -----------------------------------------------------
    def current_bank(self) -> Bank:
        with self._lock:
            params = [
                Param(key=volume_key(n), name=self._tracks[n]["name"],
                      value=self._tracks[n]["volume"], colour=(n % 12) or 1)
                for n in range(1, self._track_count + 1)
            ]
            buttons = [
                SwitchSpec(key=switch_key(n, kind),
                           name=f"{prefix} {self._tracks[n]['name']}",
                           on=self._tracks[n][kind], colour=col)
                for kind, prefix, col in (("mute", "M", 2), ("solo", "S", 4))
                for n in range(1, self._track_count + 1)
            ]
        return Bank(title="Mixer", params=params, buttons=buttons)

    def set_param(self, key: str, value: float) -> None:
        """Knob moved -> push to REAPER."""
        if self._client is None:
            log.debug("set_param ignored (no OSC client): %s = %.3f", key, value)
            return
        self._echo.sent(key, value)
        self._client.send_message("/" + key, float(value))

    def set_switch(self, key: str, on: bool) -> None:
        if self._client is None:
            return
        self._echo.sent(key, 1.0 if on else 0.0)
        self._client.send_message("/" + key, 1.0 if on else 0.0)

    # -- OSC feedback handlers (server threads) -----------------------------
    def _on_volume(self, address: str, *args) -> None:
        n = track_index(address)
        if n is None or not args:
            return
        value = float(args[0])
        with self._lock:
            if n not in self._tracks:
                return
            self._tracks[n]["volume"] = value
        key = volume_key(n)
        if self._echo.is_echo(key, value):
            return
        if self.on_param_value:
            self.on_param_value(key, value)

    def _on_switch(self, address: str, kind: str, *args) -> None:
        n = track_index(address)
        if n is None or not args:
            return
        on = bool(round(float(args[0])))
        with self._lock:
            if n not in self._tracks:
                return
            self._tracks[n][kind] = on
        key = switch_key(n, kind)
        if self._echo.is_echo(key, 1.0 if on else 0.0):
            return
        if self.on_switch_state:
            self.on_switch_state(key, on)

    def _on_name(self, address: str, *args) -> None:
        n = track_index(address)
        if n is None or not args:
            return
        name = str(args[0])
        with self._lock:
            t = self._tracks.get(n)
            if t is None or t["name"] == name:
                return   # unknown slot or no actual change -> no refresh
            t["name"] = name
        # a real label change -> refresh knob captions (debounced serial write)
        self._schedule_bank_refresh()

    def _on_other(self, address: str, *args) -> None:
        log.debug("OSC (unmapped) %s %s", address, args)

    def _schedule_bank_refresh(self) -> None:
        if self.on_bank_changed is None:
            return
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        self._refresh_timer = threading.Timer(_REFRESH_DELAY, self._do_bank_refresh)
        self._refresh_timer.daemon = True
        self._refresh_timer.start()

    def _do_bank_refresh(self) -> None:
        try:
            if self.on_bank_changed:
                self.on_bank_changed(self.current_bank())
        except Exception:
            # timer thread: a failed refresh (e.g. serial timeout) must not
            # take the thread down silently mid-session
            log.exception("bank refresh failed")
