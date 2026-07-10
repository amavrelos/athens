"""Serial transport for the ROTO-CONTROL config channel.

The device multiplexes two things on one COM port:
  * responses to our commands  -> start with 0xA5
  * unsolicited async events    -> start with 0x5A (framed like a command)

`SerialTransport` runs a background reader thread that demuxes the two on that
first byte, turns command/response into a blocking `request()` call, and hands
async frames to a callback. Only one command is in flight at a time (guarded by
a lock), which matches the device's request/response model.

Responses carry no length field, so the reader needs to be told how many data
bytes each one has. Each request enqueues an *expectation* (generation, length)
before writing; the reader consumes expectations in order. This keeps the
stream correctly sliced even when a response arrives after its request already
timed out, and the generation tag ensures a late response is discarded instead
of being handed to the next caller.

`LoopbackTransport` is a no-hardware stand-in: it logs every frame and returns a
canned success response, so the bridge and codec can be exercised offline.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Callable, Optional, Protocol

from ..protocol import codec
from ..protocol.constants import BAUDRATE, BYTESIZE, CMD_START, PARITY, RESP_START, STOPBITS

log = logging.getLogger(__name__)

AsyncHandler = Callable[[codec.AsyncEvent], None]


class Transport(Protocol):
    """Minimal interface the RotoControl client depends on."""

    on_async: Optional[AsyncHandler]
    on_disconnect: Optional[Callable[[], None]]   # fired once if the link drops

    def request(self, frame: bytes, resp_data_len: int = 0,
                timeout: float = 2.0) -> codec.Response: ...

    def close(self) -> None: ...


class SerialTransport:
    def __init__(self, port: str):
        import serial  # imported lazily so the package imports without pyserial

        self.on_async: Optional[AsyncHandler] = None
        self.on_disconnect: Optional[Callable[[], None]] = None   # link dropped
        self._ser = serial.Serial(
            port=port, baudrate=BAUDRATE, bytesize=BYTESIZE,
            parity=PARITY, stopbits=STOPBITS, timeout=0.2,
        )
        self._cmd_lock = threading.Lock()
        self._resp_q: "queue.Queue[tuple[int, codec.Response]]" = queue.Queue()
        self._expect: "deque[tuple[int, int]]" = deque()   # (generation, data_len)
        self._gen = 0
        self._closing = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="roto-serial-reader")
        self._reader.start()

    # -- public API --------------------------------------------------------
    def request(self, frame: bytes, resp_data_len: int = 0,
                timeout: float = 2.0) -> codec.Response:
        with self._cmd_lock:
            self._gen += 1
            gen = self._gen
            self._expect.append((gen, resp_data_len))
            log.debug("-> %s", frame.hex(" "))
            self._ser.write(frame)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no response to {frame.hex(' ')}")
                try:
                    rgen, resp = self._resp_q.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError(f"no response to {frame.hex(' ')}") from None
                if rgen == gen:
                    return resp
                # a response to an earlier, timed-out command: fully consumed
                # from the stream by its own expectation — just drop it here
                log.warning("dropping stale response (gen %d, waiting for %d)",
                            rgen, gen)

    def close(self) -> None:
        self._closing.set()
        try:
            self._ser.close()
        except Exception:  # pragma: no cover - best effort on teardown
            pass

    # -- reader thread -----------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n and not self._closing.is_set():
            chunk = self._ser.read(n - len(buf))
            if chunk:
                buf += chunk
        return bytes(buf)

    def _read_loop(self) -> None:
        import serial
        while not self._closing.is_set():
            try:
                b0 = self._ser.read(1)
                if not b0:
                    continue
                if b0[0] == CMD_START:
                    self._read_async()
                elif b0[0] == RESP_START:
                    self._read_response()
                else:
                    log.warning("resync: dropping stray byte 0x%02X", b0[0])
            except serial.SerialException as exc:
                # TERMINAL: the ROTO's serial port is gone (USB unplug / power).
                # A removed device fails read() forever, so do NOT keep looping —
                # that spins thousands of times a second, floods the log and
                # wedges the app. Stop the reader and signal a disconnect so the
                # service detaches cleanly (and the MIDI/state pushes stop too).
                if not self._closing.is_set():
                    log.warning("serial link lost (%s) — stopping reader", exc)
                    self._closing.set()
                    cb = self.on_disconnect
                    if cb is not None:
                        try:
                            cb()
                        except Exception:      # noqa: BLE001 - never re-raise here
                            log.exception("on_disconnect handler failed")
                return
            except Exception:  # transient parse/dispatch glitch — keep going
                if not self._closing.is_set():
                    log.exception("serial reader error")

    def _read_async(self) -> None:
        header = self._read_exact(4)          # TYPE SUBTYPE LEN:2
        if len(header) < 4:
            return
        length = (header[2] << 8) | header[3]
        data = self._read_exact(length)
        event = codec.parse_async(bytes((CMD_START,)) + header + data)
        log.debug("<~ async %s", event)
        if self.on_async:
            self.on_async(event)

    def _read_response(self) -> None:
        rc = self._read_exact(1)
        if not rc:
            return
        if self._expect:
            gen, data_len = self._expect.popleft()
        else:
            gen, data_len = -1, 0
            log.warning("response with no matching command; reading RC only")
        data = self._read_exact(data_len) if rc[0] == 0x00 else b""
        resp = codec.Response(code=rc[0], data=data)
        log.debug("<- gen %d %s", gen, resp)
        self._resp_q.put((gen, resp))


class LoopbackTransport:
    """Hardware-free transport for offline demos and tests. Records every frame
    it is asked to send and replies with a success response (optionally with
    canned data for GET-style commands)."""

    def __init__(self, canned: Optional[dict] = None):
        self.on_async: Optional[AsyncHandler] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.sent: list[bytes] = []
        # canned: {(type, subtype): data_bytes} returned as the response payload
        self._canned = canned or {}

    def request(self, frame: bytes, resp_data_len: int = 0,
                timeout: float = 2.0) -> codec.Response:
        self.sent.append(frame)
        key = (frame[1], frame[2]) if len(frame) >= 3 else None
        canned = self._canned.get(key, b"")
        if callable(canned):                 # canned(frame) -> bytes | Response
            canned = canned(frame)
        if isinstance(canned, codec.Response):
            return canned
        return codec.Response(code=0x00, data=canned)

    def close(self) -> None:
        pass

    # convenience for tests / demos: inject an async event
    def emit_async(self, event: codec.AsyncEvent) -> None:
        if self.on_async:
            self.on_async(event)
