"""A hardware/DAW-free backend so the whole bridge can run and be tested offline.

Presents a fake 8-track mixer: 8 knobs (volumes) + 8 mute buttons. `set_param` /
`set_switch` update in-memory state and echo back through the callbacks, enough
to watch the bridge wire both directions.
"""
from __future__ import annotations

import logging

from .backend import Bank, DawBackend, Param, SwitchSpec

log = logging.getLogger(__name__)

_TRACKS = ["Kick", "Snare", "Bass", "Keys", "Gtr", "Vox", "FX", "Master"]
_COLOURS = [1, 2, 3, 4, 5, 6, 7, 8]


class MockDawBackend(DawBackend):
    def __init__(self) -> None:
        super().__init__()
        self._vol = {f"track/{i}/volume": 0.75 for i in range(len(_TRACKS))}
        self._mute = {f"track/{i}/mute": False for i in range(len(_TRACKS))}

    def start(self) -> None:
        log.info("MockDaw started with %d tracks", len(_TRACKS))

    def stop(self) -> None:
        log.info("MockDaw stopped")

    def current_bank(self) -> Bank:
        params = [
            Param(key=f"track/{i}/volume", name=name,
                  value=self._vol[f"track/{i}/volume"], colour=_COLOURS[i])
            for i, name in enumerate(_TRACKS)
        ]
        buttons = [
            SwitchSpec(key=f"track/{i}/mute", name=f"M {name}",
                       on=self._mute[f"track/{i}/mute"], colour=2)
            for i, name in enumerate(_TRACKS)
        ]
        return Bank(title="Mixer", params=params, buttons=buttons)

    def set_param(self, key: str, value: float) -> None:
        if key in self._vol:
            self._vol[key] = value
            log.info("MockDaw set %s = %.3f", key, value)
            if self.on_param_value:
                self.on_param_value(key, value)

    def set_switch(self, key: str, on: bool) -> None:
        if key in self._mute:
            self._mute[key] = on
            log.info("MockDaw set %s = %s", key, on)
            if self.on_switch_state:
                self.on_switch_state(key, on)
