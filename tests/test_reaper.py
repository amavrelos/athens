"""Unit tests for the REAPER backend's pure helpers (no network needed)."""
from athens.daw.reaper import track_index, volume_key


def test_track_index_parses_per_track_address():
    assert track_index("/track/3/volume") == 3
    assert track_index("/track/12/pan") == 12
    assert track_index("track/7/name") == 7   # leading slash optional


def test_track_index_rejects_non_track_addresses():
    assert track_index("/fx/1/param/2") is None
    assert track_index("/track/name") is None
    assert track_index("/master/volume") is None
    assert track_index("/track/x/volume") is None


def test_volume_key_roundtrips_with_track_index():
    for n in (1, 5, 8):
        assert track_index("/" + volume_key(n)) == n
