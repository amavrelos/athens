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


def test_fallback_outcome_is_recorded(monkeypatch):
    """A fallback is a GUESS, not a detection — the flag lets the service/UI
    word the connect hint honestly ("no DAW feed found", not "REAPER
    detected") when nothing ever announced itself."""
    monkeypatch.setattr(detect, "reaper_feed_live", lambda: False)
    monkeypatch.setattr(detect, "cubase_bridge_live", lambda t=1.0: False)
    detect.detect_daw()
    assert detect.last_detect_fell_back is True
    monkeypatch.setattr(detect, "reaper_feed_live", lambda: True)
    detect.detect_daw()
    assert detect.last_detect_fell_back is False


def test_find_bridge_port_matches_case_insensitively():
    """ONE shared matcher for detection AND CubaseSysexSource.start: a
    hand-made "Roto-Bridge" once passed the (insensitive) probe and then
    failed the source's case-SENSITIVE open — they must never drift again."""
    from athens.daw import cubase_source
    assert detect.find_bridge_port(["IAC Driver roto-bridge"]) \
        == "IAC Driver roto-bridge"
    assert detect.find_bridge_port(["Roto-Bridge"]) == "Roto-Bridge"
    assert detect.find_bridge_port(["Bus 1", "loopMIDI Port"]) is None
    assert cubase_source.find_bridge_port is detect.find_bridge_port


def test_bridge_port_missing_enumerates_only(monkeypatch):
    """Missing-pair sensing lists port names and NEVER opens one (an open
    while the device port floods deadlocks CoreMIDI)."""
    import sys
    import types
    stub = types.SimpleNamespace(
        get_input_names=lambda: ["ROTO-Control"],
        get_output_names=lambda: ["ROTO-Control"],
        open_input=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not open a port")),
        open_output=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not open a port")))
    monkeypatch.setitem(sys.modules, "mido", stub)
    assert detect.bridge_port_missing() is True
    stub.get_input_names = lambda: ["IAC Driver roto-bridge"]
    stub.get_output_names = lambda: ["IAC Driver roto-bridge"]
    assert detect.bridge_port_missing() is False


def test_bridge_port_hint_is_platform_correct(monkeypatch):
    monkeypatch.setattr(detect.sys, "platform", "win32")
    hint = detect.bridge_port_hint()
    assert "loopMIDI" in hint and "Autostart" in hint     # autostart is essential:
    #                                       loopMIDI ports are per-user and exist
    #                                       only while it runs — without it the
    #                                       port (and Cubase detection) vanishes
    #                                       on reboot
    monkeypatch.setattr(detect.sys, "platform", "darwin")
    assert "IAC" in detect.bridge_port_hint()
