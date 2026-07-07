"""DAW auto-detect: Athens picks whichever feed identifies itself, so the user
needn't pass --daw. The liveness probes are patched — no REAPER, no MIDI."""
from athens.daw import detect


def test_reaper_wins_when_heartbeat_is_live(monkeypatch):
    monkeypatch.setattr(detect, "reaper_feed_live", lambda: True)
    monkeypatch.setattr(detect, "cubase_bridge_live", lambda t=1.0: True)
    assert detect.detect_daw() == "reaper"        # checked first (instant stat)


def test_cubase_when_only_the_bridge_answers(monkeypatch):
    monkeypatch.setattr(detect, "reaper_feed_live", lambda: False)
    monkeypatch.setattr(detect, "cubase_bridge_live", lambda t=1.0: True)
    assert detect.detect_daw() == "cubase"


def test_falls_back_to_default_when_silent(monkeypatch):
    monkeypatch.setattr(detect, "reaper_feed_live", lambda: False)
    monkeypatch.setattr(detect, "cubase_bridge_live", lambda t=1.0: False)
    assert detect.detect_daw() == "reaper"
    assert detect.detect_daw(default="system") == "system"
