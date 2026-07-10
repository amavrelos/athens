"""Cubase adapter — offline. The MIDI Remote wire contract decodes into the
SysexDawSource model + callbacks, and control encodes back the same way. No
Cubase, no hardware: a loopback port drives both directions."""
from athens.daw import cubase_contract as wire
from athens.daw.cubase_source import CubaseSysexSource
from athens.daw.source import TransportAction
from athens.roto.sysex_client import LoopbackMidiPort


def make():
    port = LoopbackMidiPort()
    src = CubaseSysexSource(port=port)
    src.start()
    return src, port


def test_contract_roundtrips_and_ignores_foreign_frames():
    m = wire.parse(wire.volume(3, 0.5))
    assert m.cmd is wire.Cmd.VOLUME
    assert wire.parse_u14(m.payload) == 3
    assert abs(wire.norm(wire.parse_u14(m.payload, 2)) - 0.5) < 0.001
    assert wire.parse(bytes((0xF0, 0x7E, 0x01, 0xF7))) is None   # other sysex id
    assert wire.parse(bytes((0xB0, 1, 2))) is None               # a CC, not ours


def test_hello_version_fires_on_script_version():
    """The script rides its SCRIPT_VERSION in the HELLO token ("cubase <v>");
    the source surfaces it (once per distinct value) so the service can flag a
    stale, still-loaded script and prompt a Cubase restart."""
    src, port = make()
    seen = []
    src.on_script_version = seen.append
    port.inject(wire.hello("cubase 42"))
    assert seen == ["42"]
    assert src._daw_tag == "cubase"                       # identity still clean
    port.inject(wire.hello("cubase 42"))                  # same -> no re-fire
    assert seen == ["42"]
    port.inject(wire.hello("cubase 43"))                  # changed -> fires
    assert seen == ["42", "43"]


def test_hello_without_version_reports_older_build():
    """A pre-version script sends bare "cubase" — no version. The source must
    still fire (with "") so the service treats it as an older build, not silence."""
    src, port = make()
    seen = []
    src.on_script_version = seen.append
    port.inject(wire.hello("cubase"))
    assert seen == [""]                                    # "" = older/unversioned
    assert src._daw_tag == "cubase"


def test_bundled_versions_present_and_parseable():
    """Both companion scripts must carry a parseable SCRIPT_VERSION, or the
    loaded-vs-latest check silently no-ops (expected == None)."""
    from athens.daw import script_install
    assert script_install.bundled_version("cubase")
    assert script_install.bundled_version("reaper")


def test_hello_handshake_records_identity():
    """The script announces "cubase" (HELLO); the source records it so auto-
    detect and the UI know which DAW is on the wire."""
    src, port = make()
    m = wire.parse(wire.hello("cubase"))
    assert m.cmd is wire.Cmd.HELLO and m.direction == wire.DIR_TO_ATHENS
    assert bytes(m.payload).decode() == "cubase"
    port.inject(wire.hello("cubase"))
    assert src._daw_tag == "cubase"


def test_own_control_echo_ignored_but_host_state_applied():
    """A shared MIDI pair loops every send back. The source must ignore its own
    control echo (DIR_TO_CUBASE) and act only on host state (DIR_TO_ATHENS) —
    this is what stops the Cubase self-feedback storm."""
    src, port = make()
    port.inject(wire.count(2))
    vols = []
    src.on_track_volume = lambda i, v: vols.append((i, v))

    port.inject(wire.as_control(wire.volume(0, 0.9)))   # our own echo -> ignored
    assert vols == []
    port.inject(wire.volume(0, 0.9))                    # genuine host state
    assert len(vols) == 1 and abs(vols[0][1] - 0.9) < 0.01
    # and every outbound send is tagged control, so Cubase acts / we don't echo
    port.sent.clear()
    src.set_track_volume(1, 0.4)
    assert wire.parse(port.sent[0]).direction == wire.DIR_TO_CUBASE


