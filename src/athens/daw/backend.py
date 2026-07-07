"""The DAW-facing interface and its data models.

A `Bank` is one surface layout: up to 8 knob parameters (values 0.0-1.0) plus a
set of buttons (on/off, for mute/solo/bypass/etc.). The backend fires callbacks
when the DAW changes underneath us:
  * on_param_value(key, value)  -> a knob value moved (motor follow)
  * on_switch_state(key, on)    -> a button state changed (LED follow)
  * on_bank_changed(bank)       -> the whole surface should be rebuilt
                                   (track selected, FX focus changed, view switched)
Anything DAW-specific lives in a concrete subclass, never here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, List, Optional

KNOBS_PER_PAGE = 8
BUTTONS_TOTAL = 16      # ROTO-CONTROL has 16 RGB buttons


@dataclass
class Param:
    """One controllable parameter as the DAW exposes it (a knob)."""
    key: str            # stable id, e.g. "track/3/volume" or "fx/2/param/5"
    name: str           # short label for the knob display (<= 12 chars ideal)
    value: float        # normalised 0.0 - 1.0
    colour: int = 0     # ROTO colour-scheme index (0x00 - 0x52), optional


@dataclass
class SwitchSpec:
    """One button: a toggled (or momentary) on/off control with an LED."""
    key: str            # e.g. "track/3/mute"
    name: str
    on: bool = False
    colour: int = 0     # LED-on colour scheme (0x00 - 0x52)
    momentary: bool = False


@dataclass
class Bank:
    """One surface layout: the knobs and buttons for the current context."""
    title: str
    params: List[Optional[Param]] = field(default_factory=list)
    buttons: List[Optional[SwitchSpec]] = field(default_factory=list)

    def knobs_padded(self) -> List[Optional[Param]]:
        p = list(self.params[:KNOBS_PER_PAGE])
        return p + [None] * (KNOBS_PER_PAGE - len(p))

    def buttons_padded(self) -> List[Optional[SwitchSpec]]:
        b = list(self.buttons[:BUTTONS_TOTAL])
        return b + [None] * (BUTTONS_TOTAL - len(b))


class DawBackend(ABC):
    """Implement one of these per DAW."""

    def __init__(self) -> None:
        # bridge sets these; backend calls them when the DAW changes
        self.on_bank_changed: Optional[Callable[[Bank], None]] = None
        self.on_param_value: Optional[Callable[[str, float], None]] = None
        self.on_switch_state: Optional[Callable[[str, bool], None]] = None

    @abstractmethod
    def start(self) -> None:
        """Open connections / begin listening to the DAW."""

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def current_bank(self) -> Bank:
        """The knobs + buttons that should be on the surface right now."""

    @abstractmethod
    def set_param(self, key: str, value: float) -> None:
        """Apply a knob movement (0.0-1.0) to the DAW."""

    @abstractmethod
    def set_switch(self, key: str, on: bool) -> None:
        """Apply a button press (new on/off state) to the DAW."""
