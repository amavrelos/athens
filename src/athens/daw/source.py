"""Data source for the native SysEx path.

Unlike `DawBackend` (which models a bank of knobs/buttons for the serial path),
a `SysexDawSource` exposes the DAW's *mixer model* — the track list, selection,
and transport — because in this protocol the device runs its own native mixer
and just needs the data. Implement one per DAW; the bridge is DAW-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from ..sysex.constants import TransportAction


@dataclass
class TrackInfo:
    index: int
    name: str
    colour: int = 0
    foldable: bool = False
    volume: float = 0.0     # normalised 0.0 - 1.0 (drives the motor in mix mode)
    pan: float = 0.5        # normalised, 0.5 = centre
    muted: bool = False
    soloed: bool = False
    armed: bool = False
    monitoring: bool = False


# the switchable mix-mode button banks (ROTO mix settings): TrackInfo field
# per bank, in the device's announcement order (SET_MIXER_ALL_MODE data)
TRACK_FLAGS = ("muted", "soloed", "armed", "monitoring")


@dataclass
class DeviceInfo:
    """One FX/instrument on the selected track (plugin mode)."""
    index: int
    name: str
    enabled: bool = True


@dataclass
class PluginParam:
    """One parameter of a device, as the DAW exposes it."""
    index: int
    name: str
    value: float = 0.0     # normalised 0.0 - 1.0
    display: str = ""      # formatted read-out, e.g. "1.2 kHz"
    steps: int = 0         # quantised step count (2 = toggle); 0 = continuous
    #                        or unknown — the DAW's own quantisation info


@dataclass
class TransportState:
    playing: bool = False
    recording: bool = False
    session_recording: bool = False
    loop: bool = False
    punch_in: bool = False
    punch_out: bool = False
    reenable_automation: bool = False


class SysexDawSource(ABC):
    DAW_NAME = "DAW"

    def __init__(self) -> None:
        self.on_tracks_changed: Optional[Callable[[], None]] = None
        self.on_transport_changed: Optional[Callable[[], None]] = None
        self.on_selected_track_changed: Optional[Callable[[], None]] = None
        # plugin mode: fired when the FX list changes, and when a mapped
        # param's value changes: (device_index, param_index, value, display)
        self.on_devices_changed: Optional[Callable[[], None]] = None
        self.on_device_param_value: Optional[
            Callable[[int, int, float, str], None]] = None
        # mix mode: a track's volume/pan/send changed in the DAW
        self.on_track_volume: Optional[Callable[[int, float], None]] = None
        self.on_track_pan: Optional[Callable[[int, float], None]] = None
        # (track_index, send_index, value)
        self.on_track_send: Optional[Callable[[int, int, float], None]] = None
        # mix mode: a track flag changed in the DAW: (track_index, flag, on)
        # where flag is one of TRACK_FLAGS
        self.on_track_flag: Optional[Callable[[int, str, bool], None]] = None
        # mix mode: VU levels (track_index, left 0..1, right 0..1)
        self.on_track_vu: Optional[Callable[[int, float, float], None]] = None
        # diagnostics: every raw bridge frame for the translation trace,
        # ("rx" DAW->Athens | "tx" Athens->DAW, wire bytes)
        self.on_frame: Optional[Callable[[str, bytes], None]] = None
        # learn mode: the user touched a param in the DAW UI:
        # (device_index, param_index) — the bridge answers with LEARN_PARAM
        self.on_param_touched: Optional[Callable[[int, int], None]] = None
        # plugin mode: the user focused a different plugin window -> the FX
        # context changed; the bridge resets mappings and re-pushes the device
        self.on_plugin_focus_changed: Optional[Callable[[], None]] = None
        # DAW liveness: fired False when the DAW disappears (e.g. REAPER
        # quit — detected via the feed heartbeat) and True when it returns.
        # The bridge blanks the device on False so it stops showing a ghost
        # session, and re-floods on True.
        self.on_daw_alive: Optional[Callable[[bool], None]] = None
        # version the LOADED companion script announced (Cubase DIAG / REAPER
        # live.json). The service compares it to the copy Athens bundles and, on
        # a mismatch, tells the user the host is running a stale script and must
        # be reloaded/restarted. Sources without a companion script never fire it.
        self.on_script_version: Optional[Callable[[str], None]] = None

    @abstractmethod
    def start(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @abstractmethod
    def tracks(self) -> List[TrackInfo]: ...
    @abstractmethod
    def selected_track(self) -> int: ...
    @abstractmethod
    def transport(self) -> TransportState: ...
    @abstractmethod
    def set_selected_track(self, index: int) -> None:
        """The device asked to select this track."""

    def set_track_volume(self, index: int, value: float) -> None:
        """A mixer knob moved on the hardware (default: ignore)."""

    def set_track_pan(self, index: int, value: float) -> None:
        """Pan knob (mix pan bank) moved on the hardware (default: ignore)."""

    def set_track_send(self, index: int, send: int, value: float) -> None:
        """Send knob (mix send bank) moved on the hardware (default: ignore)."""

    def track_send(self, index: int, send: int) -> float:
        """Current send level, for motor follow when the bank switches."""
        return 0.0

    def set_track_flag(self, index: int, flag: str, on: bool) -> None:
        """A mix button pressed on the hardware; flag is one of TRACK_FLAGS
        (default: ignore)."""

    # -- plugin mode (defaults are inert so mixer-only sources still work) --
    def devices(self) -> List[DeviceInfo]:
        """FX on the selected track."""
        return []

    def selected_device(self) -> int:
        return 0

    def set_selected_device(self, index: int) -> None:
        """The device asked to select this FX."""

    def focus_device(self, index: int) -> None:
        """Bring this FX's window to focus in the DAW (plugin-select overlay).
        Default: just select it."""
        self.set_selected_device(index)

    def set_device_enabled(self, index: int, enabled: bool) -> None:
        """Enable/bypass an FX (plugin-enable overlay). Default: ignore."""

    def device_params(self, device_index: int) -> List[PluginParam]:
        return []

    def set_device_param(self, device_index: int, param_index: int,
                         value: float) -> None:
        """A mapped knob moved on the hardware."""

    def set_watched_params(self, params: List[Tuple[int, int]]) -> None:
        """The bridge's currently mapped (device, param) pairs — a source that
        polls the DAW can restrict high-rate value watching to these."""

    def current_touched_param(self) -> Optional[Tuple[int, int]]:
        """The (device, param) the user is currently on, if the source can tell.
        The bridge offers it for binding when learn is armed."""
        return None

    def set_learn_armed(self, armed: bool) -> None:
        """Tell the source learn is armed, so a source that can detect the param
        the user is moving (rather than relying on a stale "last touched") may
        switch that detection on. Default: no-op."""

    def refresh_state(self) -> None:
        """Ask the DAW to re-send its full surface state (tracks, volumes,
        transport). Called when a device (re)connects. Default: no-op."""

    def transport_action(self, action: TransportAction, pressed: bool) -> None:
        """A transport button on the hardware (default: ignore)."""

    def run_action(self, action_id: int) -> None:
        """Fire a DAW-native action/command by id (user-assigned transport
        buttons). Default: ignore — not every DAW has an action registry."""


class MockSysexSource(SysexDawSource):
    """A fake 12-track session so the SysEx path runs and tests offline."""

    _NAMES = ["Kick", "Snare", "Hats", "Bass", "Keys", "Pad",
              "Gtr", "Vox", "Lead", "FX", "Bus", "Master"]

    def __init__(self) -> None:
        super().__init__()
        self._tracks = [TrackInfo(i, n, colour=(i % 12) or 1, volume=0.75)
                        for i, n in enumerate(self._NAMES)]
        self._sends: dict[tuple, float] = {}
        self._selected = 0
        self._transport = TransportState()
        self._selected_device = 0
        self._touched: Optional[Tuple[int, int]] = None
        self.watched: List[Tuple[int, int]] = []   # last set_watched_params()
        self._devices = [DeviceInfo(0, "EQ Eight"), DeviceInfo(1, "Compressor"),
                         DeviceInfo(2, "Reverb")]
        self._params: dict[int, List[PluginParam]] = {
            0: [PluginParam(0, "Freq A", 0.40, "1.2 kHz"),
                PluginParam(1, "Gain A", 0.50, "0.0 dB"),
                PluginParam(2, "Q A", 0.30, "0.71"),
                PluginParam(3, "Freq B", 0.60, "3.4 kHz")],
            1: [PluginParam(0, "Threshold", 0.70, "-12.0 dB"),
                PluginParam(1, "Ratio", 0.25, "4:1"),
                PluginParam(2, "Attack", 0.10, "1.0 ms"),
                PluginParam(3, "Release", 0.35, "120 ms")],
            2: [PluginParam(0, "Mix", 0.5, "50%"),
                PluginParam(1, "Size", 0.8, "Large")],
        }

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def tracks(self) -> List[TrackInfo]:
        return list(self._tracks)

    def selected_track(self) -> int:
        return self._selected

    def transport(self) -> TransportState:
        return self._transport

    def set_selected_track(self, index: int) -> None:
        changed = self._selected != index
        self._selected = index
        if changed and self.on_selected_track_changed:
            self.on_selected_track_changed()

    def set_track_volume(self, index: int, value: float) -> None:
        if 0 <= index < len(self._tracks):
            self._tracks[index].volume = value

    def set_track_pan(self, index: int, value: float) -> None:
        if 0 <= index < len(self._tracks):
            self._tracks[index].pan = value

    def set_track_send(self, index: int, send: int, value: float) -> None:
        self._sends[(index, send)] = value

    def track_send(self, index: int, send: int) -> float:
        return self._sends.get((index, send), 0.0)

    def set_track_flag(self, index: int, flag: str, on: bool) -> None:
        if 0 <= index < len(self._tracks):
            setattr(self._tracks[index], flag, on)

    def simulate_volume(self, index: int, value: float) -> None:
        self._tracks[index].volume = value
        if self.on_track_volume:
            self.on_track_volume(index, value)

    def simulate_flag(self, index: int, flag: str, on: bool) -> None:
        setattr(self._tracks[index], flag, on)
        if self.on_track_flag:
            self.on_track_flag(index, flag, on)

    # -- plugin mode --------------------------------------------------------
    def devices(self) -> List[DeviceInfo]:
        return list(self._devices)

    def selected_device(self) -> int:
        return self._selected_device

    def set_selected_device(self, index: int) -> None:
        if 0 <= index < len(self._devices):
            self._selected_device = index

    def device_params(self, device_index: int) -> List[PluginParam]:
        return list(self._params.get(device_index, []))

    def set_device_param(self, device_index: int, param_index: int,
                         value: float) -> None:
        params = self._params.get(device_index, [])
        if 0 <= param_index < len(params):
            params[param_index].value = value

    def set_device_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self._devices):
            self._devices[index].enabled = enabled

    def set_watched_params(self, params: List[Tuple[int, int]]) -> None:
        self.watched = list(params)

    def simulate_device_param(self, device_index: int, param_index: int,
                              value: float, display: str) -> None:
        p = self._params[device_index][param_index]
        p.value, p.display = value, display
        if self.on_device_param_value:
            self.on_device_param_value(device_index, param_index, value, display)

    def simulate_param_touch(self, device_index: int, param_index: int) -> None:
        """The user grabbed a param in the DAW UI (feeds learn mode)."""
        self._touched = (device_index, param_index)
        if self.on_param_touched:
            self.on_param_touched(device_index, param_index)

    def current_touched_param(self) -> Optional[Tuple[int, int]]:
        t = self._touched
        if t is not None and t[0] == self._selected_device:
            return t
        return None

    # -- transport ----------------------------------------------------------
    _TRANSPORT_TOGGLES = {
        TransportAction.PLAY: "playing",
        TransportAction.RECORD: "recording",
        TransportAction.SESSION_RECORD: "session_recording",
        TransportAction.LOOP: "loop",
        TransportAction.PUNCH_IN: "punch_in",
        TransportAction.PUNCH_OUT: "punch_out",
        TransportAction.REENABLE_AUTOMATION: "reenable_automation",
    }

    def transport_action(self, action: TransportAction, pressed: bool) -> None:
        if not pressed:
            return
        if action == TransportAction.STOP:
            self._transport.playing = False
        elif action in self._TRANSPORT_TOGGLES:
            attr = self._TRANSPORT_TOGGLES[action]
            setattr(self._transport, attr, not getattr(self._transport, attr))
        else:
            return   # rewind / fastforward move the playhead, no state here
        if self.on_transport_changed:
            self.on_transport_changed()

    # -- test/demo helpers to simulate DAW-side changes --------------------
    def simulate_play(self, playing: bool = True) -> None:
        self._transport.playing = playing
        if self.on_transport_changed:
            self.on_transport_changed()

    def simulate_rename(self, index: int, name: str) -> None:
        self._tracks[index].name = name
        if self.on_tracks_changed:
            self.on_tracks_changed()
