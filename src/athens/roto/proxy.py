"""Cross-DAW Link pass-through — a transparent MIDI proxy.

A DAW's native ROTO integration (Ableton / Bitwig / Logic) talks straight to the
device. To share learned plugin maps across DAWs, Athens sits *in* the MIDI path
as a transparent proxy and rewrites the ONE field the device keys its maps on:
the 8-byte plugin identity in PLUGIN_DETAILS. Everything else relays
byte-for-byte, so the proxy never has to understand the DAW's dialect.

See docs/cross-daw-link-passthrough.md. The `transform` is pure + unit-tested;
the port wiring is injected by the launcher (needs real virtual MIDI + device).
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ..sysex import codec

log = logging.getLogger(__name__)

HashResolver = Callable[[str], Optional[bytes]]   # name -> canonical hash | None
Observer = Callable[[str, bytes, int], None]      # name, hash8, index


class LinkProxy:
    """Relays DAW<->device, rewriting the identity on PLUGIN_DETAILS
    (DAW->device) so a map learned in one DAW is served in all.

    resolver: name -> canonical 8-byte hash to substitute, or None to leave the
              frame untouched (unlinked plugins pass through unchanged).
    observer: called for every plugin-select seen — the passive-tap hook that
              learns which maps a native DAW already has.
    rewrite:  False = passive tap (observe only, never edit the live stream)."""

    def __init__(self, to_device, to_daw, *,
                 resolver: Optional[HashResolver] = None,
                 observer: Optional[Observer] = None, rewrite: bool = True):
        self._to_device = to_device      # port.send() reaches the real device
        self._to_daw = to_daw            # port.send() reaches the DAW
        self._resolver = resolver
        self._observer = observer
        self._rewrite = rewrite
        self.seen = 0                    # PLUGIN_DETAILS frames observed
        self.rewritten = 0               # of those, how many we re-identified

    def transform(self, frame: bytes) -> bytes:
        """A DAW->device frame: observe + (optionally) rewrite the identity on
        PLUGIN_DETAILS; anything else is returned unchanged (byte-for-byte)."""
        if not codec.is_plugin_details(frame):
            return frame
        try:
            pd = codec.parse_plugin_details(frame)
        except codec.ProtocolError:
            return frame                          # malformed -> pass through
        self.seen += 1
        if self._observer is not None:
            self._observer(pd.name, pd.hash8, pd.index)
        if self._rewrite and self._resolver is not None:
            try:
                canonical = self._resolver(pd.name)
            except Exception:  # noqa: BLE001 - a resolver hiccup must not break the relay
                log.exception("Link resolver failed for %r", pd.name)
                canonical = None
            if canonical is not None and bytes(canonical) != pd.hash8:
                self.rewritten += 1
                log.info("Link: %r %s -> %s", pd.name, pd.hash8.hex(),
                         bytes(canonical).hex())
                return codec.rewrite_plugin_details_hash(frame, bytes(canonical))
        return frame

    # --- relay: the launcher wires these to the two input ports' callbacks ---
    def on_from_daw(self, frame: bytes) -> None:
        """DAW -> (transform) -> device."""
        self._to_device.send(self.transform(bytes(frame)))

    def on_from_device(self, frame: bytes) -> None:
        """device -> DAW, byte-for-byte (the device never carries the DAW's
        identity choice, so nothing to rewrite this way)."""
        self._to_daw.send(bytes(frame))


def _pick_port(names, hint: str) -> str:
    """The real device port matching hint — never our own virtual pair or the
    Cubase loopback bridge."""
    for n in names:
        low = n.lower()
        if hint.lower() in low and "bridge" not in low and "athens" not in low:
            return n
    raise RuntimeError("no MIDI port matching %r (have: %s)" % (hint, list(names)))


def _link_resolver() -> HashResolver:
    """name -> canonical 8-byte hash, read from the persisted Link registry
    (the same parse the in-process bridge uses, via links.hash_from_entry)."""
    import json
    from ..config import config_dir
    from ..links import hash_from_entry
    try:
        registry = json.loads((config_dir() / "plugin-links.json").read_text())
    except (OSError, ValueError):
        registry = {}
    return lambda name: hash_from_entry(registry.get(name))


def run_link_proxy(*, tap: bool = False, device_hint: str = "ROTO",
                   port_name: str = "Athens Link") -> int:
    """Open a virtual MIDI pair the DAW points at, relay it to the real device,
    and rewrite plugin identities from the Link registry so maps are shared
    across DAWs. Blocks until Ctrl-C. tap=True observes without rewriting.

    Needs real virtual MIDI (CoreMIDI / ALSA) + the device on USB — validate on
    hardware. Windows has no built-in virtual MIDI (see the design note)."""
    import time
    import mido

    class _Out:                          # adapt a mido output to .send(bytes)
        def __init__(self, port):
            self._p = port

        def send(self, data):
            self._p.send(mido.parse(list(data)))

    resolver = None if tap else _link_resolver()
    dev_out = mido.open_output(_pick_port(mido.get_output_names(), device_hint))
    daw_out = mido.open_output(port_name, virtual=True)   # the DAW receives here

    def _observe(name, hash8, index):
        log.info("Link tap: plugin %d %r id=%s", index, name, hash8.hex())

    proxy = LinkProxy(_Out(dev_out), _Out(daw_out), resolver=resolver,
                      observer=_observe, rewrite=not tap)

    dev_in = mido.open_input(
        _pick_port(mido.get_input_names(), device_hint),
        callback=lambda m: proxy.on_from_device(bytes(m.bytes())))
    daw_in = mido.open_input(
        port_name, virtual=True,
        callback=lambda m: proxy.on_from_daw(bytes(m.bytes())))

    log.info("Link %s up — point your DAW's ROTO surface at the virtual MIDI "
             "port %r (in+out), then select plugins. Ctrl-C to stop.",
             "TAP (observe-only)" if tap else "proxy", port_name)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in (dev_in, daw_in):       # detach callbacks before close (CoreMIDI)
            try:
                p.callback = None
            except Exception:            # noqa: BLE001 - best-effort
                pass
        for p in (dev_in, dev_out, daw_in, daw_out):
            try:
                p.close()
            except Exception:            # noqa: BLE001 - best-effort
                pass
    log.info("Link stopped — %d plugin-selects seen, %d rewritten",
             proxy.seen, proxy.rewritten)
    return 0
