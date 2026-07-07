"""RotoSysexClient: the device-facing layer for the native DAW protocol.

Speaks the MIDI+SysEx protocol (docs/DAW-SYSEX-PROTOCOL.md) over the device's
USB-MIDI port. Outbound: push tracks / transport / values as SysEx. Inbound:
parse the device's SysEx (select track, paging, transport request, ...) and CC
values, and auto-answer the PING/keepalive so the connection stays up.

The MIDI link is behind a `MidiPort` so the whole thing runs offline:
  * MidoMidiPort   - real USB-MIDI via mido (needs the `midi` extra)
  * LoopbackMidiPort - records what we send, lets tests inject device messages
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional, Protocol

from ..sysex import codec
from ..sysex.constants import (
    BUTTON_FIRST_CC, ENCODER_FIRST_CC, ENCODER_LSB_CC_OFFSET, NUM_ENCODERS,
    TOUCH_FIRST_CC, TRANSPORT_FIRST_CC, VALUE_MIDI_CHANNEL,
    DawId, General, Group, Mixer, Plugin, TransportAction,
)

log = logging.getLogger(__name__)

MidiHandler = Callable[[bytes], None]


class MidiPort(Protocol):
    on_receive: Optional[MidiHandler]
    def send(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class LoopbackMidiPort:
    """No-hardware port: records outgoing bytes, injects incoming for tests."""

    def __init__(self) -> None:
        self.on_receive: Optional[MidiHandler] = None
        self.sent: List[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def inject(self, data: bytes) -> None:
        if self.on_receive:
            self.on_receive(bytes(data))

    def close(self) -> None:
        pass


class MidoMidiPort:
    """Real USB-MIDI port via mido. Sends/receives complete MIDI byte messages."""

    HINT = "ROTO"

    def __init__(self, in_name: Optional[str] = None, out_name: Optional[str] = None):
        import mido
        self._mido = mido
        self._closed = False
        self._close_lock = threading.Lock()
        self.on_receive: Optional[MidiHandler] = None
        self._in = mido.open_input(in_name or self._pick(mido.get_input_names()),
                                   callback=self._cb)
        self._out = mido.open_output(out_name or self._pick(mido.get_output_names()))

    @classmethod
    def _pick(cls, names: list[str]) -> str:
        # the real device ("ROTO-CONTROL"), never the Cubase bridge pair
        # ("...roto-bridge") — that one carries the DAW contract, not the device
        for n in names:
            if cls.HINT.lower() in n.lower() and "bridge" not in n.lower():
                return n
        if not names:
            raise RuntimeError("no MIDI ports found")
        return names[0]

    def _cb(self, msg) -> None:
        if self.on_receive:
            self.on_receive(bytes(msg.bytes()))

    def send(self, data: bytes) -> None:
        self._out.send(self._mido.parse(list(data)))

    def send_raw(self, data: bytes) -> None:
        """Send one or more back-to-back complete MIDI messages given as raw
        bytes. HUI packs CC pairs and note-ons that a single mido.parse would
        only read the first of, so split and send each."""
        for msg in self._mido.parse_all(bytes(data)):
            self._out.send(msg)

    def close(self) -> None:
        # IDEMPOTENT: Cmd+Q cascades stop() through the WillTerminate observer,
        # the window-closed handler, the finally, AND atexit. A 2nd close() would
        # re-touch the freed rtmidi object and SIGSEGV in RtMidiIn::setCallback,
        # so run the teardown exactly once.
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        # Detach the input callback BEFORE closing. Closing a CoreMIDI input
        # while its callback is still live DEADLOCKS shutdown under Python 3.14:
        # the CoreMIDI in-thread blocks in _PyThreadState_Attach grabbing the
        # GIL to deliver a message while close_port() holds the GIL and waits
        # for that thread. Cancelling the callback first frees the in-thread
        # from needing Python, so close can complete.
        try:
            self._in.callback = None
        except Exception:      # noqa: BLE001 - best-effort
            pass
        for p in (self._in, self._out):
            try:
                p.close()
            except Exception:  # pragma: no cover
                pass


class RotoSysexClient:
    def __init__(self, port: MidiPort, daw_id: int = DawId.LOGIC_PRO):
        self._port = port
        self._port.on_receive = self._on_midi
        # H->D visibility at DEBUG: wrap send so every OUTBOUND frame is logged
        # like _on_midi logs inbound D->H — otherwise the log shows only one
        # direction. Realtime/clock (>=0xF8) excluded as noise.
        _raw_send = self._port.send

        def _logged_send(data: bytes) -> None:
            if data and data[0] < 0xF8 and log.isEnabledFor(logging.DEBUG):
                log.debug("H->D %s", bytes(data).hex(" "))
            _raw_send(data)
        self._port.send = _logged_send
        self.daw_id = daw_id      # identity announced in the PING response

        # device -> DAW event callbacks (all optional)
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_select_track: Optional[Callable[[int], None]] = None
        self.on_set_first_track: Optional[Callable[[int], None]] = None
        self.on_transport_request: Optional[Callable[[], None]] = None
        self.on_page: Optional[Callable[[int], None]] = None          # -1 / +1
        self.on_mixer_mode: Optional[Callable[[int], None]] = None     # raw command id
        self.on_plugin_mode: Optional[Callable[[], None]] = None
        self.on_select_device: Optional[Callable[[int], None]] = None
        self.on_set_first_device: Optional[Callable[[int], None]] = None
        self.on_device_learn: Optional[Callable[[bool], None]] = None
        self.on_control_mapped: Optional[Callable[[codec.ControlMapped], None]] = None
        self.on_value: Optional[Callable[[int, float], None]] = None   # (msb cc, 0..1)
        self.on_touch: Optional[Callable[[int, bool], None]] = None    # (knob, pressed)
        self.on_button: Optional[Callable[[int, bool], None]] = None   # (button, pressed)
        self.on_transport_button: Optional[
            Callable[[TransportAction, bool], None]] = None            # (action, pressed)

        # pending encoder MSBs awaiting their LSB (hardware sends MSB then +32 LSB)
        self._enc_msb: dict[int, int] = {}
        # capacitive-crosstalk ghost rejection (see _touch_gate): first touch
        # wins; a touch beginning while another is held is swallowed whole.
        self._touch_held: set = set()      # knobs with a touch in progress
        self._touch_ghosts: set = set()    # ghost touches being swallowed

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Announce the DAW; the device replies with PING then ROTO_DAW_CONNECTED."""
        self._port.send(codec.daw_started())

    def close(self) -> None:
        self._port.close()

    # -- outbound (DAW -> device) ------------------------------------------
    def send_num_tracks(self, count: int) -> None:
        self._port.send(codec.num_tracks(count))

    def send_first_track(self, index: int) -> None:
        self._port.send(codec.first_track(index))

    def send_track_details(self, index: int, name: str, colour: int,
                           foldable: bool = False) -> None:
        self._port.send(codec.track_details(index, name, colour, foldable))

    def send_track_details_end(self) -> None:
        self._port.send(codec.track_details_end())

    def send_selected_track(self, index: int, name: str, colour: int,
                            foldable: bool = False) -> None:
        self._port.send(codec.daw_select_track(index, name, colour, foldable))

    def send_transport(self, **flags) -> None:
        self._port.send(codec.transport_status(**flags))

    def send_param_value(self, index: int, text: str, is_button: bool = False) -> None:
        self._port.send(codec.param_value(index, text, is_button))

    def send_num_sends(self, count: int) -> None:
        self._port.send(codec.num_sends(count))

    def send_vu_points(self, yellow: int, red: int) -> None:
        self._port.send(codec.vu_meter_points(yellow, red))

    # -- outbound: plugin mode ---------------------------------------------
    def send_num_devices(self, count: int) -> None:
        self._port.send(codec.num_devices(count))

    def send_first_device(self, index: int) -> None:
        self._port.send(codec.first_device(index))

    def send_plugin_details(self, index: int, name: str, hash8: bytes, **kw) -> None:
        self._port.send(codec.plugin_details(index, name, hash8, **kw))

    def send_plugin_details_end(self) -> None:
        self._port.send(codec.plugin_details_end())

    def send_learn_param(self, param_index: int, name: str, value: float,
                         hash6: bytes, **kw) -> None:
        self._port.send(codec.learn_param(param_index, name, value, hash6, **kw))

    def send_mapped_control_name(self, param_index: int, name: str,
                                 hash6: bytes) -> None:
        self._port.send(codec.set_mapped_control_name(param_index, name, hash6))

    def send_daw_select_plugin(self, device_index: int) -> None:
        self._port.send(codec.daw_select_plugin(device_index))

    def send_encoder_value(self, control_index: int, value: float) -> None:
        """Live value to an encoder (drives the motor). Encoders are 14-bit:
        MSB on CC 12+i, LSB on CC+32 — send BOTH bytes, MSB first, as Ableton's
        feedback engine does."""
        from ..sysex.constants import ENCODER_FIRST_CC
        cc = ENCODER_FIRST_CC + control_index
        raw = max(0, min(0x3FFF, int(round(value * 0x3FFF))))
        status = 0xB0 | VALUE_MIDI_CHANNEL
        self._port.send(bytes((status, cc, (raw >> 7) & 0x7F)))
        self._port.send(bytes((status, cc + 32, raw & 0x7F)))

    def send_transport_led(self, action: TransportAction, on: bool) -> None:
        """Transport button LED state — the DAW echoes 127/0 on the button's CC
        (the Ableton script does this for session-record etc.)."""
        self._port.send(bytes((0xB0 | VALUE_MIDI_CHANNEL,
                               TRANSPORT_FIRST_CC + int(action), 127 if on else 0)))

    def send_button_led(self, index: int, on: bool) -> None:
        """Surface button state (127/0 on the button's own CC) — mix mode's
        mute/solo indicators ride these."""
        self._port.send(bytes((0xB0 | VALUE_MIDI_CHANNEL,
                               BUTTON_FIRST_CC + index, 127 if on else 0)))

    def send_meter(self, strip: int, left: float, right: float) -> None:
        """A strip's stereo VU columns: CC METERS_FIRST_CC + 2*strip (L) and
        +1 (R), 7-bit levels (Logic capture: 0x41/0x42 pairs per strip)."""
        from ..sysex.constants import METERS_FIRST_CC
        status = 0xB0 | VALUE_MIDI_CHANNEL
        cc = METERS_FIRST_CC + 2 * strip
        self._port.send(bytes((status, cc,
                               max(0, min(127, int(round(left * 127)))))))
        self._port.send(bytes((status, cc + 1,
                               max(0, min(127, int(round(right * 127)))))))

    # -- inbound (device -> DAW) -------------------------------------------
    def _on_midi(self, data: bytes) -> None:
        if not data:
            return
        try:
            if data[0] == 0xF0:
                self._dispatch_sysex(codec.parse_sysex(data))
            elif 0xB0 <= data[0] <= 0xBF and len(data) >= 3:
                self._dispatch_cc(data)
        except codec.ProtocolError:
            log.debug("ignoring non-ROTO/short frame: %s", data.hex(" "))
        except Exception:
            # this runs on the MIDI callback thread; a malformed frame must
            # not kill inbound processing
            log.exception("error handling MIDI frame %s", data.hex(" "))

    def _dispatch_sysex(self, msg: codec.SysexMessage) -> None:
        g, c, d = msg.group, msg.command, msg.data
        if g == Group.GENERAL:
            if c == General.PING_DAW:
                self._port.send(codec.daw_ping_response(self.daw_id))  # auto keepalive
            elif c == General.ROTO_DAW_CONNECTED:
                self._fire(self.on_connected)
            elif c == General.SELECT_TRACK and len(d) >= 2:
                slot = codec.decode_select_track(d)
                # drop the crosstalk ghost's select: it lands while a DIFFERENT
                # knob's touch is still in progress (the ghost's own touch-on
                # may not be decoded yet, so test for any OTHER held knob).
                if any(h != slot for h in self._touch_held):
                    log.debug("select slot=%d dropped (touch in progress: %s)",
                              slot, sorted(self._touch_held))
                    return
                self._fire(self.on_select_track, slot)
            elif c == General.SET_FIRST_TRACK and len(d) >= 2:
                self._fire(self.on_set_first_track, codec.decode_set_first_track(d))
            elif c == General.REQUEST_TRANSPORT_STATUS:
                self._fire(self.on_transport_request)
        elif g == Group.MIXER:
            self._fire(self.on_mixer_mode, c)
        elif g == Group.PLUGIN:
            if c == Plugin.SET_PLUGIN_MODE:
                self._fire(self.on_plugin_mode)
            elif c == Plugin.ROTO_CONTROL_SELECT_DEVICE and d:
                self._fire(self.on_select_device, codec.decode_select_device(d))
            elif c == Plugin.SET_FIRST_DEVICE and d:
                self._fire(self.on_set_first_device, codec.decode_set_first_device(d))
            elif c == Plugin.SET_DEVICE_LEARN and d:
                self._fire(self.on_device_learn, codec.decode_set_device_learn(d))
            elif c == Plugin.CONTROL_MAPPED and len(d) >= 11:
                self._fire(self.on_control_mapped, codec.decode_control_mapped(d))

    def _dispatch_cc(self, data: bytes) -> None:
        if (data[0] & 0x0F) != VALUE_MIDI_CHANNEL:
            return
        cc, val = data[1], data[2]
        enc_lsb_first = ENCODER_FIRST_CC + ENCODER_LSB_CC_OFFSET
        if ENCODER_FIRST_CC <= cc < ENCODER_FIRST_CC + NUM_ENCODERS:
            # 14-bit MSB (LSB follows on cc+32). If an MSB is already pending,
            # the LSB got lost — flush it at 7-bit first.
            if cc in self._enc_msb:
                self._fire(self.on_value, cc, codec.frac7(self._enc_msb[cc]))
            self._enc_msb[cc] = val
        elif enc_lsb_first <= cc < enc_lsb_first + NUM_ENCODERS:
            msb_cc = cc - ENCODER_LSB_CC_OFFSET
            msb = self._enc_msb.pop(msb_cc, None)
            if msb is not None:
                self._fire(self.on_value, msb_cc, codec.frac14(msb, val))
        elif BUTTON_FIRST_CC <= cc < BUTTON_FIRST_CC + NUM_ENCODERS:
            self._fire(self.on_button, cc - BUTTON_FIRST_CC, val > 0)
        elif TRANSPORT_FIRST_CC <= cc < TRANSPORT_FIRST_CC + len(TransportAction):
            self._fire(self.on_transport_button,
                       TransportAction(cc - TRANSPORT_FIRST_CC), val > 0)
        elif TOUCH_FIRST_CC <= cc < TOUCH_FIRST_CC + NUM_ENCODERS:
            self._touch_gate(cc - TOUCH_FIRST_CC, val > 0)
        else:
            log.debug("unrouted value CC %d = %d", cc, val)

    def _touch_gate(self, knob: int, pressed: bool) -> None:
        """Reject the capacitive-crosstalk ghost. While one knob is held, a
        fuller finger contact can trip the NEIGHBOUR pad; the device sends a
        2nd touch the host must ignore. First touch wins; a touch that begins
        while another is in progress is swallowed whole — its press, and its
        release — so neither the surface highlight nor a select leaks through."""
        if pressed:
            if self._touch_held and knob not in self._touch_held:
                self._touch_ghosts.add(knob)
                return                      # swallow the ghost press
            self._touch_held.add(knob)
        else:
            if knob in self._touch_ghosts:
                self._touch_ghosts.discard(knob)
                return                      # swallow the ghost release too
            self._touch_held.discard(knob)
        self._fire(self.on_touch, knob, pressed)

    @staticmethod
    def _fire(cb, *args) -> None:
        if cb is not None:
            cb(*args)
