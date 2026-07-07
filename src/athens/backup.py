"""Device backup: read everything off the ROTO into library-format data.

All reads — zero flash writes, per the zero-flash policy. This is the engine
behind the Library's snapshot feature and the answer to the firmware-update
data loss: dump_setups()/dump_plugins() pull the device's stored state over
the serial config channel; snapshot_into_library() lands setups in the local
SetupLibrary marked in-sync.

A full 64-setup dump is 64 × (32+32) reads (~4k round-trips); pass `progress`
to surface it in the UI.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from .library import SLOTS_PER_KIND, SetupLibrary
from .protocol import codec
from .protocol.constants import ControlMode, ControlType, KnobHaptic, SwitchHaptic
from .roto.client import RotoControl

log = logging.getLogger(__name__)

NUM_SETUPS = 64

_MODE_NAMES = {int(ControlMode.CC_7BIT): "CC7", int(ControlMode.CC_14BIT): "CC14",
               int(ControlMode.NRPN_7BIT): "NRPN7", int(ControlMode.NRPN_14BIT): "NRPN14",
               int(ControlMode.PROGRAM_CHANGE): "PC", int(ControlMode.NOTE): "NOTE"}
_HAPTIC_NAMES = {int(KnobHaptic.KNOB_300): "KNOB", int(KnobHaptic.KNOB_N_STEP): "STEP",
                 int(KnobHaptic.KNOB_300_CENTRE_INDENT): "INDENT"}

Progress = Optional[Callable[[str, int, int], None]]   # (stage, done, total)


def _knob_to_library(cfg) -> dict:
    return {"name": cfg.name, "mode": _MODE_NAMES.get(int(cfg.control_mode), "CC7"),
            "channel": cfg.channel, "param": cfg.param, "nrpn": cfg.nrpn_address,
            "min": cfg.min_value, "max": cfg.max_value, "colour": cfg.colour,
            "haptic": _HAPTIC_NAMES.get(int(cfg.haptic), "KNOB"),
            "steps": cfg.steps, "indent1": cfg.indent1, "indent2": cfg.indent2,
            "step_names": list(cfg.step_names)}


def _switch_to_library(cfg) -> dict:
    return {"name": cfg.name, "mode": _MODE_NAMES.get(int(cfg.control_mode), "CC7"),
            "channel": cfg.channel, "param": cfg.param, "nrpn": cfg.nrpn_address,
            "min": cfg.min_value, "max": cfg.max_value, "colour": cfg.colour,
            "led_on": cfg.led_on, "led_off": cfg.led_off,
            "toggle": bool(int(cfg.haptic)), "steps": cfg.steps,
            "step_names": list(cfg.step_names)}


def dump_setup(roto: RotoControl, index: int) -> Optional[dict]:
    """One setup in library format, or None if the slot is entirely empty."""
    info = roto.setup_info(index)
    knobs, switches = {}, {}
    for i in range(SLOTS_PER_KIND):
        cfg = roto.read_knob_config(index, i)
        if cfg is not None:
            knobs[str(i)] = _knob_to_library(cfg)
        cfg = roto.read_switch_config(index, i)
        if cfg is not None:
            switches[str(i)] = _switch_to_library(cfg)
    if not (info.name or knobs or switches):
        return None
    return {"name": info.name, "knobs": knobs, "switches": switches}


def dump_setups(roto: RotoControl, indices: Iterable[int] = range(NUM_SETUPS),
                progress: Progress = None) -> dict:
    """{setup_index: library-format setup} for every non-empty setup."""
    indices = list(indices)
    out = {}
    for n, index in enumerate(indices):
        setup = dump_setup(roto, index)
        if setup is not None:
            out[index] = setup
        if progress:
            progress("setups", n + 1, len(indices))
    log.info("dumped %d non-empty setups", len(out))
    return out


def dump_plugins(roto: RotoControl, progress: Progress = None) -> list:
    """Every stored plugin map: {hash, name, knobs: {slot: {...}}, switches}."""
    out = []
    for plugin in roto.iter_plugins():
        knobs, switches = {}, {}
        for i in range(0x40):
            cfg = roto.read_plugin_knob_config(plugin.hash, i)
            if cfg is not None:
                knobs[str(i)] = {"name": cfg.name,
                                 "param_index": cfg.mapped_param_index,
                                 "param_hash": cfg.mapped_param_hash.hex(),
                                 "macro": cfg.macro_param,
                                 "min": cfg.min_value, "max": cfg.max_value,
                                 "colour": cfg.colour, "steps": cfg.steps}
            sw = roto.read_plugin_switch_config(plugin.hash, i)
            if sw is not None:
                switches[str(i)] = {"name": sw["name"],
                                    "param_index": sw["mapped_param_index"],
                                    "param_hash": sw["mapped_param_hash"].hex(),
                                    "min": sw["min_value"], "max": sw["max_value"],
                                    "colour": sw["colour"], "led_on": sw["led_on"],
                                    "led_off": sw["led_off"]}
        out.append({"hash": plugin.hash.hex(), "name": plugin.name,
                    "type": plugin.plugin_type, "knobs": knobs,
                    "switches": switches})
        if progress:
            progress("plugins", len(out), 0)
    log.info("dumped %d plugin maps", len(out))
    return out


def snapshot_into_library(setups: dict, lib: SetupLibrary) -> int:
    """Land a device dump in the local library, marked in-sync (they ARE what
    the device holds right now)."""
    for index, setup in setups.items():
        lib.put_from_device(int(index), setup)
    return len(setups)


# --------------------------------------------------------------------------
# Restore: library -> device. Diff-based per the zero-flash policy — read
# each slot first (reads are free) and write only what actually differs, in
# one config-update session per setup. Restore-after-wipe writes exactly the
# configured slots; re-restoring an in-sync setup writes nothing.
# --------------------------------------------------------------------------

_MODE_VALUES = {v: k for k, v in _MODE_NAMES.items()}
_HAPTIC_VALUES = {v: k for k, v in _HAPTIC_NAMES.items()}


def _norm(defaults: dict, fields: dict) -> dict:
    """Library fields -> canonical comparable form (defaults filled, name
    truncated as the wire will truncate it, step names trimmed to steps)."""
    f = {**defaults, **{k: v for k, v in fields.items() if k in defaults}}
    f["name"] = str(f["name"])[:12]
    f["step_names"] = [str(n)[:12] for n in f["step_names"]][:f["steps"]]
    return f


def _knob_from_library(setup_index: int, slot: int, f: dict) -> codec.KnobConfig:
    return codec.KnobConfig(
        setup_index=setup_index, control_index=slot,
        control_mode=ControlMode(_MODE_VALUES[f["mode"]]),
        channel=f["channel"], param=f["param"], nrpn_address=f["nrpn"],
        min_value=f["min"], max_value=f["max"], name=f["name"],
        colour=f["colour"], haptic=KnobHaptic(_HAPTIC_VALUES[f["haptic"]]),
        indent1=f["indent1"], indent2=f["indent2"],
        steps=f["steps"], step_names=list(f["step_names"]))


def _switch_from_library(setup_index: int, slot: int, f: dict) -> codec.SwitchConfig:
    return codec.SwitchConfig(
        setup_index=setup_index, control_index=slot,
        control_mode=ControlMode(_MODE_VALUES[f["mode"]]),
        channel=f["channel"], param=f["param"], nrpn_address=f["nrpn"],
        min_value=f["min"], max_value=f["max"], name=f["name"],
        colour=f["colour"], led_on=f["led_on"], led_off=f["led_off"],
        haptic=SwitchHaptic.TOGGLE if f["toggle"] else SwitchHaptic.PUSH,
        steps=f["steps"], step_names=list(f["step_names"]))


def restore_setup(roto: RotoControl, index: int, setup: dict) -> dict:
    """Sync one library setup onto the device. Returns
    {"written": n, "cleared": n, "skipped": n} — zero writes when in sync."""
    from .library import KNOB_DEFAULTS, SWITCH_DEFAULTS

    pending = []   # deferred write callables, run inside one config session
    written = cleared = skipped = 0

    want_name = str(setup.get("name", ""))[:12]
    if roto.setup_info(index).name != want_name:
        pending.append(lambda: roto.set_setup_name(index, want_name))

    plans = (("knob", KNOB_DEFAULTS, setup.get("knobs", {}),
              roto.read_knob_config, _knob_from_library,
              roto.write_knob_config, ControlType.KNOB, _knob_to_library),
             ("switch", SWITCH_DEFAULTS, setup.get("switches", {}),
              roto.read_switch_config, _switch_from_library,
              roto.write_switch_config, ControlType.SWITCH, _switch_to_library))

    for (_kind, defaults, bucket, read, build, write, ctype, to_lib) in plans:
        for slot in range(SLOTS_PER_KIND):
            want = bucket.get(str(slot))
            have = read(index, slot)
            have_norm = _norm(defaults, to_lib(have)) if have else None
            want_norm = _norm(defaults, want) if want is not None else None
            if want_norm == have_norm:
                skipped += 1
            elif want_norm is None:
                pending.append(lambda ct=ctype, sl=slot:
                               roto.clear_control(index, ct, sl))
                cleared += 1
            else:
                cfg = build(index, slot, want_norm)
                pending.append(lambda c=cfg, w=write: w(c))
                written += 1

    if pending:
        with roto.config_update():
            for fn in pending:
                fn()
    log.info("restore setup %d: %d written, %d cleared, %d unchanged",
             index, written, cleared, skipped)
    return {"written": written, "cleared": cleared, "skipped": skipped}


def restore_setups(roto: RotoControl, setups: dict,
                   progress: Progress = None) -> dict:
    totals = {"written": 0, "cleared": 0, "skipped": 0}
    items = list(setups.items())
    for n, (index, setup) in enumerate(items):
        result = restore_setup(roto, int(index), setup)
        for k in totals:
            totals[k] += result[k]
        if progress:
            progress("restore", n + 1, len(items))
    return totals


def restore_plugin(roto: RotoControl, plugin: dict) -> dict:
    """Recreate one stored plugin map (the firmware-wipe recovery path).
    ADD_PLUGIN is idempotent (PLUGIN_EXISTS is an expected reply)."""
    from .protocol.constants import RespCode
    phash = bytes.fromhex(plugin["hash"])
    name = str(plugin.get("name", ""))[:12]
    written = 0
    with roto.config_update():
        roto._req(codec.add_plugin(phash, name),
                  ok_codes=(RespCode.SUCCESS, RespCode.PLUGIN_EXISTS))
        for slot, f in plugin.get("knobs", {}).items():
            roto._req(codec.set_plugin_knob_config(codec.PluginKnobConfig(
                plugin_hash=phash, control_index=int(slot),
                mapped_param_index=f.get("param_index", 0),
                mapped_param_hash=bytes.fromhex(f.get("param_hash", "00" * 6)),
                macro_param=bool(f.get("macro", False)),
                min_value=f.get("min", 0), max_value=f.get("max", 0x3FFF),
                name=str(f.get("name", ""))[:12], colour=f.get("colour", 0),
                steps=f.get("steps", 0))))
            written += 1
        for slot, f in plugin.get("switches", {}).items():
            roto._req(codec.set_plugin_switch_config(codec.PluginSwitchConfig(
                plugin_hash=phash, control_index=int(slot),
                mapped_param_index=f.get("param_index", 0),
                mapped_param_hash=bytes.fromhex(f.get("param_hash", "00" * 6)),
                min_value=f.get("min", 0), max_value=f.get("max", 0x7F),
                name=str(f.get("name", ""))[:12], colour=f.get("colour", 0),
                led_on=f.get("led_on", 0), led_off=f.get("led_off", 0))))
            written += 1
    log.info("restored plugin '%s' (%d controls)", name, written)
    return {"plugin": name, "written": written}


def restore_plugins(roto: RotoControl, plugins: list,
                    progress: Progress = None) -> dict:
    total = 0
    for n, plugin in enumerate(plugins):
        total += restore_plugin(roto, plugin)["written"]
        if progress:
            progress("plugins", n + 1, len(plugins))
    return {"plugins": len(plugins), "written": total}
