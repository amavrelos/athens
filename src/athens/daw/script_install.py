"""Auto-install / update the DAW-side companion scripts.

The user shouldn't have to copy a file into REAPER or Cubase by hand — Athens
ships both scripts and, on every launch, drops the current version into the
folder each host loads from. Idempotent and best-effort: it only writes when the
file is missing or has changed (so it can't drift from the bundled copy), and
never raises into the caller.

Each host still needs its own one-time "load / reload" — no tool can drive that:

    Cubase : MIDI Remote Manager -> Scripts -> refresh (or relaunch Cubase)
    REAPER : Actions -> Load ReaScript -> pick it from Scripts/, then run it
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config_dir as _config_dir
from .fx_feed import reaper_resource_dir

log = logging.getLogger(__name__)

CUBASE_VENDOR = "Melbourne Instruments"
CUBASE_DEVICE = "Roto-Control"
CUBASE_SCRIPT = CUBASE_VENDOR + "_" + CUBASE_DEVICE + ".js"
REAPER_SCRIPT = "roto_fx_feed.lua"
REAPER_TOGGLE = "roto_fx_toggle.lua"
# everything sync_reaper deploys: the feed + its one-click start/stop toggle
REAPER_SCRIPTS = (REAPER_SCRIPT, REAPER_TOGGLE)


# --- user overrides: a "Locate" folder for hosts the auto-discovery misses
#     (portable REAPER, a relocated Documents, a non-standard Steinberg dir) ---

def _overrides() -> Dict[str, str]:
    """User-Located folders, e.g. {"cubase": "...", "reaper": "..."} (or {})."""
    try:
        data = json.loads((_config_dir() / "daw_paths.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_override(daw: str, path: Optional[str]) -> None:
    """Persist a user-chosen folder for a DAW (or clear it when path falsey).
    The resolver then targets this folder instead of auto-discovering."""
    cfg = _overrides()
    key = (daw or "").lower()
    if path:
        cfg[key] = str(path)
    else:
        cfg.pop(key, None)
    dest = _config_dir() / "daw_paths.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(cfg, indent=1))


def _bundle_root() -> Path:
    """Where the bundled scripts live: the PyInstaller unpack dir when frozen,
    else the repo root (reaper/ and cubase/ sit beside src/)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[3]


def _bundled(subdir: str, name: str) -> Optional[bytes]:
    try:
        return (_bundle_root() / subdir / name).read_bytes()
    except OSError:
        return None


def _write_if_changed(payload: bytes, dest: Path,
                      force: bool = False) -> Optional[str]:
    """Copy payload to dest when missing or different — or ALWAYS when force
    (the Settings 'Reinstall'/repair button, which re-writes even a byte-
    identical file so a corrupted/again-deleted copy self-heals on demand).
    Returns 'installed'/'updated'/'reinstalled', or None if already current
    (and not forced) or on any OS error."""
    try:
        if dest.is_file() and dest.read_bytes() == payload:
            if not force:
                return None
            action = "reinstalled"
        else:
            action = "updated" if dest.exists() else "installed"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        log.info("%s DAW script: %s", action, dest)
        return action
    except OSError as exc:
        log.warning("DAW script sync failed for %s: %s", dest, exc)
        return None


def _steinberg_driver_roots() -> List[Path]:
    """Every Steinberg host's 'MIDI Remote/Driver Scripts' folder — or the
    user's Located folder if set (auto-discovery is skipped then)."""
    override = _overrides().get("cubase")
    if override:
        root = Path(override)
        deeper = root / "MIDI Remote" / "Driver Scripts"   # picked a host/root
        if deeper.is_dir():
            root = deeper
        return [root] if root.is_dir() else []
    if sys.platform == "darwin":
        base = Path.home() / "Documents" / "Steinberg"
    elif os.name == "nt":  # pragma: no cover - not exercised on CI
        base = Path(os.environ.get("USERPROFILE", str(Path.home()))) \
            / "Documents" / "Steinberg"
    else:  # pragma: no cover - Cubase is macOS/Windows only
        return []
    if not base.is_dir():
        return []
    roots = []
    for host in sorted(base.iterdir()):
        root = host / "MIDI Remote" / "Driver Scripts"
        if root.is_dir():
            roots.append(root)
    return roots


def sync_cubase(force: bool = False) -> List[str]:
    """Drop the MIDI Remote script into every Steinberg host found (the host
    only loads it from Local/<Vendor>/<Device>/<Vendor>_<Device>.js)."""
    payload = _bundled("cubase", CUBASE_SCRIPT)
    if payload is None:
        return []
    notes = []
    for root in _steinberg_driver_roots():
        dest = root / "Local" / CUBASE_VENDOR / CUBASE_DEVICE / CUBASE_SCRIPT
        action = _write_if_changed(payload, dest, force)
        if action:
            notes.append("Cubase script " + action)
    return notes


def reaper_scripts_dir() -> Optional[Path]:
    """REAPER's Scripts/ folder, honouring a user 'Locate' override — or None
    when REAPER isn't installed here / the override is bad."""
    override = _overrides().get("reaper")
    resource = Path(override) if override else reaper_resource_dir()
    return (resource / "Scripts") if resource.is_dir() else None


def sync_reaper(force: bool = False) -> List[str]:
    """Drop the companion Lua scripts (the feed + its start/stop toggle) into
    REAPER's Scripts/ folder. THE one override-aware REAPER installer."""
    scripts = reaper_scripts_dir()
    if scripts is None:
        return []                       # REAPER not installed / bad override
    notes = []
    for name in REAPER_SCRIPTS:
        payload = _bundled("reaper", name)
        if payload is None:
            continue
        action = _write_if_changed(payload, scripts / name, force)
        if action:
            label = "REAPER script" if name == REAPER_SCRIPT else "REAPER toggle"
            notes.append(label + " " + action)
    return notes


def sync(mode: Optional[str], force: bool = False) -> List[str]:
    """Sync the scripts relevant to the chosen DAW mode. Best-effort; returns
    human-readable notes on what changed (empty when everything is current, or
    when force re-wrote nothing because no target folder was found).

    reaper/auto -> the REAPER Lua; cubase/auto -> the Cubase script. An explicit
    single-DAW mode never touches the other host's folder. force re-writes even
    identical files (the Settings Reinstall/repair action)."""
    m = (mode or "reaper").lower()
    notes: List[str] = []
    if m in ("reaper", "auto"):
        notes += sync_reaper(force)
    if m in ("cubase", "auto"):
        notes += sync_cubase(force)
    return notes


def status() -> Dict[str, dict]:
    """Effective target folder per DAW + whether it was found and how — for the
    UI's Locate panel: {daw: {path, found, located}}."""
    ov = _overrides()
    cubase = _steinberg_driver_roots()
    reaper_res = Path(ov["reaper"]) if ov.get("reaper") else reaper_resource_dir()
    return {
        "cubase": {
            "path": str(cubase[0]) if cubase else ov.get("cubase", ""),
            "found": bool(cubase),
            "located": bool(ov.get("cubase")),
        },
        "reaper": {
            "path": str(reaper_res),
            "found": reaper_res.is_dir(),
            "located": bool(ov.get("reaper")),
        },
    }
