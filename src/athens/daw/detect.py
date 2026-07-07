"""Sense which DAW is feeding Athens, so the user needn't pass --daw.

Each Athens-facing feed self-identifies over its own transport — the same idea
the device's native integrations use (Logic/Ableton/Bitwig announce a daw_id to
the ROTO directly and never route through Athens). Athens only mediates the
DAWs without native ROTO support, and each says who it is:

  * REAPER — the reascript (roto_fx_feed.lua) writes a `heartbeat` file ~1/s
    tagged "reaper"; a fresh mtime means REAPER is live AND configured.
  * Cubase — the MIDI Remote script answers a HELLO query on the roto-bridge
    pair with "cubase".

`detect_daw` resolves the `auto` choice; an explicit `--daw` always wins. It's
authoritative in a way process-sniffing isn't: it reports the DAW that is
actually talking to Athens, not merely one that happens to be open.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)


def reaper_feed_live(gone_after: float | None = None) -> bool:
    """True if REAPER's reascript heartbeat is fresh (the Lua beats ~1/s)."""
    from .fx_feed import DAW_GONE_TIMEOUT_S, HEARTBEAT_FILE, default_feed_dir
    limit = DAW_GONE_TIMEOUT_S if gone_after is None else gone_after
    hb = default_feed_dir() / HEARTBEAT_FILE
    try:
        return (time.time() - hb.stat().st_mtime) < limit
    except OSError:
        return False


def cubase_bridge_live(timeout: float = 1.0) -> bool:
    """Open the roto-bridge pair, ask 'who?', and wait for the MIDI Remote
    script's HELLO 'cubase' reply."""
    try:
        import mido
    except Exception:      # noqa: BLE001 - no midi extra installed
        return False
    from . import cubase_contract as wire

    def _match(names):
        return next((n for n in names if "roto-bridge" in n.lower()), None)
    inp = _match(mido.get_input_names())
    outp = _match(mido.get_output_names())
    if not inp or not outp:
        return False

    got = {"cubase": False}

    def _cb(msg):
        m = wire.parse(bytes(msg.bytes()))
        if m and m.cmd is wire.Cmd.HELLO and m.direction == wire.DIR_TO_ATHENS:
            got["cubase"] = True

    pin = pout = None
    try:
        pin = mido.open_input(inp, callback=_cb)
        pout = mido.open_output(outp)
        # Re-ask periodically: after "restart both", Cubase can take a few
        # seconds to bind its MIDI Remote ports, so a single WHO at t=0 misses
        # it and Athens wrongly falls back to reaper. Keep asking until timeout.
        deadline = time.time() + timeout
        while time.time() < deadline and not got["cubase"]:
            pout.send(mido.parse(list(wire.as_control(wire.hello()))))   # WHO
            step = time.time() + 0.5
            while time.time() < step and not got["cubase"]:
                time.sleep(0.02)
    except Exception as exc:      # noqa: BLE001 - the probe is best-effort
        log.debug("cubase probe failed: %s", exc)
    finally:
        try:
            if pin is not None:
                pin.callback = None   # avoid the close-with-live-callback deadlock
        except Exception:      # noqa: BLE001
            pass
        for p in (pin, pout):
            try:
                if p is not None:
                    p.close()
            except Exception:      # noqa: BLE001
                pass
    return got["cubase"]


def make_source(daw: str):
    """Create a fresh DAW source by name — shared by the CLI, the UI shell, and
    the runtime hot-swap monitor so the three can't drift."""
    if daw == "cubase":
        from .cubase_source import CubaseSysexSource
        return CubaseSysexSource()
    if daw == "protools":
        from .protools_source import ProToolsSource
        return ProToolsSource()
    if daw == "system":
        from .system_source import SystemSource
        return SystemSource()
    from .reaper_source import ReaperSysexSource, install_reascript
    install_reascript()      # keep REAPER's copy of the feed current
    return ReaperSysexSource()


def detect_daw(default: str = "reaper", cubase_timeout: float = 3.0) -> str:
    """Resolve 'auto' to the DAW that is actually feeding Athens — at STARTUP.

    REAPER first (an instant file-stat); Cubase second (a ~1s MIDI probe). The
    probe is safe HERE because it runs before the device port is opened and starts
    flooding. If both are live REAPER wins — pass --daw to be explicit. `system`
    is never auto-picked (it isn't a DAW feed); ask for it directly.

    There is deliberately NO runtime equivalent that opens a probe port: once the
    device port is live, opening ANY MIDI input port spins forever in a process-
    wide CoreMIDI lock (LocalMIDIReceiverList::Add) while holding the GIL, which
    freezes every thread (un-interruptible hang). The runtime
    monitor in BridgeService therefore detects only what's free: REAPER's
    heartbeat file-stat, and an already-open Cubase source on its own port.
    """
    if reaper_feed_live():
        log.info("auto-detect DAW: reaper (reascript heartbeat is live)")
        return "reaper"
    if cubase_bridge_live(cubase_timeout):
        log.info("auto-detect DAW: cubase (bridge script answered HELLO)")
        return "cubase"
    log.info("auto-detect DAW: %s (default — no feed announced itself)", default)
    return default
