"""Dialect-neutral bridge helpers: display-name fitting, bank paging, and the
transport push. Kept here (no I/O) so the bridge stays unit-testable offline."""
from __future__ import annotations

from ..sysex.constants import NUM_ENCODERS, STRING_LEN, TransportAction


def short_name(name: str) -> str:
    """Fit a param name into the device's 12-char display while keeping what
    distinguishes it. Hierarchical VST names ('Layer A Osc 1 Frequency') are
    front-loaded with shared context, so the informative part is the tail: drop
    leading whitespace-separated words until it fits, keeping at least the last
    word, then hard-clamp. The identity hash is computed elsewhere from the FULL
    name, so this only changes what the screen shows."""
    limit = STRING_LEN - 1
    if len(name) <= limit:
        return name
    words = name.split()
    while len(words) > 1 and len(" ".join(words)) > limit:
        words = words[1:]
    return " ".join(words)[:limit]


def paged_first_track(current: int, delta: int, track_count: int) -> int:
    """New bank offset after a page step (delta -1/+1), clamped to page-aligned
    windows so paging never drifts off NUM_ENCODERS boundaries (12 tracks ->
    windows start at 0 and 8, never 11)."""
    max_first = max(0, ((max(track_count, 1) - 1) // NUM_ENCODERS) * NUM_ENCODERS)
    return max(0, min(current + delta * NUM_ENCODERS, max_first))


# transport buttons with an LED/state to reflect back (stop/rw/ff have none)
_TRANSPORT_STATE_FIELDS = {
    TransportAction.PLAY: "playing",
    TransportAction.RECORD: "recording",
    TransportAction.SESSION_RECORD: "session_recording",
    TransportAction.LOOP: "loop",
    TransportAction.PUNCH_IN: "punch_in",
    TransportAction.PUNCH_OUT: "punch_out",
    TransportAction.REENABLE_AUTOMATION: "reenable_automation",
}


def push_transport(client, source) -> None:
    """Push the DAW's transport state + the per-action button-LED states to
    the device."""
    st = source.transport()
    client.send_transport(
        playing=st.playing, recording=st.recording,
        session_recording=st.session_recording, loop=st.loop,
        punch_in=st.punch_in, punch_out=st.punch_out,
        reenable_automation=st.reenable_automation)
    for action, attr in _TRANSPORT_STATE_FIELDS.items():
        client.send_transport_led(action, getattr(st, attr))
