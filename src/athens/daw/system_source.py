"""System adapter — control the Mac itself, no DAW anywhere.

The multi-DAW seam taken to its conclusion: the "session" is the operating
system. The device runs its native DAW mode against this source, so knob
end-stops, value readouts, flash-stored maps and the whole app keep working.

Layout:
* MIX screen — strips are levels: `Sys Vol` (system output volume, with
  readback so the motor follows keyboard volume changes) and `Display`
  (internal-display brightness via the `brightness` CLI when installed).
* PLUGIN screen — one plugin named `System`: Volume, Brightness, the
  **Cursor** action knob (adjusts whatever UI control the mouse is over —
  Accessibility API when the element exposes a value, synthesized scroll
  otherwise; the knob re-centres after each gesture like a spring), plus
  user-defined params from `system-controls.json` (AppleScript or shell,
  `{value}` = 0-100).
* Transport — media keys: play/pause, previous, next (system-wide when pyobjc
  is present; AppleScript fallback to Music/Spotify).

macOS notes: cursor + media keys need pyobjc (a base darwin dependency of
`pip install -e "."` — there is no [system] extra) and the Accessibility
permission; system volume works out of the box via osascript; per-app volume
is not natively possible and stays out of scope.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional

from .source import (
    DeviceInfo, PluginParam, SysexDawSource, TrackInfo, TransportAction,
    TransportState,
)

log = logging.getLogger(__name__)

_CURSOR_SENSITIVITY = 40      # scroll pixels per full knob turn segment
_CURSOR_RECENTER_S = 0.4      # idle time before the knob springs back

# System Settings deep-link to the Accessibility list (stable for years)
_AX_SETTINGS_URL = ("x-apple.systempreferences:"
                    "com.apple.preference.security?Privacy_Accessibility")


def _app_identity() -> dict:
    """Which macOS 'app' the Accessibility grant will attach to. A bundled,
    codesigned .app is what macOS is built around; a bare venv-python grant
    is flaky for event posting AND, when launched from a terminal, macOS
    attributes it to the responsible PARENT app (the terminal / host) — so
    the entry can read e.g. 'Terminal', not us. The UI steers the user to the
    bundle accordingly. Its name is derived from the .app so a rebuild under
    a new product name (Athens) shows correctly in the TCC list."""
    exe = Path(sys.executable)
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        app = next((p for p in exe.parents if p.suffix == ".app"), None)
        name = app.stem if app is not None else exe.name
    else:
        name = exe.name
    return {"bundle": frozen, "name": name, "path": str(exe)}


def system_permissions() -> dict:
    """What the System adapter can actually do on this machine right now.

    Deliberately called ONLY when the user opted into system control (the
    Settings toggle or --daw system) — plain DAW users must never see a
    permission probe. Read-only: no dialog, no side effects (that's
    request_accessibility()). CGPreflightPostEventAccess covers the
    Accessibility permission that event posting (scroll, media keys) and AX
    value setting ride on; Quartz always ships with the app, so no probe can
    fail for a missing package."""
    out = {"pyobjc": False, "accessibility": None,
           "brightness_cli": shutil.which("brightness") is not None,
           "identity": _app_identity()}
    try:
        import Quartz
        out["pyobjc"] = True
        out["accessibility"] = bool(Quartz.CGPreflightPostEventAccess())
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — a probe must never crash startup
        log.warning("accessibility probe failed: %s", exc)
    return out


def _register_post_event_access() -> Optional[bool]:
    """Best-effort: ask macOS to register this app for event-posting access.
    The one-time consent dialog only shows from the main run loop, so this
    mainly ensures an entry exists in the Accessibility list to toggle."""
    try:
        import Quartz
        return bool(Quartz.CGRequestPostEventAccess())
    except Exception:  # noqa: BLE001 — never crash the grant flow
        return None


def request_accessibility() -> dict:
    """User-initiated grant flow (the Settings button). Registers the app in
    the TCC list (best-effort — we do NOT rely on the one-time consent
    dialog) and opens the Accessibility settings pane, which works from any
    thread every time. Returns the fresh status so the UI can guide next
    steps (enable the entry, then relaunch — event-posting grants take
    effect on restart)."""
    _register_post_event_access()
    opened = False
    try:
        subprocess.run(["open", _AX_SETTINGS_URL], timeout=5.0, check=False)
        opened = True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not open Accessibility settings: %s", exc)
    status = system_permissions()
    status["opened_settings"] = opened
    return status


class MacController:
    """Thin, injectable wrapper around the OS surfaces (tests stub `run`)."""

    def __init__(self, run: Optional[Callable[[list], str]] = None,
                 enable_hid: bool = True):
        self.run = run or self._run
        self.brightness_cli = shutil.which("brightness")
        # HID media-key events reach the REAL session — injectable so tests
        # never toggle the user's music
        self.enable_hid = enable_hid

    @staticmethod
    def _run(cmd: list) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=3.0)
            return (out.stdout or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("system command failed (%s): %s", cmd[0], exc)
            return ""

    # -- volume ---------------------------------------------------------------
    def get_volume(self) -> Optional[float]:
        out = self.run(["osascript", "-e",
                        "output volume of (get volume settings)"])
        try:
            return max(0.0, min(1.0, int(out) / 100.0))
        except ValueError:
            return None

    def set_volume(self, value: float) -> None:
        self.run(["osascript", "-e",
                  f"set volume output volume {round(value * 100)}"])

    # -- display brightness ----------------------------------------------------
    def get_brightness(self) -> Optional[float]:
        if not self.brightness_cli:
            return None
        out = self.run([self.brightness_cli, "-l"])
        for line in out.splitlines():          # "display 0: brightness 0.625"
            if "brightness" in line:
                try:
                    return max(0.0, min(1.0, float(line.rsplit(" ", 1)[-1])))
                except ValueError:
                    pass
        return None

    def set_brightness(self, value: float) -> None:
        if self.brightness_cli:
            self.run([self.brightness_cli, f"{value:.3f}"])

    # -- media keys -------------------------------------------------------------
    def media_key(self, key: str) -> None:
        """key: 'playpause' | 'previous' | 'next'. System-wide HID event via
        pyobjc when available, else AppleScript to the running player."""
        if self._hid_media_key(key):
            return
        script = {"playpause": "playpause", "previous": "previous track",
                  "next": "next track"}[key]
        for app in ("Spotify", "Music"):
            probe = self.run(["osascript", "-e",
                              f'application "{app}" is running'])
            if probe == "true":
                self.run(["osascript", "-e",
                          f'tell application "{app}" to {script}'])
                return

    def _hid_media_key(self, key: str) -> bool:
        if not self.enable_hid:
            return False
        try:
            import Quartz  # pyobjc, optional (the `system` extra)
        except ImportError:
            return False
        codes = {"playpause": 16, "previous": 20, "next": 19}  # NX_KEYTYPE_*
        code = codes[key]
        for down in (True, False):
            flags = 0xA00 if down else 0xB00
            ev = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(  # noqa: E501
                14, (0, 0), 0xA00, 0, 0, None, 8,
                (code << 16) | (flags << 8), -1)
            Quartz.CGEventPost(0, ev.CGEvent())
        return True


class CursorControl:
    """The action knob: adjust the UI control under the mouse cursor.

    Primary mechanism is a synthesized scroll-wheel event at the cursor
    position (near-universal). When the Accessibility API exposes a settable
    numeric value on the element under the cursor, that is used instead for
    precise stepping. Needs the `system` extra + Accessibility permission."""

    def __init__(self) -> None:
        try:
            import Quartz  # noqa: F401
            self.available = True
        except ImportError:
            self.available = False
            log.warning("cursor knob disabled — pyobjc is missing (a base "
                        'macOS dependency: pip install -e ".") and the '
                        "Accessibility permission must be granted")

    def adjust(self, delta: float) -> None:
        """delta: signed fraction of a knob sweep (positive = increase)."""
        if not self.available or not delta:
            return
        if self._ax_adjust(delta):
            return
        self._scroll(delta)

    def _scroll(self, delta: float) -> None:
        import Quartz
        px = int(round(delta * _CURSOR_SENSITIVITY * 4))
        if px == 0:
            px = 1 if delta > 0 else -1
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 1, px)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def _ax_adjust(self, delta: float) -> bool:
        try:
            import Quartz
            from ApplicationServices import (
                AXUIElementCopyAttributeValue, AXUIElementCopyElementAtPosition,
                AXUIElementCreateSystemWide, AXUIElementSetAttributeValue,
            )
            loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            err, el = AXUIElementCopyElementAtPosition(
                AXUIElementCreateSystemWide(), loc.x, loc.y, None)
            if err or el is None:
                return False
            vals = {}
            for attr in ("AXValue", "AXMinValue", "AXMaxValue"):
                e, v = AXUIElementCopyAttributeValue(el, attr, None)
                if e or not isinstance(v, (int, float)):
                    return False
                vals[attr] = float(v)
            span = vals["AXMaxValue"] - vals["AXMinValue"]
            if span <= 0:
                return False
            new = max(vals["AXMinValue"],
                      min(vals["AXMaxValue"], vals["AXValue"] + delta * span))
            return AXUIElementSetAttributeValue(el, "AXValue", new) == 0
        except Exception:  # noqa: BLE001 — AX is best-effort; scroll fallback
            return False


class SystemSource(SysexDawSource):
    DAW_NAME = "System"

    PARAM_VOLUME, PARAM_BRIGHTNESS, PARAM_CURSOR = 0, 1, 2

    def __init__(self, controller: Optional[MacController] = None,
                 cursor: Optional[CursorControl] = None,
                 controls_path: Optional[Path] = None,
                 poll: bool = True) -> None:
        super().__init__()
        self.mac = controller or MacController()
        self.cursor = cursor or CursorControl()
        self._poll_enabled = poll
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recenter: Optional[threading.Timer] = None
        self._volume = self.mac.get_volume() or 0.5
        self._brightness = self.mac.get_brightness()
        self._cursor_pos = 0.5
        self._selected = 0
        self._transport = TransportState()
        self._custom = self._load_custom(controls_path)

    @staticmethod
    def _load_custom(path: Optional[Path]) -> list:
        """system-controls.json: [{"name": "...", "kind": "shell"|
        "applescript", "set": "... {value} ..."}] — {value} = 0-100."""
        if path is None:
            path = (Path.home() / "Library/Application Support/roto-reaper"
                    / "system-controls.json")
        try:
            entries = json.loads(Path(path).read_text())
            return [e for e in entries
                    if isinstance(e, dict) and e.get("name") and e.get("set")]
        except (OSError, ValueError):
            return []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._poll_enabled and (self._thread is None
                                   or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll, daemon=True,
                                            name="system-source")
            self._thread.start()
        if self.on_tracks_changed:
            self.on_tracks_changed()

    def stop(self) -> None:
        self._stop.set()
        if self._recenter is not None:
            self._recenter.cancel()

    def _poll(self) -> None:
        """Readback loop: volume changed from the keyboard -> motors follow."""
        while not self._stop.wait(1.5):
            v = self.mac.get_volume()
            if v is not None and abs(v - self._volume) > 0.005:
                self._volume = v
                self._announce_volume()

    def _announce_volume(self) -> None:
        if self.on_track_volume:
            self.on_track_volume(0, self._volume)
        if self.on_device_param_value:
            self.on_device_param_value(0, self.PARAM_VOLUME, self._volume,
                                       f"{round(self._volume * 100)} %")

    # -- session ------------------------------------------------------------
    def tracks(self) -> List[TrackInfo]:
        out = [TrackInfo(index=0, name="Sys Vol", colour=10,
                         volume=self._volume)]
        if self._brightness is not None:
            out.append(TrackInfo(index=1, name="Display", colour=4,
                                 volume=self._brightness))
        return out

    def selected_track(self) -> int:
        return self._selected

    def set_selected_track(self, index: int) -> None:
        if 0 <= index < len(self.tracks()):
            self._selected = index
            if self.on_selected_track_changed:
                self.on_selected_track_changed()

    def transport(self) -> TransportState:
        return self._transport

    def set_track_volume(self, index: int, value: float) -> None:
        if index == 0:
            self._volume = value
            self.mac.set_volume(value)
        elif index == 1 and self._brightness is not None:
            self._brightness = value
            self.mac.set_brightness(value)

    def transport_action(self, action: TransportAction, pressed: bool) -> None:
        if not pressed:
            return
        if action in (TransportAction.PLAY, TransportAction.STOP):
            self.mac.media_key("playpause")
        elif action == TransportAction.REWIND:
            self.mac.media_key("previous")
        elif action == TransportAction.FASTFORWARD:
            self.mac.media_key("next")

    # -- the System plugin ----------------------------------------------------
    def devices(self) -> List[DeviceInfo]:
        return [DeviceInfo(index=0, name="System", enabled=True)]

    def selected_device(self) -> int:
        return 0

    def device_params(self, device_index: int) -> List[PluginParam]:
        params = [
            PluginParam(self.PARAM_VOLUME, "Volume", self._volume,
                        f"{round(self._volume * 100)} %"),
            PluginParam(self.PARAM_BRIGHTNESS, "Brightness",
                        self._brightness if self._brightness is not None
                        else 0.0,
                        f"{round((self._brightness or 0) * 100)} %"
                        if self._brightness is not None else "n/a"),
            PluginParam(self.PARAM_CURSOR, "Cursor", self._cursor_pos,
                        "under mouse" if self.cursor.available else "n/a"),
        ]
        for i, entry in enumerate(self._custom):
            params.append(PluginParam(3 + i, str(entry["name"])[:24],
                                      float(entry.get("value", 0.0)),
                                      entry.get("display", "")))
        return params

    def set_device_param(self, device_index: int, param_index: int,
                         value: float) -> None:
        if param_index == self.PARAM_VOLUME:
            self._volume = value
            self.mac.set_volume(value)
            self._announce_volume()
        elif param_index == self.PARAM_BRIGHTNESS:
            if self._brightness is not None:
                self._brightness = value
                self.mac.set_brightness(value)
        elif param_index == self.PARAM_CURSOR:
            self._cursor_moved(value)
        else:
            self._run_custom(param_index - 3, value)

    def _cursor_moved(self, value: float) -> None:
        self.cursor.adjust(value - self._cursor_pos)
        self._cursor_pos = value
        # spring-return: an absolute motor knob acting as a relative encoder
        if self._recenter is not None:
            self._recenter.cancel()
        self._recenter = threading.Timer(_CURSOR_RECENTER_S, self._do_recenter)
        self._recenter.daemon = True
        self._recenter.start()

    def _do_recenter(self) -> None:
        self._cursor_pos = 0.5
        if self.on_device_param_value:
            self.on_device_param_value(0, self.PARAM_CURSOR, 0.5,
                                       "under mouse")

    def _run_custom(self, idx: int, value: float) -> None:
        if not 0 <= idx < len(self._custom):
            return
        entry = self._custom[idx]
        entry["value"] = value
        cmd = str(entry["set"]).replace("{value}", str(round(value * 100)))
        if entry.get("kind") == "applescript":
            self.mac.run(["osascript", "-e", cmd])
        else:
            self.mac.run(["sh", "-c", cmd])