def test_host_stream_fills_tracks_and_fires_callbacks():
    src, port = make()
    fired = {"tracks": 0, "sel": 0, "transport": 0, "vol": []}
    src.on_tracks_changed = lambda: fired.update(tracks=fired["tracks"] + 1)
    src.on_selected_track_changed = lambda: fired.update(sel=fired["sel"] + 1)
    src.on_transport_changed = lambda: fired.update(transport=fired["transport"] + 1)
    src.on_track_volume = lambda i, v: fired["vol"].append((i, v))

    port.inject(wire.count(3))
    port.inject(wire.name(0, "Kick"))
    port.inject(wire.name(1, "Snare"))
    port.inject(wire.volume(1, 0.75))
    port.inject(wire.flag(1, wire.Flag.MUTE, True))
    port.inject(wire.select(2))
    port.inject(wire.transport(playing=True, loop=True))

    tracks = src.tracks()
    assert len(tracks) == 3
    assert tracks[0].name == "Kick" and tracks[1].name == "Snare"
    assert abs(tracks[1].volume - 0.75) < 0.001
    assert tracks[1].muted is True
    assert src.selected_track() == 2
    assert src.transport().playing and src.transport().loop
    assert fired["sel"] == 1 and fired["transport"] == 1
    assert fired["vol"][-1][0] == 1


def test_pan_and_vu_fire_their_callbacks():
    src, port = make()
    pans, vus = [], []
    src.on_track_pan = lambda i, v: pans.append((i, v))
    src.on_track_vu = lambda i, left, right: vus.append((i, left, right))
    port.inject(wire.pan(1, 0.25))
    port.inject(wire.vu(3, 0.8))
    assert pans and pans[-1][0] == 1 and abs(pans[-1][1] - 0.25) < 0.01
    assert vus and vus[-1][0] == 3 and abs(vus[-1][1] - 0.8) < 0.01   # mono L=R
    assert abs(vus[-1][1] - vus[-1][2]) < 0.001


def test_value_before_name_creates_track_and_extends_count():
    src, port = make()
    port.inject(wire.volume(5, 0.3))     # arrives with no count/name yet
    assert len(src.tracks()) == 6        # count extended to fit index 5
    assert abs(src.tracks()[5].volume - 0.3) < 0.001


def test_control_is_sent_back_as_the_same_contract():
    src, port = make()
    port.inject(wire.count(4))
    port.sent.clear()
    src.set_track_volume(1, 0.5)
    src.set_selected_track(3)
    src.set_track_flag(1, "soloed", True)
    src.transport_action(TransportAction.PLAY, True)

    sent = [wire.parse(f) for f in port.sent]
    cmds = {m.cmd for m in sent if m}
    assert {wire.Cmd.VOLUME, wire.Cmd.SELECT, wire.Cmd.FLAG,
            wire.Cmd.TRANSPORT} <= cmds
    sel = next(m for m in sent if m and m.cmd is wire.Cmd.SELECT)
    assert wire.parse_u14(sel.payload) == 3      # selection round-trips


def test_plugin_stream_fills_devices_params_and_fires():
    src, port = make()
    focus_fired, param_events = [], []
    src.on_plugin_focus_changed = lambda: focus_fired.append(1)
    src.on_device_param_value = \
        lambda d, p, v, disp: param_events.append((d, p, v, disp))

    port.inject(wire.device_count(2))
    port.inject(wire.device_name(0, "EQ"))
    port.inject(wire.device_name(1, "Diva"))
    port.inject(wire.focus_device(1))
    port.inject(wire.param_count(3))
    port.inject(wire.param_name(0, "Cutoff"))
    port.inject(wire.param_value(0, 0.5))
    port.inject(wire.param_display(0, "2.4 kHz"))

    assert [d.name for d in src.devices()] == ["EQ", "Diva"]
    assert src.selected_device() == 1 and focus_fired == [1]
    params = src.device_params(1)                  # the focused device's page
    assert params[0].name == "Cutoff"
    assert abs(params[0].value - 0.5) < 0.001
    assert params[0].display == "2.4 kHz"
    assert src.device_params(0) == []              # non-focused -> empty page
    assert param_events[-1] == (1, 0, params[0].value, "2.4 kHz")


def test_focus_change_replaces_the_param_set():
    src, port = make()
    port.inject(wire.focus_device(0))
    port.inject(wire.param_count(1))
    port.inject(wire.param_name(0, "Old"))
    port.inject(wire.focus_device(1))              # focus a different plugin
    assert src.device_params(1) == []              # cleared for the new focus
    port.inject(wire.param_name(0, "New"))
    assert src.device_params(1)[0].name == "New"


