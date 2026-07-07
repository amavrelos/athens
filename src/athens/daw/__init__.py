"""DAW-facing side of the bridge.

A `DawBackend` exposes the parameter model the ROTO knobs should reflect,
accepts value changes coming back from the knobs, and notifies the bridge when
the DAW state changes. The bridge itself is DAW-agnostic; swap the backend to
target a different host (REAPER now, others later).
"""
from .backend import Bank, DawBackend, Param  # noqa: F401
from .mock import MockDawBackend  # noqa: F401
