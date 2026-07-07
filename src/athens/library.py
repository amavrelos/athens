"""SetupLibrary: the local store of setups — the source of truth.

Zero-flash policy: the device is a deployment target, never the archive.
Setups live here (JSON-native dicts, optionally persisted to a file); editing
marks slots dirty; `deploy()` is the explicit, user-initiated flash write
(simulated until hardware bring-up).

Control dicts mirror the serial-protocol fields (protocol/codec.py) so a
deploy can build KnobConfig/SwitchConfig directly:

  knob:   {name, mode: CC7|CC14|NRPN7|NRPN14, channel 1-16, param, nrpn,
           min, max, colour 0-82, haptic: KNOB|STEP|INDENT, steps}
  switch: {name, mode: CC7|CC14|NRPN7|NRPN14|PC|NOTE, channel, param, nrpn,
           min, max, colour, led_on, led_off, toggle}

Export/import uses a shape compatible with the official app's JSON export
({version, type: "MIDI", name, index, knobs[], buttons[]}).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

NUM_SETUPS = 64
SLOTS_PER_KIND = 32
KINDS = ("knob", "switch")

KNOB_DEFAULTS = {"name": "", "mode": "CC7", "channel": 1, "param": 0, "nrpn": 0,
                 "min": 0, "max": 127, "colour": 0, "haptic": "KNOB", "steps": 0,
                 "indent1": 255, "indent2": 255, "step_names": []}
SWITCH_DEFAULTS = {"name": "", "mode": "CC7", "channel": 1, "param": 0, "nrpn": 0,
                   "min": 0, "max": 127, "colour": 0, "led_on": 0, "led_off": 0,
                   "toggle": True, "steps": 0, "step_names": []}


def _fresh(defaults: dict) -> dict:
    """Copy defaults without sharing the mutable list values."""
    return {k: (list(v) if isinstance(v, list) else v) for k, v in defaults.items()}


def _blank_setup(name: str = "") -> dict:
    return {"name": name,
            "knobs": {},      # str(slot) -> control dict (JSON-friendly keys)
            "switches": {},
            "dirty": False,   # local edits not yet deployed
            "deployed": False}


class SetupLibrary:
    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else None
        self._setups: dict[int, dict] = {}
        if self._path and self._path.exists():
            self._load()

    # -- queries -------------------------------------------------------------
    def list(self) -> list:
        out = []
        for i in sorted(self._setups):
            s = self._setups[i]
            out.append({"index": i, "name": s["name"],
                        "knobs": len(s["knobs"]), "switches": len(s["switches"]),
                        "dirty": s["dirty"], "deployed": s["deployed"]})
        return out

    def get(self, index: int) -> dict:
        self._check_index(index)
        s = self._setups.get(index)
        if s is None:
            return {"index": index, **_blank_setup(), "exists": False}
        return {"index": index, **s, "exists": True}

    # -- mutations -----------------------------------------------------------
    def set_name(self, index: int, name: str) -> None:
        s = self._ensure(index)
        s["name"] = str(name)[:12]
        self._touch(s)

    def update_control(self, index: int, kind: str, slot: int,
                       fields: Optional[dict]) -> dict:
        """Merge fields into a control (creating it), or clear it (fields=None)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if not 0 <= slot < SLOTS_PER_KIND:
            raise ValueError(f"slot out of range: {slot}")
        s = self._ensure(index)
        bucket = s["knobs"] if kind == "knob" else s["switches"]
        key = str(slot)
        if fields is None:
            bucket.pop(key, None)
            self._touch(s)
            return {}
        defaults = KNOB_DEFAULTS if kind == "knob" else SWITCH_DEFAULTS
        current = bucket.get(key, _fresh(defaults))
        unknown = set(fields) - set(defaults)
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        current.update(fields)
        current["name"] = str(current["name"])[:12]
        bucket[key] = current
        self._touch(s)
        return current

    def deploy(self, index: int) -> dict:
        """Explicit write-to-device. Until hardware bring-up this only flips
        the bookkeeping (the UI's unsaved -> writing -> on-device flow)."""
        s = self._ensure(index)
        s["dirty"] = False
        s["deployed"] = True
        self._save()
        return {"index": index, "deployed": True}

    def put_from_device(self, index: int, setup: dict) -> None:
        """Land a device-dump setup: it IS the device state, so in-sync."""
        self._check_index(index)
        s = _blank_setup(str(setup.get("name", ""))[:12])
        for slot, fields in setup.get("knobs", {}).items():
            s["knobs"][str(slot)] = {**_fresh(KNOB_DEFAULTS),
                                     **{k: v for k, v in fields.items()
                                        if k in KNOB_DEFAULTS}}
        for slot, fields in setup.get("switches", {}).items():
            s["switches"][str(slot)] = {**_fresh(SWITCH_DEFAULTS),
                                        **{k: v for k, v in fields.items()
                                           if k in SWITCH_DEFAULTS}}
        s["dirty"] = False
        s["deployed"] = True
        self._setups[index] = s
        self._save()

    # -- import/export ---------------------------------------------------------
    def export_setup(self, index: int) -> dict:
        s = self.get(index)
        return {"version": 1, "type": "MIDI", "name": s["name"], "index": index,
                "knobs": [{"index": int(k), **v} for k, v in sorted(
                    s["knobs"].items(), key=lambda kv: int(kv[0]))],
                "buttons": [{"index": int(k), **v} for k, v in sorted(
                    s["switches"].items(), key=lambda kv: int(kv[0]))]}

    def import_setup(self, data: dict, index: Optional[int] = None) -> int:
        if data.get("type") != "MIDI":
            raise ValueError("only type=MIDI setup files are supported here")
        target = index if index is not None else int(data.get("index", 0))
        self._check_index(target)
        s = _blank_setup(str(data.get("name", ""))[:12])
        for item in data.get("knobs", []):
            slot = int(item.get("index", 0))
            fields = {k: v for k, v in item.items() if k in KNOB_DEFAULTS}
            s["knobs"][str(slot)] = {**_fresh(KNOB_DEFAULTS), **fields}
        for item in data.get("buttons", []):
            slot = int(item.get("index", 0))
            fields = {k: v for k, v in item.items() if k in SWITCH_DEFAULTS}
            s["switches"][str(slot)] = {**_fresh(SWITCH_DEFAULTS), **fields}
        s["dirty"] = True
        self._setups[target] = s
        self._save()
        return target

    # -- internals ----------------------------------------------------------------
    def _ensure(self, index: int) -> dict:
        self._check_index(index)
        if index not in self._setups:
            self._setups[index] = _blank_setup()
        return self._setups[index]

    @staticmethod
    def _check_index(index: int) -> None:
        if not 0 <= index < NUM_SETUPS:
            raise ValueError(f"setup index out of range: {index}")

    def _touch(self, s: dict) -> None:
        s["dirty"] = True
        self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({str(i): s for i, s in
                                          self._setups.items()}, indent=1))

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._setups = {int(i): s for i, s in raw.items()}
        except (json.JSONDecodeError, OSError, ValueError):
            log.exception("could not load library %s; starting empty", self._path)


def seed_demo(lib: SetupLibrary) -> None:
    """Populate the demo library so the editor has something real to show."""
    lib.set_name(0, "REAPER Mix")
    tracks = ["Kick", "Snare", "Bass", "Keys", "Gtr", "Vox", "FX", "Master"]
    for i, n in enumerate(tracks):
        lib.update_control(0, "knob", i, {"name": n, "param": 0x10 + i,
                                          "colour": i + 1})
        lib.update_control(0, "switch", i, {"name": f"M {n}", "param": 0x66 + i,
                                            "colour": 14, "led_on": 14})
        lib.update_control(0, "switch", i + 8, {"name": f"S {n}",
                                                "param": 0x6E + i,
                                                "colour": 17, "led_on": 17})
    lib.deploy(0)   # pretend it's already on the device

    lib.set_name(1, "Synth Perf")
    for i, (n, addr) in enumerate([("Cutoff", 74), ("Resonance", 71),
                                   ("Env Amt", 300), ("LFO Rate", 301)]):
        lib.update_control(1, "knob", i, {"name": n, "mode": "NRPN14",
                                          "nrpn": addr, "max": 16383,
                                          "colour": 24 + i})