def test_plugin_control_sends_contract():
    src, port = make()
    port.inject(wire.device_count(2))
    port.inject(wire.focus_device(0))
    port.sent.clear()
    src.set_device_param(0, 2, 0.25)
    src.set_selected_device(1)
    src.set_device_enabled(0, False)
    cmds = {m.cmd for m in (wire.parse(f) for f in port.sent) if m}
    assert {wire.Cmd.PARAM_VALUE, wire.Cmd.FOCUS_DEVICE,
            wire.Cmd.DEVICE_ENABLED} <= cmds


def test_page_sends_scroll_command_and_claims_it():
    src, port = make()
    port.sent.clear()
    assert src.page(1) is True                # source pages itself (not the bridge)
    m = wire.parse(port.sent[0])
    assert m.cmd is wire.Cmd.PAGE and m.direction == wire.DIR_TO_CUBASE


def test_who_probe_sends_hello():
    # the WHO probe (fired off a daemon timer after start, so it can't wedge
    # launch) is an empty HELLO so a running script replies + DIAGs
    src, port = make()
    port.sent.clear()
    src._probe_who()
    assert any(wire.parse(f).cmd is wire.Cmd.HELLO for f in port.sent)


def test_describe_decodes_contract_frames():
    assert wire.describe(wire.volume(0, 0.5)) == ("VOLUME trk1 0.50", "Track 1 volume")
    assert wire.describe(wire.hello())[0] == "HELLO (WHO?)"
    assert wire.describe(wire.hello("cubase")) == ("HELLO 'cubase'", "identity")
    assert wire.describe(wire.flag(0, wire.Flag.MUTE, True)) == \
        ("FLAG trk1 MUTE on", "Track 1 Mute")
    assert wire.describe(wire.pan(0, 0.28))[0].startswith("PAN trk1")
    assert wire.describe(b"\x01\x02")[1] == "not a bridge frame"


def test_check_alive_fires_edges_and_uses_ka_tag(monkeypatch):
    """Cubase quitting must blank the device: check_alive fires the on_daw_alive
    EDGES itself, and its keepalive WHO is tagged "ka" so the script answers
    lightly. The gone-edge is DEBOUNCED (grace zeroed here) — the first missed
    keepalive only arms it; a later still-silent poll blanks. A watcher that
    has NEVER heard Cubase reports False even inside the grace: the optimistic
    initial state otherwise made the auto-DAW monitor adopt a phantom Cubase
    at its first tick."""
    from athens.daw import cubase_source as cs
    monkeypatch.setattr(cs, "_GONE_GRACE_S", 0.0)
    src, port = make()
    alive = []
    src.on_daw_alive = alive.append
    port.sent.clear()

    assert src.check_alive(timeout=0.02) is False      # never heard Cubase: not
    assert alive == []                                 #  alive — but not BLANKED
    #                                                     either (edge debounced)
    kas = [wire.parse(f) for f in port.sent]
    assert any(m and m.cmd is wire.Cmd.HELLO and bytes(m.payload) == b"ka"
               for m in kas)                           # keepalive tagged "ka"

    assert src.check_alive(timeout=0.02) is False      # still silent -> gone
    assert alive == [False]
    assert src.check_alive(timeout=0.02) is False      # still gone: NO re-fire
    assert alive == [False]
    port.inject(wire.hello("cubase"))                  # traffic returns
    assert src.check_alive(timeout=0.02) is True       # fresh rx = fast path
    assert alive == [False, True]                      # returned-edge fired


def test_gap_slot_auto_focuses_first_named_device():
    """Insert-only track: slot 0 (the instrument) is an empty gap. The source
    must focus the first NAMED device — otherwise the bridge pushes nothing
    (the gap-slot bug: an empty-name focus early-returns forever)."""
    src, port = make()
    port.inject(wire.device_count(2))
    port.sent.clear()
    port.inject(wire.device_name(1, "TH8"))            # slot 0 stays unnamed
    assert src.selected_device() == 1                  # auto-focused the real one
    sent = [wire.parse(f) for f in port.sent]
    assert any(m and m.cmd is wire.Cmd.FOCUS_DEVICE for m in sent)
    # and a NAMED slot 0 (instrument track) is left alone
    port.inject(wire.device_name(0, "DUNE 3"))
    assert src.selected_device() == 1                  # no refocus churn


