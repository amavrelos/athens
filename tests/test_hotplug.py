"""ROTO hot-plug: attach when the device (re)appears, at launch or later.

Starting Athens and THEN plugging the ROTO in must connect on its own — the old
one-shot auto-connect gave up ("waiting for manual") and never noticed. A manual
disconnect, though, must stay down until the user connects again; an involuntary
unplug must NOT (replug should reconnect).
"""
from athens.api.service import BridgeService
from athens.daw.source import MockSysexSource


class _FakeBridge:
    def stop(self):
        pass


def _svc(monkeypatch):
    svc = BridgeService(source=MockSysexSource(), daw="reaper")
    # never open a real MIDI port in the attach path
    monkeypatch.setattr("athens.roto.sysex_client.MidoMidiPort",
                        lambda *a, **k: object())
    svc._attached = 0

    def fake_attach(_port):
        svc._attached += 1
        svc.bridge = _FakeBridge()      # attach makes bridge non-None
    monkeypatch.setattr(svc, "_attach_midi", fake_attach)

    def fake_serial(_cands):
        svc.roto = object()             # faithful: a real attach sets self.roto
        return "1.0"
    monkeypatch.setattr(svc, "_attach_best_serial", fake_serial)
    return svc


def test_hotplug_attaches_when_roto_appears(monkeypatch):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(svc, "_roto_midi_present", lambda: True)

    assert svc._auto_connect_tick() is True     # ROTO appeared -> attach
    assert svc._attached == 1
    assert svc._auto_connect_tick() is False    # already attached -> no re-attach
    assert svc._attached == 1


def test_hotplug_retries_serial_when_it_lags(monkeypatch):
    """USB-MIDI can enumerate before the CDC serial node exists: MIDI attaches,
    serial misses. Later ticks must RETRY serial — it used to be one-shot, so
    device maps/learn stayed dead for the whole session."""
    svc = _svc(monkeypatch)
    monkeypatch.setattr(svc, "_roto_midi_present", lambda: True)
    calls = []

    def flaky_serial(_cands):
        calls.append(1)
        if len(calls) == 1:
            return None                 # CDC node not up yet
        svc.roto = object()
        return "1.0"
    monkeypatch.setattr(svc, "_attach_best_serial", flaky_serial)

    assert svc._auto_connect_tick() is True      # MIDI attached, serial missed
    assert svc.roto is None
    assert svc._auto_connect_tick() is True      # retry: serial lands
    assert svc.roto is not None
    assert svc._auto_connect_tick() is False     # fully attached -> idle
    assert svc._attached == 1                    # MIDI never re-attached


def test_hotplug_no_attach_without_roto(monkeypatch):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(svc, "_roto_midi_present", lambda: False)

    assert svc._auto_connect_tick() is False
    assert svc._attached == 0


def test_hotplug_respects_manual_disconnect(monkeypatch):
    svc = _svc(monkeypatch)
    monkeypatch.setattr(svc, "_roto_midi_present", lambda: True)
    svc._user_disconnected = True               # user clicked disconnect

    assert svc._auto_connect_tick() is False    # must NOT fight the user
    assert svc._attached == 0


def test_involuntary_unplug_allows_reattach(monkeypatch):
    svc = _svc(monkeypatch)
    svc.bridge = _FakeBridge()
    svc._connected = True

    svc._detach()                               # involuntary (serial-lost) detach

    assert svc._user_disconnected is False      # NOT a manual disconnect...
    monkeypatch.setattr(svc, "_roto_midi_present", lambda: True)
    assert svc._auto_connect_tick() is True     # ...so replug reconnects
    assert svc._attached == 1
