"""The serializer: pure encode/decode of the ROTO-CONTROL Serial API framing.

No I/O here — everything is byte-in/byte-out so it is fully unit-testable
without hardware. See codec.py for builders/parsers and constants.py for the
protocol enumerations.
"""
from . import codec, constants  # noqa: F401
