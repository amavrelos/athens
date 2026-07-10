"""SerialTransport: the ROTO-unplug path. A removed USB device fails read()
forever, so the reader must STOP and signal a disconnect — not spin on the dead
fd (which floods the log thousands/sec and wedged the app, 2026-07-07)."""
import threading
import time

import serial as pyserial


class _DeadSerial:
    """Fake pyserial: a couple of empty (timeout) reads, then the device is
    'removed' and every read raises — like a real USB unplug."""

    def __init__(self, **kw):
        self.reads = 0
        self.closed = False

    def read(self, n):
        self.reads += 1
        if self.reads > 2:
            raise pyserial.SerialException("read failed: [Errno 6] Device not configured")
        time.sleep(0.05)     # mimic the real read timeout so the reader doesn't
        #                      race past the caller wiring on_disconnect
        return b""

    def write(self, frame):
        pass

    def close(self):
        self.closed = True


def test_unplug_stops_reader_and_signals(monkeypatch):
    monkeypatch.setattr(pyserial, "Serial", _DeadSerial)
    from athens.roto.transport import SerialTransport

    t = SerialTransport("/dev/fake")
    fired = threading.Event()
    t.on_disconnect = fired.set

    # the removed device triggers exactly one disconnect signal, promptly
    assert fired.wait(2.0), "on_disconnect must fire when the device is removed"
    # and the reader thread STOPS — it does not spin forever on the dead fd
    t._reader.join(timeout=1.0)
    assert not t._reader.is_alive(), "reader must stop, not flood, on unplug"


def test_transient_error_keeps_reader_alive(monkeypatch):
    """A non-serial glitch (e.g. a parse hiccup) must NOT kill the reader —
    only a SerialException (device gone) is terminal."""
    class _GlitchyThenIdle:
        def __init__(self, **kw):
            self.reads = 0
        def read(self, n):
            self.reads += 1
            if self.reads == 1:
                return bytes((0x99,))      # stray byte -> resync warning, survives
            time.sleep(0.05)               # then idle timeouts forever
            return b""
        def write(self, f): pass
        def close(self): pass

    monkeypatch.setattr(pyserial, "Serial", _GlitchyThenIdle)
    from athens.roto.transport import SerialTransport
    t = SerialTransport("/dev/fake")
    gone = threading.Event()
    t.on_disconnect = gone.set
    time.sleep(0.3)
    assert not gone.is_set(), "a stray byte must not trigger a disconnect"
    assert t._reader.is_alive(), "reader must survive a transient glitch"
    t.close()
