"""Cross-DAW Link pass-through: codec parse/rewrite + proxy transform (offline).

The wire foundation the design note says the tap must "validate": PLUGIN_DETAILS
round-trips, the identity rewrite touches only the 8 hash bytes, and the proxy
rewrites linked plugins while passing everything else through byte-for-byte.
"""
from __future__ import annotations

import pytest

from athens.roto.proxy import LinkProxy
from athens.sysex import codec


def a_details(index=1, name="Pro-Q 3", hash8=None, enabled=True):
    h = hash8 if hash8 is not None else codec.device_hash(name)
    return codec.plugin_details(index, name, h, enabled=enabled)


# --- codec ------------------------------------------------------------------

def test_plugin_details_roundtrip():
    frame = a_details(index=2, name="Reverb", enabled=False)
    pd = codec.parse_plugin_details(frame)
    assert pd.index == 2 and pd.name == "Reverb" and pd.enabled is False
    assert pd.hash8 == codec.device_hash("Reverb")
    assert codec.is_plugin_details(frame)


def test_is_plugin_details_rejects_others():
    assert not codec.is_plugin_details(codec.plugin_details_end())
    assert not codec.is_plugin_details(codec.num_tracks(4))
    assert not codec.is_plugin_details(b"\xf0\x7d\x01\x02\xf7")   # not a ROTO PD


def test_rewrite_only_touches_the_hash():
    frame = a_details(index=3, name="Delay")
    canon = bytes(range(100, 108))                # a distinct 8-byte identity
    out = codec.rewrite_plugin_details_hash(frame, canon)
    assert out[:8] == frame[:8] and out[16:] == frame[16:]   # only hash changed
    pd = codec.parse_plugin_details(out)
    assert pd.hash8 == canon and pd.index == 3 and pd.name == "Delay"


def test_rewrite_bad_hash_length():
    with pytest.raises(ValueError):
        codec.rewrite_plugin_details_hash(a_details(), b"short")


# --- proxy transform --------------------------------------------------------

class Rec:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(bytes(data))


def test_transform_rewrites_linked_plugin():
    dev, daw = Rec(), Rec()
    canon = bytes(range(8, 16))
    proxy = LinkProxy(dev, daw, resolver=lambda n: canon if n == "Pro-Q 3" else None)
    proxy.on_from_daw(a_details(name="Pro-Q 3"))
    assert proxy.seen == 1 and proxy.rewritten == 1
    assert codec.parse_plugin_details(dev.sent[0]).hash8 == canon


def test_transform_passes_through_unlinked():
    dev, daw = Rec(), Rec()
    proxy = LinkProxy(dev, daw, resolver=lambda n: None)   # nothing linked
    frame = a_details(name="Unknown")
    proxy.on_from_daw(frame)
    assert dev.sent[0] == frame and proxy.rewritten == 0


def test_transform_passes_through_non_details():
    dev, daw = Rec(), Rec()
    proxy = LinkProxy(dev, daw, resolver=lambda n: bytes(8))
    other = codec.num_tracks(3)
    proxy.on_from_daw(other)
    assert dev.sent[0] == other and proxy.seen == 0


def test_tap_mode_observes_without_rewriting():
    dev, daw = Rec(), Rec()
    seen = []
    proxy = LinkProxy(dev, daw, rewrite=False,
                      resolver=lambda n: bytes(range(8)),
                      observer=lambda name, h, i: seen.append((name, h, i)))
    frame = a_details(index=5, name="Comp")
    proxy.on_from_daw(frame)
    assert dev.sent[0] == frame and proxy.rewritten == 0   # tap never edits
    assert seen == [("Comp", codec.device_hash("Comp"), 5)]


def test_device_to_daw_is_byte_for_byte():
    dev, daw = Rec(), Rec()
    proxy = LinkProxy(dev, daw, resolver=lambda n: bytes(range(8)))
    blob = b"\xf0\x00\x22\x03\x02\x0b\x05anything\xf7"
    proxy.on_from_device(blob)
    assert daw.sent[0] == blob                     # device->DAW untouched


def test_transform_survives_resolver_exception():
    dev, daw = Rec(), Rec()

    def boom(_name):
        raise RuntimeError("registry on fire")

    proxy = LinkProxy(dev, daw, resolver=boom)
    frame = a_details(name="X")
    proxy.on_from_daw(frame)               # must not raise into the relay
    assert dev.sent[0] == frame and proxy.rewritten == 0   # passed through


# --- link registry helper (shared by the bridge + the proxy) ----------------

def test_hash_from_entry():
    from athens.links import hash_from_entry
    assert hash_from_entry({"hash": "0102030405060708"}) == bytes(range(1, 9))
    assert hash_from_entry({"hash": "00" * 8}) == bytes(8)
    assert hash_from_entry(None) is None                   # unlinked
    assert hash_from_entry({}) is None                     # no "hash" key
    assert hash_from_entry({"hash": "zz"}) is None          # not hex
    assert hash_from_entry({"hash": "0102"}) is None        # not 8 bytes
