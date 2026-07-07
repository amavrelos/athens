"""Athens' per-user config/state directory.

One home for everything persisted between runs — library.json, param-refs.json,
plugin-links.json, and the DAW folder overrides. Single-sourced so the modules
that read/write it (the service, the script installer, the Link proxy) can't
disagree on where it lives.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    """The directory (not created here — callers mkdir when they write)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "roto-reaper"
    if os.name == "nt":  # pragma: no cover - not exercised on CI
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "roto-reaper"
    return Path.home() / ".config" / "roto-reaper"  # pragma: no cover
