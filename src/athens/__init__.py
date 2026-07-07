"""roto-reaper: bridge Melbourne Instruments ROTO-CONTROL to REAPER.

Architecture (mirrors the ROTO-CONTROL Serial API v1.2, see docs/):

    ROTO-CONTROL hardware
        |  USB-CDC serial (config)  +  USB-MIDI (real-time values)
        v
    [ protocol ]  serializer: pure command/response/async codec
        v
    [ roto ]      API client: owns the serial + MIDI ports, high-level facade
        v
    [ bridge ]    DAW-agnostic mapping engine
        v
    [ daw ]       DAW-facing component (ReaperBackend, MockDawBackend, ...)
"""

__version__ = "0.1.0"