def test_focus_echo_regate():
    """A re-pulse of the CURRENT slot (Cubase page re-activation does this)
    must not clear params / refire focus — only an actual change does."""
    src, port = make()
    port.inject(wire.device_count(2))
    port.inject(wire.device_name(0, "EQ"))
    port.inject(wire.device_name(1, "Diva"))
    fired = []
    src.on_plugin_focus_changed = lambda: fired.append(1)
    port.inject(wire.focus_device(1))                  # 0 -> 1: a real change
    port.inject(wire.param_count(2))
    port.inject(wire.param_name(0, "Cutoff"))
    assert fired == [1]
    port.inject(wire.focus_device(1))                  # re-pulse of the same slot
    assert fired == [1]                                # gated: no refire
    assert src.device_params(1)                        # params NOT cleared


def _flush(src):
    """Force the debounced grab decision now, skipping the 200 ms quiet timer."""
    if src._grab_timer is not None:
        src._grab_timer.cancel()
    src._commit_learn_grab()


def test_learn_move_fires_touch_but_not_echo():
    """LEARN armed: a genuine, ISOLATED Cubase param move fires on_param_touched
    (the grab that becomes PLUGIN_PARAM_SWEEP); our own write bouncing back does
    NOT, and a move while disarmed does NOT."""
    src, port = make()
    touched = []
    src.on_param_touched = lambda fx, p: touched.append((fx, p))

    port.inject(wire.param_value(3, 0.40))          # learn OFF -> no grab
    _flush(src)
    assert touched == []

    src.set_learn_armed(True)
    src.set_device_param(0, 3, 0.70)                # our own write...
    port.inject(wire.param_value(3, 0.70))          # ...its echo is NOT a grab
    _flush(src)
    assert touched == []

    port.inject(wire.param_value(3, 0.55))          # a real, isolated user move
    _flush(src)
    assert touched == [(0, 3)]

    src.set_learn_armed(False)
    port.inject(wire.param_value(3, 0.90))          # disarmed -> no grab
    _flush(src)
    assert touched == [(0, 3)]


def test_bank_dump_is_not_a_grab():
    """Plugin focus DUMPS all 8 bank params at once. That must NOT read as eight
    grabs — the bug that swept param 0 ('Bypass') every time a plugin loaded."""
    src, port = make()
    touched = []
    src.on_param_touched = lambda fx, p: touched.append((fx, p))
    src.set_learn_armed(True)

    for slot in range(8):                           # the focus dump, all at once
        port.inject(wire.param_value(slot, 0.5))
    _flush(src)

    assert touched == []                            # a dump is not a grab


def test_ambiguous_multi_move_is_not_a_grab():
    """Two params changing together in one window is ambiguous — grab neither."""
    src, port = make()
    touched = []
    src.on_param_touched = lambda fx, p: touched.append((fx, p))
    src.set_learn_armed(True)

    port.inject(wire.param_value(2, 0.3))
    port.inject(wire.param_value(5, 0.9))
    _flush(src)

    assert touched == []


def test_gone_edge_is_debounced(monkeypatch):
    """A brief Cubase silence (script re-bind) must NOT blank the device; only
    sustained silence past the grace does. Time-based so the two pollers that
    call check_alive can't race it."""
    from athens.daw import cubase_source as cs
    src = cs.CubaseSysexSource()                 # no port; probe is stubbed
    edges = []
    src.on_daw_alive = lambda alive: edges.append(alive)
    clock = {"t": 100.0}
    monkeypatch.setattr(cs.time, "monotonic", lambda: clock["t"])
    probe = {"ok": True}
    monkeypatch.setattr(src, "_check_alive", lambda timeout=0.6: probe["ok"])

    src.check_alive()
    assert edges == []                           # answering -> no edge

    probe["ok"] = False                          # a brief silence begins...
    src.check_alive()                            # 1st miss -> start the clock
    clock["t"] += 3.0
    src.check_alive()
    assert edges == []                           # 3s < 6s grace -> NOT blanked

    probe["ok"] = True                           # ...Cubase returns in time
    src.check_alive()
    assert edges == []                           # ridden out, no blank at all

    probe["ok"] = False                          # now a SUSTAINED silence
    src.check_alive()                            # 1st miss
    clock["t"] += 7.0
    src.check_alive()
    assert edges == [False]                      # 7s >= 6s grace -> gone

    probe["ok"] = True
    src.check_alive()
    assert edges == [False, True]                # recovery -> answering again
