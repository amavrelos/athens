"""API-client layer: owns the physical links to the ROTO-CONTROL.

- transport.py : the serial (config) channel + a loopback for offline runs
- midi.py      : the USB-MIDI (real-time value) channel
- client.py    : RotoControl, the high-level facade the bridge talks to
"""
from .client import RotoControl  # noqa: F401
from .transport import LoopbackTransport, SerialTransport, Transport  # noqa: F401
