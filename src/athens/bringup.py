"""Hardware bring-up harness: an ordered, logged, READ-ONLY probe session.

Run once when the ROTO-CONTROL is first plugged in (`roto-reaper bringup`).
It answers the open protocol questions and takes the first real backup:

  1. serial     — link proof: firmware version, mode, current setup
  2. backup     — full device dump (setups + plugin maps) to a JSON file
  3. handshake  — native DAW SysEx handshake incl. the Logic daw-id
                  masquerade: does the device accept it and send CONNECTED?
  4. knobs      — user turns each encoder: which CCs appear, and is the
                  14-bit layout the expected MSB/MSB+32 pairing?
  5. touch      — user touches each knob: are events at CC 52+i?
  6. buttons    — user presses every button: which emit (and which are
                  firmware-reserved and emit nothing)

Zero flash writes by design. The two write-requiring questions (haptic-enum
semantics vs the newer firmware, palette 83-vs-86) are intentionally NOT
probed here — they get a supervised follow-up.

Everything is injectable for offline tests: pass fakes for `roto`, `port`,
and a ScriptedUI; the CLI builds the real ones.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import backup
from .roto.sysex_client import MidiPort, RotoSysexClient
from .sysex import codec as sx
from .sysex.constants import (
    ENCODER_FIRST_CC, NUM_ENCODERS, TOUCH_FIRST_CC, VALUE_MIDI_CHANNEL,
    General, Group,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Small infrastructure
# --------------------------------------------------------------------------

class ConsoleUI:
    def say(self, msg: str) -> None:
        print(msg)

    def ask(self, msg: str) -> str:
        return input(msg + " [Enter] ")


class ScriptedUI:
    """Test double: each ask() pops an action; callables are executed (used to
    inject device traffic at exactly the moment a user would act)."""

    def __init__(self, actions: Optional[list] = None):
        self.actions = list(actions or [])
        self.transcript: List[str] = []

    def say(self, msg: str) -> None:
        self.transcript.append(msg)

    def ask(self, msg: str) -> str:
        self.transcript.append("? " + msg)
        if self.actions:
            action = self.actions.pop(0)
            if callable(action):
                action()
                return ""
            return str(action)
        return ""


class TrafficLog:
    """Chains onto a MidiPort's receive slot (after the client claimed it) and
    records every inbound message for analysis."""

    def __init__(self) -> None:
        self.messages: List[bytes] = []

    def attach(self, port: MidiPort) -> None:
        orig = port.on_receive

        def tap(data: bytes) -> None:
            if orig is not None:
                orig(data)
            self.messages.append(bytes(data))
        port.on_receive = tap

    def mark(self) -> int:
        return len(self.messages)

    def value_ccs(self, since: int = 0) -> list:
        """(cc, value) for CCs on the value channel since a mark."""
        out = []
        for m in self.messages[since:]:
            if len(m) == 3 and (m[0] & 0xF0) == 0xB0 \
                    and (m[0] & 0x0F) == VALUE_MIDI_CHANNEL:
                out.append((m[1], m[2]))
        return out

    def notes(self, since: int = 0) -> list:
        return [(m[1], m[2]) for m in self.messages[since:]
                if len(m) == 3 and (m[0] & 0xF0) in (0x90, 0x80)]

    def sysex_cmds(self, since: int = 0) -> set:
        out = set()
        for m in self.messages[since:]:
            if m and m[0] == 0xF0:
                try:
                    p = sx.parse_sysex(m)
                    out.add((p.group, p.command))
                except sx.ProtocolError:
                    pass
        return out


@dataclass
class StepResult:
    name: str
    ok: bool
    details: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Report:
    stamp: str
    results: List[StepResult] = field(default_factory=list)

    def add(self, result: StepResult) -> StepResult:
        self.results.append(result)
        return result

    def save(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"bringup-{self.stamp}.json"
        path.write_text(json.dumps(
            {"stamp": self.stamp,
             "results": [{"name": r.name, "ok": r.ok, "details": r.details,
                          "note": r.note} for r in self.results]}, indent=1))
        return path


# --------------------------------------------------------------------------
# Pure analysers (unit-testable)
# --------------------------------------------------------------------------

def analyse_encoders(cc_events: list) -> dict:
    """Which encoder MSB CCs appeared, and which have a paired +32 LSB."""
    seen = {cc for cc, _ in cc_events}
    msbs = sorted(cc for cc in seen
                  if ENCODER_FIRST_CC <= cc < ENCODER_FIRST_CC + NUM_ENCODERS)
    pairs = {msb: msb + 32 for msb in msbs if (msb + 32) in seen}
    other = sorted(cc for cc in seen
                   if cc not in msbs and cc not in set(pairs.values()))
    return {"msb_ccs": msbs, "paired_lsb": pairs,
            "is_14bit": bool(pairs) and len(pairs) == len(msbs),
            "nrpn_seen": bool({6, 38, 98, 99} & seen),
            "other_ccs": other}


def analyse_touch(cc_events: list) -> dict:
    knobs = sorted({cc - TOUCH_FIRST_CC for cc, _ in cc_events
                    if TOUCH_FIRST_CC <= cc < TOUCH_FIRST_CC + NUM_ENCODERS})
    stray = sorted({cc for cc, _ in cc_events
                    if not TOUCH_FIRST_CC <= cc < TOUCH_FIRST_CC + NUM_ENCODERS})
    return {"touch_knobs": knobs, "stray_ccs": stray}


def analyse_buttons(cc_events: list, note_events: list) -> dict:
    return {"button_ccs": sorted({cc for cc, _ in cc_events}),
            "button_notes": sorted({n for n, _ in note_events})}


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def step_serial(roto, report: Report, ui) -> bool:
    try:
        fw = roto.firmware_version()
        mode = roto.mode()
        setup = roto.current_setup()
        r = report.add(StepResult("serial", True, {
            "firmware": str(fw), "mode": str(mode),
            "setup": {"index": setup.index, "name": setup.name}}))
        ui.say(f"  firmware {fw} · {mode} · setup #{setup.index} {setup.name!r}")
        return r.ok
    except Exception as exc:  # noqa: BLE001 - keep the session going
        report.add(StepResult("serial", False, note=str(exc)))
        ui.say(f"  serial FAILED: {exc}")
        return False


def step_backup(roto, report: Report, ui, out_dir: Path, stamp: str) -> bool:
    try:
        ui.say("  dumping all setups (reads only — this can take a while)…")
        setups = backup.dump_setups(
            roto, progress=lambda st, d, t: ui.say(f"    {st} {d}/{t}")
            if d % 16 == 0 or d == t else None)
        plugins = backup.dump_plugins(roto)
        path = out_dir / f"device-backup-{stamp}.json"
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stamp": stamp, "setups": setups,
                                    "plugins": plugins}, indent=1))
        report.add(StepResult("backup", True, {
            "setups": len(setups), "plugins": len(plugins),
            "file": str(path)}))
        ui.say(f"  BACKED UP: {len(setups)} setups, {len(plugins)} plugin maps "
               f"-> {path}")
        return True
    except Exception as exc:  # noqa: BLE001
        report.add(StepResult("backup", False, note=str(exc)))
        ui.say(f"  backup FAILED: {exc}")
        return False


def step_handshake(client: RotoSysexClient, traffic: TrafficLog,
                   report: Report, ui) -> bool:
    connected = []
    client.on_connected = lambda: connected.append(True)
    mark = traffic.mark()
    client.start()          # DAW_STARTED; PING auto-answered with daw_id=3
    ui.ask("Sent DAW_STARTED (masquerading as Logic Pro). Watch the device — "
           "press Enter once it shows a DAW connection (or ~10s pass)")
    cmds = traffic.sysex_cmds(mark)
    details = {
        "ping_seen": (int(Group.GENERAL), int(General.PING_DAW)) in cmds,
        "connected_seen": bool(connected),
        "sysex_seen": sorted(f"{g:02X}/{c:02X}" for g, c in cmds),
        "daw_id_masquerade": "accepted" if connected else "unconfirmed"}
    ok = bool(connected)
    report.add(StepResult("handshake", ok, details,
                          note="" if ok else "no ROTO_DAW_CONNECTED — device "
                          "may gate on daw_id or need mode change"))
    ui.say(f"  ping={details['ping_seen']} connected={details['connected_seen']}")
    return ok


def step_knobs(traffic: TrafficLog, report: Report, ui) -> bool:
    mark = traffic.mark()
    ui.ask("Slowly turn EACH of the 8 encoders in order (1..8), then Enter")
    analysis = analyse_encoders(traffic.value_ccs(mark))
    ok = len(analysis["msb_ccs"]) > 0
    report.add(StepResult("knobs", ok, analysis,
                          note="" if ok else "no encoder CCs captured — check "
                          "device mode / MIDI port"))
    ui.say(f"  encoder CCs: {analysis['msb_ccs']}  14-bit pairs: "
           f"{analysis['paired_lsb']}  nrpn: {analysis['nrpn_seen']}")
    return ok


def step_touch(traffic: TrafficLog, report: Report, ui) -> bool:
    mark = traffic.mark()
    ui.ask("TOUCH each knob top briefly WITHOUT turning (1..8), then Enter")
    analysis = analyse_touch(traffic.value_ccs(mark))
    ok = len(analysis["touch_knobs"]) > 0
    report.add(StepResult("touch", ok, analysis))
    ui.say(f"  touch events for knobs: {analysis['touch_knobs']}")
    return ok


def step_buttons(traffic: TrafficLog, report: Report, ui) -> bool:
    mark = traffic.mark()
    ui.ask("Press EVERY button once, one at a time — including the left-side "
           "and nav keys — then Enter")
    analysis = analyse_buttons(traffic.value_ccs(mark), traffic.notes(mark))
    ok = bool(analysis["button_ccs"] or analysis["button_notes"])
    report.add(StepResult("buttons", ok, analysis,
                          note="buttons that emitted nothing are "
                          "firmware-reserved (nav keys expected here)"))
    ui.say(f"  button CCs: {analysis['button_ccs']}  "
           f"notes: {analysis['button_notes']}")
    return ok


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_bringup(roto=None, port: Optional[MidiPort] = None,
                ui=None, out_dir: str = ".",
                stamp: Optional[str] = None,
                skip_backup: bool = False) -> Report:
    ui = ui or ConsoleUI()
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    out = Path(out_dir)
    report = Report(stamp=stamp)

    try:
        ui.say("== 1/6 serial link ==")
        if roto is not None:
            step_serial(roto, report, ui)
            if skip_backup:
                report.add(StepResult("backup", False, note="skipped by flag"))
            else:
                ui.say("== 2/6 first backup ==")
                step_backup(roto, report, ui, out, stamp)
        else:
            report.add(StepResult("serial", False, note="no serial port given"))
            report.add(StepResult("backup", False, note="no serial port given"))
            ui.say("  (no serial port — skipping serial + backup)")

        if port is not None:
            client = RotoSysexClient(port)
            traffic = TrafficLog()
            traffic.attach(port)
            ui.say("== 3/6 DAW handshake ==")
            step_handshake(client, traffic, report, ui)
            ui.say("== 4/6 encoders ==")
            step_knobs(traffic, report, ui)
            ui.say("== 5/6 touch ==")
            step_touch(traffic, report, ui)
            ui.say("== 6/6 buttons ==")
            step_buttons(traffic, report, ui)
        else:
            for name in ("handshake", "knobs", "touch", "buttons"):
                report.add(StepResult(name, False, note="no MIDI port given"))
            ui.say("  (no MIDI port — skipping SysEx/CC probes)")
    finally:
        path = report.save(out)
        ui.say(f"report -> {path}")
        passed = sum(1 for r in report.results if r.ok)
        ui.say(f"{passed}/{len(report.results)} steps ok")
        ui.say("NOT probed (need supervised writes): haptic-enum semantics, "
               "palette 83-vs-86.")
    return report
