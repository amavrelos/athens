"""Unit tests for ReaperBackend handler logic and the EchoGate (no OSC server
needed — handlers are called directly with the post-fix signatures)."""
import time

from athens.daw.echo import EchoGate
from athens.daw.reaper import ReaperBackend


def test_switch_handler_takes_plain_kind_string():
    be = ReaperBackend()
    seen = []
    be.on_switch_state = lambda k, on: seen.append((k, on))
    be._on_switch("/track/3/mute", "mute", 1.0)
    assert seen == [("track/3/mute", True)]
    assert be._tracks[3]["mute"] is True


def test_unchanged_name_schedules_no_refresh():
    be = ReaperBackend()
    be.on_bank_changed = lambda bank: None
    be._on_name("/track/2/name", "Track 2")      # same as the seeded default
    assert be._refresh_timer is None
    be._on_name("/track/2/name", "Bass")         # a real change
    assert be._refresh_timer is not None
    be._refresh_timer.cancel()


def test_switch_echo_is_suppressed_but_state_stored():
    be = ReaperBackend()
    be._client = object()   # sentinel: pretend OSC is up (send will not run)
    seen = []
    be.on_switch_state = lambda k, on: seen.append(on)
    be._echo.sent("track/3/mute", 1.0)           # as set_switch would record
    be._on_switch("/track/3/mute", "mute", 1.0)  # REAPER's echo
    assert seen == []                            # not re-driven
    assert be._tracks[3]["mute"] is True         # but state updated


def test_echo_gate_handles_full_sweep():
    gate = EchoGate()
    sweep = [0.50, 0.55, 0.60]
    for v in sweep:
        gate.sent("k", v)
    # echoes may come back in order; each one is consumed exactly once
    assert all(gate.is_echo("k", v) for v in sweep)
    # a genuine external change afterwards passes through
    assert not gate.is_echo("k", 0.70)


def test_echo_gate_expires_stale_entries():
    gate = EchoGate(ttl=0.01)
    gate.sent("k", 0.5)
    time.sleep(0.03)
    assert not gate.is_echo("k", 0.5)   # too old to be this echo
