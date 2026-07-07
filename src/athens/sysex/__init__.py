"""The native DAW integration protocol (MIDI + SysEx).

This is a SECOND, independent device-facing protocol, distinct from the serial
API in `protocol/`. It's how Melbourne's own Ableton/Bitwig/Logic integrations
drive the device's native MIX / PLUGIN / transport modes, reverse-read from the
Ableton Remote Script shipped inside ROTO-SETUP.app. See docs/DAW-SYSEX-PROTOCOL.md.

Framing: F0 00 22 03 02 <group> <command> <data...> F7
Values : CC on MIDI channel 16 (see constants). Names/formatted values: SysEx.
"""
from . import codec, constants  # noqa: F401
