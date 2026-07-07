"""The cross-DAW Link registry: plugin name -> canonical device identity.

A learned map lives in device flash keyed by an 8-byte identity hash. Link lets
one map serve every DAW by substituting a canonical hash for the per-DAW default
(see docs/cross-daw-link-passthrough.md). Both the in-process REAPER bridge and
the standalone proxy resolve `plugin-links.json` entries the same way — this is
that one shared parse, so the two can't drift.
"""
from __future__ import annotations

from typing import Optional


def hash_from_entry(entry) -> Optional[bytes]:
    """A plugin-links.json entry -> its canonical 8-byte device hash, or None if
    the entry is absent or malformed."""
    if isinstance(entry, dict) and "hash" in entry:
        try:
            h = bytes.fromhex(entry["hash"])
        except (ValueError, TypeError):
            return None
        return h if len(h) == 8 else None
    return None
