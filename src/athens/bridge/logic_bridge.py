"""LogicBridge: wires a SysexDawSource to a RotoLogicClient (daw_id=3).

In the Logic dialect the DEVICE owns the control->param mappings (in flash,
keyed by plugin hash); the DAW never sees a CONTROL_MAPPED. Both sides just
exchange param-indexed values on the plugin CC channels, plus three jobs this
bridge does:

  populate   on plugin entry push track strip + PLUGIN_DETAILS + every param
             value — an unpopulated plugin view is why the device refuses to
             arm learn;
  learn      run the DAW half of the device-driven sweep: user moves a param
             (move-detection) -> PLUGIN_PARAM_SWEEP -> apply each
             PLUGIN_PARAM_SWEEP_VALUE to the DAW and answer with the next
             SWEEP -> LEARN_PARAM when the ramp ends -> restore the value on
             PLUGIN_LEARN_COMPLETE. State machine semantics ported verbatim
             from logic/config.lua ~1760-1920 (see reference/logic-protocol/);
  reflect    stream value CCs + formatted read-outs for the focused plugin's
             params so knobs/screens follow the DAW.

Device->DAW events arrive on the MIDI callback thread; DAW events on feed/OSC
threads. Everything is funnelled through one worker queue so sweep steps,
pushes and feedback never interleave (synchronous=True runs actions inline
for tests).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from ..daw.source import (
    TRACK_FLAGS, PluginParam, SysexDawSource, TrackInfo, TransportState,
)
from ..roto.logic_client import RotoLogicClient
from ..sysex import codec
from ..sysex.constants import (
    MAX_PLUGIN_PARAMETERS, MAX_QUANTISED_STEPS, MAX_QUANTISED_STRING_STEPS,
    METER_LEVEL_RED, METER_LEVEL_YELLOW, NUM_ENCODERS, SMART_MODE_PARAMS,
    STRING_LEN, LearnMode, RackKind,
)
from .common import paged_first_track, push_transport, short_name

log = logging.getLogger(__name__)

# Logic renders real RGB on the strips; REAPER colours aren't in the feed yet,
# so palette-slot bytes (TrackInfo.colour) map to a fixed 12-colour wheel.
_RGB_PALETTE = [
    (26, 163, 49), (235, 80, 60), (240, 160, 40), (235, 210, 60),
    (120, 200, 60), (40, 180, 170), (50, 140, 235), (110, 90, 235),
    (190, 70, 220), (235, 90, 160), (150, 150, 150), (220, 220, 220),
]
_SMART_LABEL_COLOUR = 21          # config.lua COLOR_PLUGIN_LABEL

# Sweep tuning. SETTLE is how long a step waits for REAPER's read-back (the
# feed round-trip is ~50-130ms); SMALL_STEP_LIMIT/ramp semantics mirror
# config.lua exactly (small_step_counter > 4 -> continuous, early exit).
_SWEEP_SETTLE_S = 0.15
_SWEEP_POLL_S = 0.025
_SWEEP_SMALL_STEP_LIMIT = 4
_SWEEP_SMALL_DELTA = 4            # ramp delta (7-bit) counted as a "small" step
_SWEEP_IDLE_ABORT_S = 5.0
_RESTART_BACKSTEP = 0.05          # read-back moved backwards -> user interfering


class SweepState(Enum):
    SYNC_1 = 1              # expect ramp 0x00
    SYNC_2 = 2              # expect ramp 0x7f
    RUNNING_PENDING = 3     # expect ramp 0x00 again -> RUNNING
    RUNNING = 4             # +2 ramp; count steps, pull with PLUGIN_PARAM_SWEEP
    AWAIT_COMPLETE = 5      # LEARN_PARAM sent; waiting for PLUGIN_LEARN_COMPLETE


@dataclass
class _Sweep:
    device: int
    param: int
    name: str
    restore_value: float
    restore_display: str = ""    # the DAW's value string at learn time
    daw_steps: int = 0           # REAPER's own step count (0 = continuous)
    state: SweepState = SweepState.SYNC_1
    step_count: int = 0                  # distinct display strings seen
    small_steps: int = 0                 # consecutive small-delta display changes
    last_display: Optional[str] = None
    last_change_step: int = -1           # ramp position at last display change
    prev_readback: float = 0.0
    strings: int = 0                     # placeholder step-strings to send
    last_activity: float = field(default_factory=time.monotonic)
    curve: list = field(default_factory=list)   # [value, display] at each display step


class LogicBridge:
    def __init__(self, client: RotoLogicClient, source: SysexDawSource,
                 num_sends: int = 2, synchronous: bool = False,
                 settle_s: float = _SWEEP_SETTLE_S, minimal: bool = False):
        self.client = client
        self.source = source
        self._num_sends = num_sends
        self._synchronous = synchronous
        self._settle_s = settle_s
        # bisect aid: disable everything added after the last knob-verified
        # hardware state (meters, overlays, focus strip, strip blanking)
        self._minimal = minimal
        # plugin-identity override: maps an FX name to the hash8 the device
        # should see (the plugin-LINK feature: reuse maps learned in Logic
        # under Logic's plugin name). None/miss -> hash of the name itself.
        self.hash_resolver = None
        # called on learn completion with the DAW's full param identity
        # (the device only stores a 12-char display name)
        self.on_learn_reference = None
        # PARAM-IDENTITY NORMALISATION: the device speaks the param INDEX it was
        # learned with (a REAPER index, say); a different DAW numbers the same
        # param differently. Resolve device-index -> identity hash -> the CURRENT
        # DAW's index by the param's name-hash, so a map learned in one DAW drives
        # the right param in another. resolver(plugin_hash) -> {device_index: hash6}
        # (the service builds it from the persisted learn refs). Additive: when
        # the index already matches (the learn DAW), it is a no-op.
        self.param_identity_resolver = None
        self._param_ids: dict = {}          # device_index -> identity hash6
        self._param_ids_for: Optional[bytes] = None    # plugin the ids are for
        self._resolved: dict = {}           # device_index -> DAW index (cache)
        self._reverse: dict = {}            # DAW index -> device index (feedback)
        # fired when the on-surface bank window (_first_track) moves, so the
        # app can follow the device's page arrows (device -> app)
        self.on_bank_changed = None
        # In mix-ALL mode the device fires SELECT_TRACK on every knob TOUCH
        # (a Logic-ism: touch a strip -> select it). Off by default so brushing
        # a volume knob doesn't yank REAPER's selection; the service flips this
        # from the Settings toggle. Track-select mode's deliberate picks are
        # honoured regardless (they arrive with surface != "mix").
        self.mix_touch_select = False

        self._first_track = 0
        self._surface = ""             # last surface the device announced:
        #   "" (unknown) | "mix" | "mix_focus" | "track_select" | "plugin".
        #   Only "mix" (announced SET_MIXER_ALL_MODE) gates touch-select; a
        #   SELECT_TRACK in any other surface is always honoured.
        self._meter_cache: dict = {}   # strip -> last sent 7-bit (L, R)
        self._plugin_mode: Optional[bool] = None   # None until device says; False=mix
        self._overlay: Optional[str] = None        # "enable" | "select" | None
        self._mix_focus = False                    # mix FOCUS submode (arrows page it)
        self._mix_focus_page = 0                   # 0: sends 1-6, 1: sends 7-12
        self._mix_knob_mode = 0                    # 0 volume, 1 pan, 2 send
        self._mix_button_mode = 0                  # index into TRACK_FLAGS
        self._mix_send = 0                         # active send for knob mode 2
        self._smart = False
        self._learn_mode = int(LearnMode.DISABLED)
        self._sweep: Optional[_Sweep] = None
        self._pushed_sig: Optional[tuple] = None   # last full-value-push context
        self._display_cache: dict[int, str] = {}
        self._echo_last: dict[int, float] = {}     # param -> last echo time
        # per-(device, param) value snap-targets learned from the sweep curve.
        # Applied device->DAW so an *unevenly* stepped param reaches every step
        # (the device's evenly-spaced detents can't). Continuous/tapered params
        # get no entry and pass through untouched.
        self._calibration: dict[tuple[int, int], list[float]] = {}

        self._q: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None

        client.on_connected = lambda: self._submit(self._on_connected)
        client.on_ping = lambda: self._submit(self._on_ping)
        client.on_plugin_mode_logic = \
            lambda smart: self._submit(lambda: self._on_plugin_mode(smart))
        client.on_mixer_all_mode = \
            lambda k, b, s: self._submit(lambda: self._on_mixer_all_mode(k, b, s))
        client.on_mixer_focus_mode = \
            lambda: self._submit(self._on_mixer_focus_mode)
        client.on_mixer_mode = \
            lambda cmd: self._submit(lambda: self._on_mixer_mode(cmd))
        client.on_track_select_mode = \
            lambda: self._submit(self._on_track_select_mode)
        client.on_plugin_enable_mode = \
            lambda: self._submit(self._on_plugin_enable_mode)
        client.on_plugin_select_mode = \
            lambda: self._submit(self._on_plugin_select_mode)
        client.on_select_device = \
            lambda slot: self._submit(lambda: self._on_device_select_device(slot))
        client.on_learn_mode = \
            lambda mode: self._submit(lambda: self._on_learn_mode(mode))
        client.on_sweep_value = \
            lambda sv: self._submit(lambda: self._on_sweep_value(sv))
        client.on_learn_complete = \
            lambda: self._submit(self._on_learn_complete)
        client.on_switch_value_request = \
            lambda p: self._submit(lambda: self._send_display(p))
        client.on_param_value_cc = \
            lambda p, v: self._submit(lambda: self._on_device_param(p, v))
        client.on_param_touch = \
            lambda p, pressed: self._submit(lambda: self._on_param_touch(p, pressed))
        client.on_select_track = \
            lambda i: self._submit(lambda: self._on_select_track(i))
        client.on_transport_request = \
            lambda: self._submit(self._on_transport_request)
        client.on_transport_button = \
            lambda a, pr: self._submit(lambda: self.source.transport_action(a, pr))
        client.on_custom_action = \
            lambda aid, pr: self._submit(
                lambda: self.source.run_action(aid) if pr else None)
        client.on_value = \
            lambda cc, v: self._submit(lambda: self._on_mix_knob(cc, v))
        client.on_button = \
            lambda i, pr: self._submit(lambda: self._on_mix_button(i, pr))
        client.on_page = lambda d: self._submit(lambda: self._on_page(d))

        self.bind_source(source)

    def bind_source(self, source: SysexDawSource) -> None:
        """(Re)wire the DAW->device push path onto `source`. These are DIRECT
        assignments — the bridge owns the source's callback slots; the service
        chains its UI taps on top afterwards. Called from __init__ AND on every
        runtime DAW hot-swap: a swapped-in source that is not re-bound leaves
        the device deaf to the new DAW while the UI keeps working."""
        self.source = source
        source.on_param_touched = \
            lambda fx, p: self._submit(lambda: self._on_daw_param_moved(fx, p))
        source.on_device_param_value = \
            lambda fx, p, v, d: self._submit(lambda: self._on_daw_param_value(fx, p, v, d))
        source.on_plugin_focus_changed = \
            lambda: self._submit(self._on_focus_changed)
        source.on_devices_changed = \
            lambda: self._submit(self._on_devices_changed)
        source.on_tracks_changed = \
            lambda: self._submit(self._push_mixer)
        source.on_selected_track_changed = \
            lambda: self._submit(self._on_selection_changed)
        source.on_transport_changed = \
            lambda: self._submit(self._push_transport)
        source.on_track_volume = \
            lambda i, v: self._submit(lambda: self._on_daw_knob_value(i, v, 0))
        source.on_track_pan = \
            lambda i, v: self._submit(lambda: self._on_daw_knob_value(i, v, 1))
        source.on_track_send = \
            lambda i, s, v: self._submit(lambda: self._on_daw_send(i, s, v))
        source.on_track_flag = \
            lambda i, f, on: self._submit(lambda: self._on_daw_flag(i, f, on))
        source.on_track_vu = \
            lambda i, l, r: self._submit(lambda: self._on_daw_vu(i, l, r))
        source.on_daw_alive = \
            lambda alive: self._submit(lambda: self._on_daw_alive(alive))
        # a swapped-in source numbers the same params its own way: force the
        # cross-DAW index caches to re-resolve against THIS source (the plugin
        # hash is often unchanged across a swap, so _refresh_param_ids' early
        # return would otherwise keep the OLD DAW's routing). And re-apply the
        # device's current learn state — the new source starts disarmed and
        # would never fire on_param_touched until learn was toggled off/on.
        self._param_ids_for = None
        self._resolved.clear()
        self._reverse.clear()
        source.set_learn_armed(self._learn_mode == LearnMode.ENABLED)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if not self._synchronous:
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, daemon=True,
                                            name="logic-bridge")
            self._worker.start()
        self.source.start()
        self.client.start()      # DAW_STARTED; device PINGs -> daw_id=3 reply

    def blank_surface(self) -> None:
        """Clear the device to a clean 'no DAW' state without tearing the bridge
        down — used when REAPER goes away (feed heartbeat stops) and as the first
        half of stop(). EXACT match to Logic's own shutdown: zero the 8 stereo VU
        meters, then NUM_SENDS 0, NUM_TRACKS 0, NUM_DEVICES 0, and clear the
        plugin slot — nothing else (empty 0x7 TRACK_DETAILS would read as '8
        nameless tracks exist' rather than clearing)."""
        log.info("blanking device surface (Logic-style shutdown zeroing)")
        try:
            self._zero_meters()
            self.client.send_num_sends(0)
            self.client.send_num_tracks(0)
            self.client.send_num_devices(0)
            self._clear_plugin_slot()
            self._pushed_sig = None      # force a fresh flood when data returns
        except Exception:
            log.debug("surface blank failed (port already gone)")

    def _clear_plugin_slot(self) -> None:
        """The 'no plugin' placeholder Logic sends at connect AND shutdown
        (logic_good_full.txt: PLUGIN_DETAILS slot0 hash=00..00 name='' en=1).
        This is what actually clears a ghost PLUGIN screen — the mix
        counts=0 zeroing doesn't touch the plugin display."""
        self._empty_plugin_slot(0)

    def _empty_plugin_slot(self, slot: int) -> None:
        """The 'no plugin' marker frame for one slot: ALL-ZERO hash, empty
        name, en=1 (the device treats zeros as no-plugin — NOT hash of '')."""
        self.client.send_plugin_details(slot, "", bytes(8), enabled=True,
                                        rack_kind=RackKind.NORMAL)

    def stop(self) -> None:
        self.source.stop()
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        # leave the device in a clean state instead of a half-open session
        self.blank_surface()
        self.client.close()

    # -- worker ---------------------------------------------------------------
    def _submit(self, fn) -> None:
        if self._synchronous:
            fn()
        else:
            self._q.put(fn)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fn = self._q.get(timeout=0.25)
            except queue.Empty:
                self._check_sweep_stalled()
                continue
            try:
                fn()
            except Exception:
                log.exception("logic-bridge action failed")

    def _check_sweep_stalled(self) -> None:
        s = self._sweep
        if s and time.monotonic() - s.last_activity > _SWEEP_IDLE_ABORT_S:
            log.warning("sweep of param %d stalled; restoring %.3f",
                        s.param, s.restore_value)
            self.source.set_device_param(s.device, s.param, s.restore_value)
            self._end_sweep()

    def _sleep(self, seconds: float) -> None:
        if not self._synchronous and seconds > 0:
            time.sleep(seconds)

    # -- device lifecycle ------------------------------------------------------
    def _on_connected(self) -> None:
        """Connect response is pure STATE — no command-channel traffic (the
        0xB6 channel is Logic-internal; see logic_client.send_command)."""
        log.info("device connected (Logic dialect); reset then push state")
        # RESET-THEN-CONFIGURE, mirroring Logic's connect prelude: zero the
        # controls + clear the plugin slot (empty PLUGIN_DETAILS) so any ghost
        # session from a prior connect is wiped BEFORE we build up the current
        # state. Logic does exactly this at connect (logic_good_full.txt).
        self._zero_controls()
        self._clear_plugin_slot()
        self._push_init_burst()       # counts + VU, Logic's order
        self.source.refresh_state()   # tracks arrive async -> on_tracks_changed
        # _push_mixer handles BOTH cases: real tracks, or (empty project) a
        # RESET_TRACK_DETAILS for all 8 strips — which is what actually
        # repaints away a retained ghost. counts=0 alone doesn't (the device
        # keeps its last strip render until the slots are reset). Logic's
        # connect to an empty project hammers exactly these resets.
        self._push_mixer()
        self._push_transport()

    def _on_ping(self) -> None:
        """Device discovery ping. Logic answers each one by (re)pushing the
        init burst BEFORE the connect ack (logic_startup.mmon) — that early,
        repeated push is what makes the device initialise its display rather
        than keep a ghost. Cheap; the device stops pinging once connected."""
        self._push_init_burst()

    def _push_init_burst(self) -> None:
        """Logic's session-shape burst (logic_startup.mmon lines 3-8):
        NUM_SENDS, NUM_TRACKS, FIRST_TRACK, NUM_DEVICES, FIRST_DEVICE, VU
        points, in that exact order."""
        self.client.send_num_sends(self._num_sends)
        self.client.send_num_tracks(len(self.source.tracks()))
        self.client.send_first_track(self._first_track)
        self.client.send_num_devices(len(self.source.devices()))
        self.client.send_first_device(0)
        self.client.send_vu_points(METER_LEVEL_YELLOW, METER_LEVEL_RED)

    def _zero_meters(self) -> None:
        """Zero all 8 VU meters and reset the delta cache to match — the
        screen-entry / teardown prelude shared by every mode."""
        for i in range(NUM_ENCODERS):
            self.client.send_meter(i, 0.0, 0.0)
        self._meter_cache = {i: (0, 0) for i in range(NUM_ENCODERS)}

    def _zero_controls(self) -> None:
        """Logic's reset prelude at connect/mode entry: button LEDs, meters
        and transport LEDs zeroed (logic_good.mmon)."""
        for i in range(NUM_ENCODERS):
            self.client.send_button_led(i, False)
        self._zero_meters()

    def _on_mixer_all_mode(self, knob_mode: int, button_mode: int,
                           send_index: int) -> None:
        log.info("device mix mode (knobs=%d buttons=%d send=%d); acknowledging",
                 knob_mode, button_mode, send_index)
        self._plugin_mode = False
        self._surface = "mix"
        self._overlay = None
        self._mix_focus = False
        self._mix_focus_page = 0
        self._mix_knob_mode = knob_mode
        self._mix_button_mode = button_mode if button_mode < len(TRACK_FLAGS) else 0
        self._mix_send = send_index
        self._cancel_sweep()
        self._push_mixer()

    def _on_mixer_focus_mode(self) -> None:
        log.info("device mix FOCUS mode; acknowledging")
        self._plugin_mode = False
        self._surface = "mix_focus"
        self._overlay = None
        self._mix_focus = True
        self._mix_focus_page = 0
        self._cancel_sweep()
        if not self._minimal:
            self._push_focus_strip()

    def _on_mixer_mode(self, _cmd: int) -> None:
        self._plugin_mode = False
        self._overlay = None
        self._cancel_sweep()
        self._push_mixer()

    def _on_plugin_mode(self, smart: bool) -> None:
        """Device announced its plugin screen (it re-sends this often). Push the
        strip + PLUGIN_DETAILS *and the full value flood* EVERY time, exactly as
        Logic does. Flood-once-on-entering was a traffic optimisation that broke
        anchoring on hardware: the device announces the screen ~100ms before it
        is ready to take value CCs, so the single flood landed in the void and
        the later announces (where it would stick) got details with NO values —
        knobs kept their previous screen's positions. Logic re-floods on every
        announce; so do we."""
        entering = self._plugin_mode is not True or self._smart != smart
        self._plugin_mode, self._smart = True, smart
        self._surface = "plugin"
        self._overlay = None
        if entering:
            # capture-faithful entry prelude: LEDs + meters zeroed, then
            # colour -> PLUGIN_DETAILS -> full value flood
            self._zero_controls()
        self._push_plugin_context(force_values=True)

    # -- device-side overlays --------------------------------------------------
    def _on_track_select_mode(self) -> None:
        """Track-select overlay (overlay.mmon): meters zeroed, then the
        track names as 0x11 details in the neutral label colour — no 0x7
        flow, no counts, no END. The pick comes back as GEN/SELECT_TRACK."""
        self._surface = "track_select"     # so the pick is honoured even if
        #                                    the touch-select toggle is off
        if self._minimal:
            log.info("device track-select overlay (minimal: ignored)")
            return
        log.info("device track-select overlay; pushing track names")
        self._overlay = None
        tracks = self.source.tracks()
        window = tracks[self._first_track:self._first_track + NUM_ENCODERS]
        self._zero_meters()
        for i in range(NUM_ENCODERS):
            if i < len(window):
                self.client.send_logic_track_details(
                    i, window[i].name, self._FOCUS_LABEL_COLOUR)
            else:
                self.client.send_reset_track_details(i)
            self._sleep(0.005)

    def _on_plugin_enable_mode(self) -> None:
        """Plugin enable/bypass overlay: 8 FX slots on the buttons."""
        if self._minimal:
            log.info("device plugin-enable overlay (minimal: ignored)")
            return
        log.info("device plugin-enable overlay; pushing slots")
        self._overlay = "enable"
        self._push_plugin_slots(leds=True)

    def _on_plugin_select_mode(self) -> None:
        """Plugin-select overlay: pick a slot -> ROTO_CONTROL_SELECT_DEVICE."""
        if self._minimal:
            log.info("device plugin-select overlay (minimal: ignored)")
            return
        log.info("device plugin-select overlay; pushing slots")
        self._overlay = "select"
        self._push_plugin_slots(leds=False)

    # the select overlay is two pages of 8 slots (overlay.mmon: Logic
    # pushes PLUGIN_DETAILS for slots 0-15)
    _OVERLAY_SLOTS = 16

    def _push_plugin_slots(self, leds: bool) -> None:
        """The FX chain as overlay slots — one PLUGIN_DETAILS per slot.
        Empty slots per the capture: ALL-ZERO hash, en=1, empty name (NOT
        the hash of '' — the device treats zeros as the no-plugin marker)."""
        devices = {d.index: d for d in self.source.devices()}
        for slot in range(self._OVERLAY_SLOTS):
            d = devices.get(slot)
            # empty-NAMED slots (Cubase gap devices, e.g. a bare instrument
            # slot) must take the all-zero marker too — hash("") is a real
            # non-zero hash the device would render as a garbled box
            if d is not None and d.name:
                self.client.send_plugin_details(
                    slot, d.name, self._device_hash(d.name),
                    enabled=d.enabled, rack_kind=RackKind.NORMAL)
            else:
                self._empty_plugin_slot(slot)
            self._sleep(0.005)
            if leds and slot < NUM_ENCODERS:
                self.client.send_button_led(slot, bool(d and d.name and d.enabled))

    def _on_device_select_device(self, slot: int) -> None:
        """The user picked an FX slot in the select overlay -> focus it in
        REAPER (the feed reports the focus change and the plugin context
        follows); mirror config.lua's PLUGIN_SLOT command back."""
        log.info("device selected plugin slot %d", slot)
        if slot >= len(self.source.devices()):
            return
        self.source.focus_device(slot)

    # -- pushes -----------------------------------------------------------------
    def _rgb(self, colour: int) -> tuple:
        return _RGB_PALETTE[colour % len(_RGB_PALETTE)]

    def _device_hash(self, name: str) -> bytes:
        if self.hash_resolver is not None:
            linked = self.hash_resolver(name)
            if linked:
                return linked
        return codec.device_hash(name)

    def refresh_plugin_context(self) -> None:
        """Re-announce + re-populate the focused plugin (used after a link
        change so the device re-attaches under the new identity)."""
        self._submit(self._on_focus_changed)

    def _knob_value(self, t: TrackInfo) -> float:
        """The value the strip's knob shows in the current mix knob bank."""
        if self._mix_knob_mode == 1:
            return t.pan
        if self._mix_knob_mode == 2:
            return self.source.track_send(t.index, self._mix_send)
        return t.volume

    def _led_state(self, t: TrackInfo) -> bool:
        """Button LED for the current bank: mute lights when AUDIBLE (per the
        Logic capture); solo/arm/monitor light when active."""
        flag = TRACK_FLAGS[self._mix_button_mode]
        on = getattr(t, flag)
        return not on if flag == "muted" else on

    def _push_mixer(self) -> None:
        """The 8-strip window. Names go as the ABLETON-style track flow
        (NUM_TRACKS + FIRST_TRACK + TRACK_DETAILS 0x7 + END) — the device renders
        strip names from that flow in the Logic dialect too, while byte-exact
        Logic-style SET_TRACK_DETAILS (0x11) frames are accepted but never
        rendered. RGB colours (0x13), volumes and mute LEDs as in the Logic
        capture. SysEx frames are paced ~5ms apart, as config.lua does."""
        if self._plugin_mode is True:
            # NEVER touch the surface while the PLUGIN screen is up: the mix
            # push sends encoder-position CCs (volumes) that CLOBBER the mapped
            # knobs' anchors — a queued tracks_changed draining after a plugin
            # entry left the knobs sitting at their mix positions. Logic sends
            # no mix flow during the plugin screen; the mode-change handlers
            # re-push the mixer when the mix screen returns.
            return
        tracks = self.source.tracks()
        log.info("mixer push: %d tracks known, window from %d",
                 len(tracks), self._first_track)
        window = [(i, tracks[self._first_track + i])
                  for i in range(NUM_ENCODERS)
                  if self._first_track + i < len(tracks)]
        if self._minimal:
            # bisect: Logic-pure strip push (0x11 — fw doesn't render these
            # names, but it also can't corrupt the dialect session state)
            for i, t in window:
                self.client.send_track_color(i, self._rgb(t.colour))
                self._sleep(0.005)
                self.client.send_logic_track_details(i, t.name, t.colour)
                self._sleep(0.005)
                self.client.send_encoder_value(i, self._knob_value(t))
                self.client.send_button_led(i, self._led_state(t))
            return
        self.client.send_num_tracks(len(tracks))
        self.client.send_first_track(self._first_track)
        for i, t in window:
            self.client.send_track_color(i, self._rgb(t.colour))
            self._sleep(0.005)
            self.client.send_encoder_value(i, self._knob_value(t))
        for _i, t in window:
            self.client.send_track_details(t.index, t.name, t.colour)
            self._sleep(0.005)
        self.client.send_track_details_end()
        for i, t in window:
            self.client.send_button_led(i, self._led_state(t))
        if self._minimal:
            return
        # blank strips past the project's end (leftover content stays lit
        # otherwise; RESET_TRACK_DETAILS is Logic's screen-clear)
        for i in range(len(window), NUM_ENCODERS):
            self.client.send_reset_track_details(i)
            self._sleep(0.005)
            self.client.send_encoder_value(i, 0.0)
            self.client.send_button_led(i, False)

    def _on_selection_changed(self) -> None:
        # instrumentation: gap from the OSC "selected track (received)" log to
        # here = how long the action waited in the bridge worker queue
        log.debug("bridge: pushing selection to device (worker dequeued)")
        # in FOCUS the whole strip is the selected track — rebuild it
        if self._mix_focus:
            self._push_focus_strip()
            self._push_focus_track()
        elif self._surface == "mix":
            # mix-ALL: Logic keeps the mix screen and streams the selected
            # strip's readout, never a focus-track (logic-touch.mmon)
            self._push_mix_touch_readout()
        else:
            self._push_focus_track()

    def _push_focus_track(self) -> None:
        # selection trio per logic-mix capture (FOCUS_TRACK + CURRENT_TRACK_
        # NAME 0x16 + CURRENT_TRACK_COLOR 0x17, lines 353-355)
        tracks = self.source.tracks()
        si = self.source.selected_track()
        if 0 <= si < len(tracks):
            t = tracks[si]
            self.client.send_focus_track(si % NUM_ENCODERS, t.name,
                                         self._rgb(t.colour))
            self.client.send_current_track_name(t.name)
            self.client.send_current_track_color(self._rgb(t.colour))

    def _push_mix_touch_readout(self) -> None:
        """Logic-faithful reflection of the selected track on the mix-ALL
        screen (logic-touch.mmon): stream the strip's value readout
        (PARAM_VALUES) + motor echo and keep the device on the mix screen —
        NO focus-track (Logic never sends one here; it flips the display
        context). No-op if the selected track isn't in the on-surface window."""
        si = self.source.selected_track()
        slot = si - self._first_track
        tracks = self.source.tracks()
        if not (0 <= slot < NUM_ENCODERS) or not (0 <= si < len(tracks)):
            return
        t = tracks[si]
        self.client.send_encoder_value(slot, self._knob_value(t))
        self.client.send_param_display(slot, self._mix_readout(t))

    # The focused-track channel strip (choreography: logic-focus.mmon).
    # Knob layout per config.lua's assignment table (mode='Focus'):
    # knob 1 volume, knob 2 pan, knobs 3-8 sends; Focus 2 = next sends page.
    _FOCUS_LABELS = ("Volume", "Pan")
    _FOCUS_LABEL_COLOUR = 0x16   # neutral label colour Logic uses here

    def _focus_knob_label(self, knob: int) -> str:
        if self._mix_focus_page == 0 and knob < len(self._FOCUS_LABELS):
            return self._FOCUS_LABELS[knob]
        return f"Send {self._focus_send_index(knob) + 1}"

    def _focus_send_index(self, knob: int) -> int:
        base = len(self._FOCUS_LABELS) if self._mix_focus_page == 0 else 0
        page = 0 if self._mix_focus_page == 0 \
            else NUM_ENCODERS - len(self._FOCUS_LABELS)
        return page + knob - base

    def _focus_used_slots(self) -> range:
        """Slots that carry content on the current focus page; the rest are
        blanked with RESET_TRACK_DETAILS like Logic does."""
        if self._mix_focus_page == 0:
            return range(min(2 + self._num_sends, NUM_ENCODERS))
        overflow = max(0, self._num_sends - (NUM_ENCODERS - 2))
        return range(min(overflow, NUM_ENCODERS))

    def _focus_readout(self, t: TrackInfo, knob: int) -> str:
        """Value string for the focus strip (Logic streams these as
        PARAM_VALUES 0x18). REAPER's OSC floats are normalized, so show
        percent / pan position rather than fake a dB law."""
        v = self._focus_knob_value(t, knob)
        if self._mix_focus_page == 0 and knob == 1:
            c = round((v - 0.5) * 200)
            return "C" if c == 0 else (f"L {-c}" if c < 0 else f"R {c}")
        return f"{round(v * 100)} %"

    def _mix_readout(self, t: TrackInfo) -> str:
        """Readout string for a mix-ALL strip in the current knob bank —
        same formatting as the focus strip (percent / pan position; REAPER's
        OSC values are normalized, so no faked dB law)."""
        v = self._knob_value(t)
        if self._mix_knob_mode == 1:                 # pan
            c = round((v - 0.5) * 200)
            return "C" if c == 0 else (f"L {-c}" if c < 0 else f"R {c}")
        return f"{round(v * 100)} %"

    def _push_focus_strip(self) -> None:
        """Capture-exact focus entry (logic-focus.mmon): meters zeroed,
        encoder values, per-slot track colour (0x13), 0x11 labels for used
        slots + 0x12 resets for the rest, focused track last, then value
        readouts. The FOCUS screen renders the 0x11 flow — no 0x7 details
        and no END terminator here (those belong to the mix-ALL screen)."""
        tracks = self.source.tracks()
        si = self.source.selected_track()
        if not 0 <= si < len(tracks):
            return
        t = tracks[si]
        self._zero_meters()
        for knob in range(NUM_ENCODERS):
            self.client.send_encoder_value(knob, self._focus_knob_value(t, knob))
        for knob in range(NUM_ENCODERS):
            self.client.send_track_color(knob, self._rgb(t.colour))
            self._sleep(0.005)
        used = self._focus_used_slots()
        for knob in range(NUM_ENCODERS):
            if knob in used:
                self.client.send_logic_track_details(
                    knob, self._focus_knob_label(knob),
                    self._FOCUS_LABEL_COLOUR)
            else:
                self.client.send_reset_track_details(knob)
            self._sleep(0.005)
        self.client.send_focus_track(si % NUM_ENCODERS, t.name,
                                     self._rgb(t.colour))
        for knob in used:
            self.client.send_param_display(knob, self._focus_readout(t, knob))

    def _focus_knob_value(self, t: TrackInfo, knob: int) -> float:
        if self._mix_focus_page == 0 and knob == 0:
            return t.volume
        if self._mix_focus_page == 0 and knob == 1:
            return t.pan
        return self.source.track_send(t.index, self._focus_send_index(knob))

    def _on_daw_alive(self, alive: bool) -> None:
        if alive:
            log.info("DAW returned; re-flooding the surface")
            self.source.refresh_state()      # tracks/devices arrive async
            if self._plugin_mode:
                self._push_plugin_context(force_values=True)
            else:
                self._push_mixer()
        else:
            log.info("DAW went away; blanking the device")
            self._cancel_sweep()
            self.blank_surface()

    def _on_transport_request(self) -> None:
        log.info("device transport mode; pushing status")
        self._push_transport()

    def _push_transport(self) -> None:
        push_transport(self.client, self.source)

    def _push_plugin_context(self, force_values: bool = False) -> None:
        """force_values: the device does NOT retain param values across screen
        changes — every (re)entry into plugin mode needs the full value flood or
        mapped knobs have no position anchor (no end-stops, free-spin). Logic
        re-floods all values on every plugin entry."""
        devices = self.source.devices()
        dev = self.source.selected_device()
        d = next((x for x in devices if x.index == dev), None)
        if d is None or not d.name:
            return          # no plugin, or the name frame hasn't landed yet:
        #                     never flash an empty-name (hash-of-"") push — the
        #                     device shows a blank map for that frame otherwise
        #                     (seen in the log: DEVICE_COUNT arrives 1 frame
        #                     ahead of DEVICE_NAME on a fresh plugin load).
        self._push_focus_track()
        tracks = self.source.tracks()
        si = self.source.selected_track()
        if 0 <= si < len(tracks):
            self.client.send_current_track_color(self._rgb(tracks[si].colour))
        # NO PLUGIN_SLOT command here: config.lua sends it only on explicit
        # plugin SELECTION (slot 0 = instrument semantics); sending it on every
        # entry detaches the device's stored mapping from the active plugin ->
        # knobs free-spin. A single PLUGIN_DETAILS with rack_kind 0, no
        # NUM_DEVICES/_END — byte-faithful to what Logic sends.
        self.client.send_plugin_details(
            d.index, d.name, self._device_hash(d.name), enabled=d.enabled,
            rack_kind=RackKind.NORMAL)

        self._refresh_param_ids(self._device_hash(d.name))
        params = self.source.device_params(dev)
        sig = (dev, d.name, self._smart, len(params))
        if force_values or sig != self._pushed_sig:
            self._pushed_sig = sig
            self._display_cache.clear()
            if self._smart:
                self._push_smart_params(params)
            else:
                self._push_all_param_values(params)
            self._push_mapped_param_values(dev)   # cross-DAW re-index for learns
            self._watch_params(dev, params)

    def _push_all_param_values(self, params: List[PluginParam]) -> None:
        """Populate the plugin view: every param's value CC pair. This is the
        push that makes the device willing to arm learn."""
        vals = [p for p in params if p.index < MAX_PLUGIN_PARAMETERS]
        if len(vals) > 8 and all(p.value == 0.0 for p in vals):
            # a >8-param plugin with EVERY value at 0.0 is not a real state —
            # it's placeholders (values never received: fresh relaunch) or the
            # degenerate all-zero DA read. Flooding it would anchor every
            # mapped knob to 0; keep the current anchors instead — real values
            # trickle through on_device_param_value the moment they arrive.
            log.warning("plugin value table is all-zero (%d params) — skipping "
                        "the anchor flood; knobs keep their anchors until real "
                        "values arrive", len(vals))
            return
        n = 0
        for p in params:
            if p.index >= MAX_PLUGIN_PARAMETERS:
                break
            self.client.send_param_value_cc(p.index, p.value)
            n += 1
            if n % 32 == 0:
                self._sleep(0.005)       # don't slam the device's input buffer
        log.info("plugin populated: %d param values pushed", n)

    def _push_smart_params(self, params: List[PluginParam]) -> None:
        for p in params[:SMART_MODE_PARAMS]:
            self.client.send_smart_ctl_details(p.index, short_name(p.name),
                                               _SMART_LABEL_COLOUR)
            self.client.send_param_display(p.index, p.display or f"{p.value:.2f}")
            self.client.send_param_value_cc(p.index, p.value)

    def _watch_params(self, dev: int, params: List[PluginParam]) -> None:
        self.source.set_watched_params(
            [(dev, p.index) for p in params if p.index < MAX_PLUGIN_PARAMETERS])

    # -- param-identity normalisation (cross-DAW) ------------------------------
    def _refresh_param_ids(self, plugin_hash: bytes) -> None:
        """Load {device_index -> identity hash} for the active plugin from the
        persisted learn refs (via the service). Cached until the plugin changes."""
        if plugin_hash == self._param_ids_for:
            return
        # resolve FIRST, stamp after: stamping before a resolver exception would
        # leave the previous plugin's map attributed to the new hash — and the
        # same-hash early-return above would make that permanent.
        r = self.param_identity_resolver
        ids = (r(plugin_hash) or {}) if r is not None else {}
        self._param_ids_for = plugin_hash
        self._param_ids = ids
        self._resolved.clear()
        self._reverse.clear()

    def _daw_param_for_device_index(self, dev: int, device_index: int) -> int:
        """Translate the index the DEVICE sends to the CURRENT DAW's index for
        the SAME param, by identity hash. No-op when the param already sits at
        that index (the learn DAW / an aligned one); falls back to the raw index
        when there's no identity or the param isn't exposed in this DAW."""
        if device_index in self._resolved:
            return self._resolved[device_index]
        want = self._param_ids.get(device_index)
        if want is None:                    # no learned identity: raw is final
            self._resolved[device_index] = device_index
            return device_index
        here = self._param(dev, device_index)
        if here is not None and here.name \
                and codec.param_hash(here.name) == want:
            self._resolved[device_index] = device_index
            return device_index
        target = next((p.index for p in self.source.device_params(dev)
                       if p.name and codec.param_hash(p.name) == want), None)
        if target is None:
            # the DAW's param names may still be streaming in (OSC drip /
            # PARAM_NAME frames): do NOT pin the fallback — return it uncached
            # so the next event retries and snaps to the right param once its
            # name lands.
            return device_index
        self._resolved[device_index] = target
        return target

    def _device_index_for_daw_param(self, dev: int, daw_index: int) -> int:
        """The inverse of _daw_param_for_device_index: which DEVICE knob shows
        this DAW param. DAW-side feedback (value/display) must be echoed at the
        knob's LEARNED index — a re-indexed knob never tracks DAW-side moves
        otherwise (and the knob that happens to sit at the DAW's index would be
        driven with the wrong param's value)."""
        if not self._param_ids:
            return daw_index
        if daw_index in self._reverse:
            return self._reverse[daw_index]
        p = self._param(dev, daw_index)
        if p is None or not p.name:
            return daw_index                # name not in yet — retry, uncached
        h = codec.param_hash(p.name)
        device_index = next((di for di, want in self._param_ids.items()
                             if want == h), daw_index)
        self._reverse[daw_index] = device_index
        return device_index

    def _push_mapped_param_values(self, dev: int) -> None:
        """Cross-DAW display: for each LEARNED knob whose param this DAW exposes
        at a DIFFERENT index, push that value/display at the DEVICE's index so the
        knob shows the right position + endstops. No-op when aligned or absent."""
        if not self._param_ids:
            return
        by_hash: dict = {}
        for p in self.source.device_params(dev):
            by_hash.setdefault(codec.param_hash(p.name), p)
        for device_index, want in self._param_ids.items():
            p = by_hash.get(want)
            if (p is not None and p.index != device_index
                    and device_index < MAX_PLUGIN_PARAMETERS):
                self.client.send_param_value_cc(device_index, p.value)
                if p.display:
                    self.client.send_param_display(device_index, p.display)

    # -- learn / sweep -----------------------------------------------------------
    def _on_learn_mode(self, mode: int) -> None:
        log.info("device learn mode -> %d", mode)
        self._learn_mode = mode
        self.source.set_learn_armed(mode == LearnMode.ENABLED)
        if mode != LearnMode.ENABLED:
            self._cancel_sweep()

    def _on_daw_param_moved(self, fx: int, param: int) -> None:
        """Move-detection fired: the user grabbed this param while learn is
        armed -> trigger the device's sweep of it (config.lua ~1780)."""
        if self._learn_mode != LearnMode.ENABLED or self._sweep is not None:
            return
        if fx != self.source.selected_device():
            return
        if param >= MAX_PLUGIN_PARAMETERS:
            log.warning("param %d beyond the Logic dialect's 256-param bus; "
                        "not learnable", param)
            return
        p = self._param(fx, param)
        if p is None:
            return
        self._sweep = _Sweep(device=fx, param=param, name=p.name,
                             restore_value=p.value,
                             restore_display=p.display or "",
                             daw_steps=p.steps)
        self.source.set_watched_params([(fx, param)])
        log.info("learn: sweeping param %d (%s), restore=%.3f",
                 param, p.name, p.value)
        self.client.send_plugin_param_sweep(param)

    def _on_sweep_value(self, sv: codec.SweepValue) -> None:
        s = self._sweep
        if s is None or sv.param_index != s.param:
            log.debug("stray sweep value for param %d", sv.param_index)
            return
        s.last_activity = time.monotonic()
        # config.lua PLUGIN_PARAM_SWEEP_VALUE handler: apply the requested
        # value, and RUNNING_PENDING becomes RUNNING on the next value in
        if s.state == SweepState.RUNNING_PENDING:
            s.state = SweepState.RUNNING
        p = self._param(s.device, s.param)
        before_display = p.display if p else ""
        self.source.set_device_param(s.device, s.param, sv.value)
        readback, display = self._settle(s, sv.value, before_display)
        self._sweep_step(s, sv, readback, display)

    def _settle(self, s: _Sweep, target: float,
                before_display: str) -> Tuple[float, str]:
        """Wait for REAPER's read-back of the value we just set. The source
        stores our write optimistically, so the VALUE matches immediately —
        the fresh signal is the DISPLAY string coming back through the feed
        (~50-130ms). Step counting needs it; wait up to settle_s per step and
        proceed with whatever is newest on timeout (a display can legitimately
        not change between fine ramp steps)."""
        deadline = time.monotonic() + self._settle_s
        while True:
            p = self._param(s.device, s.param)
            if p is not None and p.display != before_display:
                return p.value, p.display
            if self._synchronous or time.monotonic() >= deadline:
                return (p.value, p.display) if p else (target, "")
            self._sleep(_SWEEP_POLL_S)

    def _sweep_step(self, s: _Sweep, sv: codec.SweepValue,
                    readback: float, display: str) -> None:
        """One step of config.lua's sweep machine (CSFeedback ~1815 +
        CSFeedbackText ~1971): numeric check first, then display counting."""
        if s.state == SweepState.SYNC_1:
            if sv.step == 0x00:
                s.state = SweepState.SYNC_2
            return
        if s.state == SweepState.SYNC_2:
            s.state = (SweepState.RUNNING_PENDING if sv.step == 0x7F
                       else SweepState.SYNC_1)
            s.prev_readback = 0.0
            return
        if s.state != SweepState.RUNNING:
            return

        if s.small_steps > _SWEEP_SMALL_STEP_LIMIT or sv.step == 0x7F:
            # continuous param detected (5 consecutive fine steps each moving
            # the display) or the ramp ran out -> finalise
            if s.small_steps > _SWEEP_SMALL_STEP_LIMIT:
                s.step_count = MAX_QUANTISED_STEPS + 1
            self._send_learn_param(s)
            return
        if readback < s.prev_readback - _RESTART_BACKSTEP:
            # config.lua restarts the sweep here (user still holding the
            # knob). Our read-back is optimistic-store + lagged feed, so a
            # backwards value may just be a stale report — log, don't restart.
            log.info("sweep read-back moved backwards (%.3f < %.3f); "
                     "user interference or stale feed", readback,
                     s.prev_readback)
        s.prev_readback = readback
        self.client.send_plugin_param_sweep(s.param)     # pull the next value

        if display and display != s.last_display:
            delta = sv.step - s.last_change_step
            s.last_change_step = sv.step
            s.small_steps = s.small_steps + 1 if delta <= _SWEEP_SMALL_DELTA else 0
            s.step_count += 1
            if s.step_count <= MAX_QUANTISED_STRING_STEPS:
                s.strings += 1
            s.last_display = display
            if len(s.curve) < 128:                   # measured value->display curve
                s.curve.append([round(readback, 4), display])

    def _send_learn_param(self, s: _Sweep) -> None:
        quantised = s.step_count if s.step_count <= MAX_QUANTISED_STEPS else 0
        strings = s.strings
        if 2 <= s.daw_steps <= MAX_QUANTISED_STEPS:
            # REAPER knows this param's step count — trust it over the
            # sweep-derived one (display timing can under-count)
            quantised = s.daw_steps
            strings = min(s.daw_steps, MAX_QUANTISED_STRING_STEPS)
        elif s.daw_steps == 0 and s.small_steps > _SWEEP_SMALL_STEP_LIMIT:
            quantised = 0
        self._build_calibration(s, quantised)
        placeholders = b""
        if quantised and quantised <= MAX_QUANTISED_STRING_STEPS:
            # zero-filled step strings: the device then shows live values.
            # 13 bytes per step (logic_failed.mmon: qsteps=5 carries 65 tail zeros)
            placeholders = b"\x00" * (STRING_LEN * strings)
        log.info("learn: LEARN_PARAM %d (%s) quantised_steps=%d",
                 s.param, s.name, quantised)
        self.client.send_learn_param(
            s.param, short_name(s.name), 0.0, codec.param_hash(s.name),
            quantised_steps=quantised, quantised_strings=placeholders)
        s.state = SweepState.AWAIT_COMPLETE
        s.last_activity = time.monotonic()
        self._learn_mode = int(LearnMode.END)   # config.lua: one learn per arm

    def _on_learn_complete(self) -> None:
        s = self._sweep
        if s is None:
            return
        kind = ("continuous" if (s.step_count == 0 or s.step_count > MAX_QUANTISED_STEPS)
                else "%d steps" % s.step_count)
        log.info("learn profile: param %d (%s) mapped as %s; daw_steps=%d, "
                 "curve=%d pts %s; restoring %.3f", s.param, s.name, kind,
                 s.daw_steps, len(s.curve), [d for _v, d in s.curve[:8]],
                 s.restore_value)
        self.source.set_device_param(s.device, s.param, s.restore_value)
        self.client.send_param_value_cc(s.param, s.restore_value)
        if self.on_learn_reference is not None:
            fx = next((d for d in self.source.devices()
                       if d.index == s.device), None)
            self.on_learn_reference({
                "hash": self._device_hash(fx.name).hex() if fx else None,
                "fx_name": fx.name if fx else "",
                "param_index": s.param, "param_name": s.name,
                "value": s.restore_value, "display": s.restore_display,
                # measured calibration: steps (sweep-derived), the DAW's own step
                # count, and the value->display curve — the data a translation
                # layer needs, and what makes a "broken" param's taper visible.
                "steps": s.step_count, "daw_steps": s.daw_steps, "curve": s.curve,
            })
        self._end_sweep()

    def _cancel_sweep(self) -> None:
        s = self._sweep
        if s is None:
            return
        if s.state != SweepState.AWAIT_COMPLETE:
            self.source.set_device_param(s.device, s.param, s.restore_value)
        self._end_sweep()

    def _end_sweep(self) -> None:
        s, self._sweep = self._sweep, None
        if s is not None:
            self._watch_params(s.device, self.source.device_params(s.device))

    # -- param traffic ------------------------------------------------------------
    def _param(self, device: int, param: int) -> Optional[PluginParam]:
        return next((p for p in self.source.device_params(device)
                     if p.index == param), None)

    def _build_calibration(self, s: _Sweep, quantised: int) -> None:
        """Turn the measured value->display curve into value snap-targets.

        Only *stepped* params (quantised >= 2) with a usable curve get an
        entry; the targets are the distinct values at which the display
        changed. Snapping the device's value to the nearest target lands the
        DAW on a real step even when the plugin's steps are unevenly spaced.
        Continuous/tapered params (quantised 0) get no entry — their taper is
        intentional and left alone."""
        key = (s.device, s.param)
        self._calibration.pop(key, None)
        if quantised < 2 or len(s.curve) < 2:
            return
        reps = sorted({round(v, 3) for v, _d in s.curve})
        if reps[0] > 0.05:
            reps.insert(0, 0.0)          # keep the first step reachable
        if len(reps) >= 2:
            self._calibration[key] = reps
            log.info("calibration: param %d (%s) -> %d snap targets %s",
                     s.param, s.name, len(reps), reps[:8])

    def _snap(self, dev: int, param: int, value: float) -> float:
        """Snap a device->DAW value to the nearest calibrated step, if any."""
        cal = self._calibration.get((dev, param))
        if not cal:
            return value
        return min(cal, key=lambda t: abs(t - value))

    def _on_device_param(self, param: int, value: float) -> None:
        """The device set a param (mapped knob turned / button toggled).
        CRITICAL: echo the applied value straight back (value CCs + display),
        throttled ~30ms like Logic. The knob's position authority and END STOPS
        are slaved to the DAW's confirmed value — without the echo the device
        abandons the anchor and the knob free-spins."""
        if self._sweep is not None and param == self._sweep.param:
            return          # our own sweep traffic
        dev = self.source.selected_device()
        # NORMALISE: the device sends the param index it LEARNED (a REAPER index,
        # say); route it to THIS DAW's index for the same param by identity hash.
        target = self._daw_param_for_device_index(dev, param)
        if log.isEnabledFor(logging.DEBUG):
            p = self._param(dev, target)
            log.debug("device param %d -> DAW %r (index %d)%s", param,
                      p.name if p else None, target,
                      "" if p else " (NOT in the DAW's exposed params)")
        # calibration stays keyed by the DEVICE knob (its learned curve/steps);
        # the resolved value is DAW-agnostic 0-1, so it applies to the target.
        value = self._snap(dev, param, value)
        self.source.set_device_param(dev, target, value)
        now = time.monotonic()
        if now - self._echo_last.get(param, 0.0) >= 0.025:
            self._echo_last[param] = now
            self.client.send_param_value_cc(param, value)   # echo at DEVICE index
            p = self._param(dev, target)                     # display from target
            if p is not None and p.display:
                self.client.send_param_display(param, p.display)

    def _on_param_touch(self, param: int, pressed: bool) -> None:
        if pressed:
            self._send_display(param)

    def _send_display(self, param: int) -> None:
        p = self._param(self.source.selected_device(), param)
        if p is not None:
            self.client.send_param_display(param, p.display or f"{p.value:.2f}")

    def _on_daw_param_value(self, fx: int, param: int, value: float,
                            display: str) -> None:
        """A param changed REAPER-side -> reflect to the device (config.lua
        only feeds values back while learn is off; sweeps drive themselves)."""
        if self._learn_mode != LearnMode.DISABLED or self._sweep is not None:
            return
        if fx != self.source.selected_device():
            return
        # echo at the DEVICE's learned index: cross-DAW, this DAW may expose
        # the param at a different index than the knob was learned with
        slot = self._device_index_for_daw_param(fx, param)
        if slot >= MAX_PLUGIN_PARAMETERS:
            return
        self.client.send_param_value_cc(slot, value)
        if display and self._display_cache.get(slot) != display:
            self._display_cache[slot] = display
            self.client.send_param_display(slot, display)

    # -- mixer ---------------------------------------------------------------------
    def _on_select_track(self, index: int) -> None:
        """Device selected a track (index = bank slot 0-7)."""
        raw = index
        if index < NUM_ENCODERS:
            index = self._first_track + index
        # In mix-ALL the device sends SELECT_TRACK on every knob TOUCH; unless
        # the user opted in, adjusting a volume knob must NOT move REAPER's
        # selection. A deliberate pick arrives with surface == "track_select".
        # (The capacitive-crosstalk ghost is already dropped in the client, so
        # what reaches here is the real, first-touched knob.)
        if self._surface == "mix" and not self.mix_touch_select:
            log.info("device SELECT_TRACK slot=%d ignored (mix touch-select "
                     "off)", raw)
            return
        log.info("device SELECT_TRACK: slot=%d first_track=%d -> track %d",
                 raw, self._first_track, index)
        self.source.set_selected_track(index)
        if self._surface == "mix":
            # opted-in mix touch-select: readout + motor echo, stay on mix
            self._push_mix_touch_readout()
        else:
            self._push_focus_track()

    def _on_mix_knob(self, cc: int, value: float) -> None:
        from ..sysex.constants import ENCODER_FIRST_CC
        ctl = cc - ENCODER_FIRST_CC
        if not (0 <= ctl < NUM_ENCODERS) or self._plugin_mode is True:
            return
        if self._mix_focus:
            # focused strip: the knobs are one track's channel controls
            si = self.source.selected_track()
            log.debug("mix knob %d (FOCUS page %d) -> track %d", ctl,
                      self._mix_focus_page, si)
            if self._mix_focus_page == 0 and ctl == 0:
                self.source.set_track_volume(si, value)
            elif self._mix_focus_page == 0 and ctl == 1:
                self.source.set_track_pan(si, value)
            else:
                self.source.set_track_send(si, self._focus_send_index(ctl),
                                           value)
            return
        idx = self._first_track + ctl
        log.debug("mix knob %d (bank %d) -> track %d = %.3f", ctl,
                  self._mix_knob_mode, idx, value)
        if self._mix_knob_mode == 1:
            self.source.set_track_pan(idx, value)
        elif self._mix_knob_mode == 2:
            self.source.set_track_send(idx, self._mix_send, value)
        else:
            self.source.set_track_volume(idx, value)

    def _on_daw_knob_value(self, track_index: int, value: float,
                           knob_mode: int) -> None:
        """A volume/pan changed in the DAW -> motor follows if that bank is
        on the knobs."""
        if self._plugin_mode is True:
            return
        if self._mix_focus:
            if track_index == self.source.selected_track() \
                    and self._mix_focus_page == 0 and knob_mode in (0, 1):
                # echo + readout stream, like Logic (logic-focus.mmon)
                self.client.send_encoder_value(knob_mode, value)
                tracks = self.source.tracks()
                if 0 <= track_index < len(tracks):
                    self.client.send_param_display(
                        knob_mode,
                        self._focus_readout(tracks[track_index], knob_mode))
            return
        ctl = track_index - self._first_track
        if 0 <= ctl < NUM_ENCODERS and self._mix_knob_mode == knob_mode:
            self.client.send_encoder_value(ctl, value)

    def _on_daw_send(self, track_index: int, send: int, value: float) -> None:
        if self._plugin_mode is True:
            return
        if self._mix_focus:
            if track_index == self.source.selected_track():
                base = 2 if self._mix_focus_page == 0 else 0
                page0 = 0 if self._mix_focus_page == 0 else NUM_ENCODERS - 2
                knob = base + send - page0
                if 0 <= knob < NUM_ENCODERS:
                    self.client.send_encoder_value(knob, value)
                    self.client.send_param_display(knob,
                                                   f"{round(value * 100)} %")
            return
        if send == self._mix_send:
            self._on_daw_knob_value(track_index, value, 2)

    def _on_mix_button(self, index: int, pressed: bool) -> None:
        """A surface button toggles the enable overlay's FX slot when that
        overlay is up, else the active mix bank's flag (mute/solo/arm/monitor).
        Plugin-mode buttons are the device's own business (learned switches
        arrive as param CCs)."""
        if not pressed:
            return
        if self._overlay == "enable":
            d = next((x for x in self.source.devices() if x.index == index),
                     None)
            if d is not None:
                new_enabled = not d.enabled
                self.source.set_device_enabled(index, new_enabled)
                self.client.send_button_led(index, new_enabled)
            return
        if self._plugin_mode is True:
            return
        tracks = self.source.tracks()
        idx = self._first_track + index
        if idx < len(tracks):
            flag = TRACK_FLAGS[self._mix_button_mode]
            new_on = not getattr(tracks[idx], flag)
            self.source.set_track_flag(idx, flag, new_on)
            # optimistic LED; the DAW's feedback re-confirms via _on_daw_flag
            lit = not new_on if flag == "muted" else new_on
            self.client.send_button_led(index, lit)

    def _on_daw_flag(self, track_index: int, flag: str, on: bool) -> None:
        ctl = track_index - self._first_track
        if 0 <= ctl < NUM_ENCODERS and self._plugin_mode is not True \
                and TRACK_FLAGS[self._mix_button_mode] == flag:
            self.client.send_button_led(ctl, not on if flag == "muted" else on)

    def _on_daw_vu(self, track_index: int, left: float, right: float) -> None:
        if self._minimal:
            return
        if self._mix_focus:
            # focus strips are volume/pan/sends, not tracks — per-track
            # meters would paint the wrong strips. The capture only shows
            # meters being zeroed at focus entry (no playback recorded), so
            # stay silent until a focus+playback capture says otherwise.
            return
        ctl = track_index - self._first_track
        if 0 <= ctl < NUM_ENCODERS and self._plugin_mode is not True:
            # delta-throttle like Logic (logic_vu capture: only changed
            # meters are sent) — REAPER streams /vu even for silent tracks
            q = (min(127, int(round(left * 127))),
                 min(127, int(round(right * 127))))
            if self._meter_cache.get(ctl) != q:
                self._meter_cache[ctl] = q
                self.client.send_meter(ctl, left, right)

    @property
    def first_track(self) -> int:
        """The first track on the 8-knob surface (bank window offset)."""
        return self._first_track

    def _on_page(self, delta: int) -> None:
        if self._mix_focus:
            # arrows in mix FOCUS switch the focused strip's page
            self._mix_focus_page = 1 if delta > 0 else 0
            self._push_focus_strip()
            return
        # Cubase pages its OWN 8-channel bank (the source re-streams the new
        # window, which re-fires on_tracks_changed -> _push_mixer); REAPER
        # instead windows the full track list the bridge already holds.
        if hasattr(self.source, "page") and self.source.page(delta):
            if self.on_bank_changed:
                self.on_bank_changed()
            return
        before = self._first_track
        self._first_track = paged_first_track(
            self._first_track, delta, len(self.source.tracks()))
        self._push_mixer()
        if self._first_track != before and self.on_bank_changed:
            self.on_bank_changed()      # let the app follow the device's page

    def _on_focus_changed(self) -> None:
        self._cancel_sweep()
        self._pushed_sig = None
        if self._plugin_mode:
            self._push_plugin_context()

    def _on_devices_changed(self) -> None:
        if self._plugin_mode:
            self._push_plugin_context()
