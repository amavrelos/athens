"""Echo suppression for DAW feedback loops.

When we push a value to the DAW (OSC send), the DAW echoes it back on its
feedback channel. Re-forwarding that echo to the device re-drives the motor
against the user's hand. `EchoGate` remembers every value we sent per key
(not just the latest — a knob sweep has many in flight) and lets the feedback
handler drop matching echoes exactly once each.

Thread-safe: values are recorded from the MIDI callback thread and checked
from OSC server threads. Entries expire after `ttl` so a dead peer can't
poison the gate, and per-key history is bounded.
"""
from __future__ import annotations

import threading
import time
from collections import deque

DEFAULT_EPS = 1e-3
DEFAULT_TTL = 2.0      # seconds a sent value stays eligible to eat its echo
_MAX_PENDING = 64      # per-key bound on in-flight sends


class EchoGate:
    def __init__(self, eps: float = DEFAULT_EPS, ttl: float = DEFAULT_TTL):
        self._eps = eps
        self._ttl = ttl
        self._pending: dict[str, deque] = {}
        self._lock = threading.Lock()

    def sent(self, key: str, value: float) -> None:
        """Record a value we just pushed toward the DAW."""
        now = time.monotonic()
        with self._lock:
            q = self._pending.setdefault(key, deque(maxlen=_MAX_PENDING))
            q.append((value, now))

    def is_echo(self, key: str, value: float) -> bool:
        """True if this feedback value matches a recent send; consumes it."""
        now = time.monotonic()
        with self._lock:
            q = self._pending.get(key)
            if not q:
                return False
            while q and now - q[0][1] > self._ttl:
                q.popleft()
            for i, (v, _) in enumerate(q):
                if abs(v - value) < self._eps:
                    del q[i]
                    return True
            return False
