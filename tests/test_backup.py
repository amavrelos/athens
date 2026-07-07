"""Backup path: config GET decoders, UNCONFIGURED handling, plugin
enumeration, and the device-dump -> library round trip — against a fake
device built on the callable-canned LoopbackTransport."""
from athens import backup
from athens.library import SetupLibrary
from athens.protocol import codec
from athens.protocol.constants import (
    GET_STEP_NAMES, NAME_LEN, CmdType, ControlMode, General, KnobHaptic, Midi,
    Plugin, RespCode, SwitchHaptic,
)
from athens.roto.client import RotoControl
from athens.roto.transport import LoopbackTransport

UNCONF = codec.Response(code=int(RespCode.UNCONFIGURED), data=b"")
SN_PAD = bytes(GET_STEP_NAMES * NAME_LEN)


def knob_get_payload(si, ci, name, param, colour=3, steps=0):
    """Build the fixed 237-byte GET KNOB CONTROL CONFIG data blob."""
    return (bytes((si, ci, int(ControlMode.CC_7BIT), 1, param)) + bytes(2)
            + bytes((0, 0, 0, 0x7F)) + codec.encode_name(name)
            + bytes((colour, int(KnobHaptic.KNOB_300), 0xFF, 0xFF, steps))
            + SN_PAD)


def switch_get_payload(si, ci, name, param):
    return (bytes((si, ci, int(ControlMode.CC_7BIT), 1, param)) + bytes(2)
            + bytes((0, 0, 0, 0x7F)) + codec.encode_name(name)
            + bytes((5, 14, 0, int(SwitchHaptic.TOGGLE), 0)) + SN_PAD)


def make_fake_device():
    """Setup 0: 2 knobs + 1 switch; one stored plugin with one mapped knob."""
    plugin_hash = codec.get_plugin(b"\x01" * 8)[5:13]  # any 8 bytes; reuse

    def knob(frame):
        si, ci = frame[5], frame[6]
        if si == 0 and ci == 0:
            return knob_get_payload(0, 0, "Cutoff", 74)
        if si == 0 and ci == 1:
            return knob_get_payload(0, 1, "Reso", 71, colour=7)
        return UNCONF

    def switch(frame):
        si, ci = frame[5], frame[6]
        if si == 0 and ci == 3:
            return switch_get_payload(0, 3, "Mute", 102)
        return UNCONF

    plugins_left = [b"\x01" * 8]

    def first_plugin(frame):
        return b"\x01" * 8 + codec.encode_name("EQ Eight") + bytes((4,))

    def next_plugin(frame):
        return codec.Response(code=int(RespCode.NO_PLUGIN), data=b"")

    def plugin_knob(frame):
        ci = frame[13]
        if ci == 2:
            return (b"\x01" * 8 + bytes((2,)) + bytes((0, 5))
                    + b"\x0A" * 6 + bytes((0,)) + bytes((0, 0, 0x3F, 0x7F))
                    + codec.encode_name("Freq A")
                    + bytes((9, 0, 0xFF, 0xFF, 0)) + SN_PAD)
        return UNCONF

    canned = {
        (CmdType.GENERAL, General.GET_FW_VERSION): bytes((2, 0, 1)) + b"abc1234",
        (CmdType.GENERAL, General.GET_MODE): bytes((0x00, 0x00)),
        (CmdType.MIDI, Midi.GET_CURRENT_SETUP):
            bytes((0,)) + codec.encode_name("Live Set"),
        (CmdType.MIDI, Midi.GET_SETUP):
            lambda f: bytes((f[5],)) + codec.encode_name("Live Set" if f[5] == 0 else ""),
        (CmdType.MIDI, Midi.GET_KNOB_CONTROL_CONFIG): knob,
        (CmdType.MIDI, Midi.GET_SWITCH_CONTROL_CONFIG): switch,
        (CmdType.PLUGIN, Plugin.GET_FIRST_PLUGIN): first_plugin,
        (CmdType.PLUGIN, Plugin.GET_NEXT_PLUGIN): next_plugin,
        (CmdType.PLUGIN, Plugin.GET_PLUGIN_KNOB_CONFIG): plugin_knob,
        (CmdType.PLUGIN, Plugin.GET_PLUGIN_SWITCH_CONFIG): lambda f: UNCONF,
    }
    return RotoControl(LoopbackTransport(canned))


# --- decoders ----------------------------------------------------------------

def test_decode_knob_config_round_trips_fields():
    cfg = codec.decode_knob_config(knob_get_payload(0, 1, "Reso", 71, colour=7))
    assert cfg.control_index == 1 and cfg.name == "Reso"
    assert cfg.param == 71 and cfg.colour == 7 and cfg.max_value == 0x7F
    assert cfg.step_names == []                    # steps=0 -> no names kept


def test_decode_switch_config_reads_leds_and_toggle():
    cfg = codec.decode_switch_config(switch_get_payload(0, 3, "Mute", 102))
    assert cfg.name == "Mute" and cfg.param == 102
    assert cfg.colour == 5 and cfg.led_on == 14
    assert cfg.haptic == SwitchHaptic.TOGGLE


# --- reader methods -----------------------------------------------------------

def test_unconfigured_slot_reads_as_none_not_error():
    roto = make_fake_device()
    assert roto.read_knob_config(0, 9) is None
    assert roto.read_knob_config(0, 0).name == "Cutoff"


def test_plugin_enumeration_ends_cleanly_on_no_plugin():
    roto = make_fake_device()
    plugins = list(roto.iter_plugins())
    assert len(plugins) == 1
    assert plugins[0].name == "EQ Eight" and plugins[0].plugin_type == 4


# --- dump + snapshot ------------------------------------------------------------

def test_dump_setup_collects_only_configured_slots():
    roto = make_fake_device()
    s = backup.dump_setup(roto, 0)
    assert s["name"] == "Live Set"
    assert set(s["knobs"]) == {"0", "1"} and set(s["switches"]) == {"3"}
    assert s["knobs"]["0"] == {"name": "Cutoff", "mode": "CC7", "channel": 1,
                               "param": 74, "nrpn": 0, "min": 0, "max": 127,
                               "colour": 3, "haptic": "KNOB", "steps": 0,
                               "indent1": 0xFF, "indent2": 0xFF,
                               "step_names": []}
    assert s["switches"]["3"]["toggle"] is True


def test_dump_setups_skips_empty_and_reports_progress():
    roto = make_fake_device()
    ticks = []
    dumped = backup.dump_setups(roto, indices=range(3),
                                progress=lambda st, d, t: ticks.append((d, t)))
    assert list(dumped) == [0]                     # setups 1,2 fully empty
    assert ticks == [(1, 3), (2, 3), (3, 3)]


def test_dump_plugins_reads_mapped_controls():
    roto = make_fake_device()
    plugins = backup.dump_plugins(roto)
    assert len(plugins) == 1
    p = plugins[0]
    assert p["name"] == "EQ Eight" and p["hash"] == "01" * 8
    assert p["knobs"]["2"]["name"] == "Freq A"
    assert p["knobs"]["2"]["param_index"] == 5
    assert p["switches"] == {}


# --- restore (library -> device) ----------------------------------------------

WIPED = {  # every config GET answers "unconfigured"; setup names empty
    (CmdType.MIDI, Midi.GET_SETUP): lambda f: bytes((f[5],)) + codec.encode_name(""),
    (CmdType.MIDI, Midi.GET_KNOB_CONTROL_CONFIG): lambda f: UNCONF,
    (CmdType.MIDI, Midi.GET_SWITCH_CONTROL_CONFIG): lambda f: UNCONF,
}

SETUP_LIB = {"name": "Live Set",
             "knobs": {"0": {"name": "Cutoff", "param": 74, "colour": 3},
                       "1": {"name": "Reso", "param": 71, "colour": 7}},
             "switches": {"3": {"name": "Mute", "param": 102, "colour": 5,
                                "led_on": 14}}}


def frames_of(transport, type_sub):
    return [f for f in transport.sent if f[1:3] == bytes(type_sub)]


def test_restore_after_wipe_writes_only_configured_slots():
    transport = LoopbackTransport(dict(WIPED))
    roto = RotoControl(transport)
    result = backup.restore_setup(roto, 0, SETUP_LIB)
    assert result == {"written": 3, "cleared": 0, "skipped": 61}
    assert len(frames_of(transport, (0x02, 0x07))) == 2   # knob writes
    assert len(frames_of(transport, (0x02, 0x08))) == 1   # switch write
    assert len(frames_of(transport, (0x02, 0x04))) == 1   # setup name
    assert len(frames_of(transport, (0x01, 0x04))) == 1   # ONE config session
    assert len(frames_of(transport, (0x01, 0x05))) == 1


def test_restore_in_sync_setup_writes_nothing():
    # the fake device already holds exactly what the library wants
    roto = make_fake_device()
    transport = roto._t
    dumped = backup.dump_setup(roto, 0)
    sent_before = len(transport.sent)
    result = backup.restore_setup(roto, 0, dumped)
    assert result["written"] == 0 and result["cleared"] == 0
    # only GETs after that point — no config session was opened
    assert not frames_of(transport, (0x01, 0x04))[sent_before:]
    assert len(frames_of(transport, (0x02, 0x07))) == 0


def test_restore_diff_writes_exactly_the_changed_slot():
    roto = make_fake_device()
    transport = roto._t
    setup = backup.dump_setup(roto, 0)
    setup["knobs"]["1"]["name"] = "Res 2"          # change one field
    result = backup.restore_setup(roto, 0, setup)
    assert result == {"written": 1, "cleared": 0, "skipped": 63}
    writes = frames_of(transport, (0x02, 0x07))
    assert len(writes) == 1 and b"Res 2" in writes[0]


def test_restore_clears_slot_absent_from_library():
    roto = make_fake_device()
    transport = roto._t
    setup = backup.dump_setup(roto, 0)
    del setup["knobs"]["1"]                        # library says: slot empty
    result = backup.restore_setup(roto, 0, setup)
    assert result["cleared"] == 1 and result["written"] == 0
    clears = frames_of(transport, (0x02, 0x09))
    assert len(clears) == 1 and clears[0][6] == 0 and clears[0][7] == 1


def test_restore_plugin_recreates_map():
    transport = LoopbackTransport()
    roto = RotoControl(transport)
    plugin = {"hash": "0a" * 8, "name": "EQ Eight",
              "knobs": {"2": {"name": "Freq A", "param_index": 5,
                              "param_hash": "0b" * 6, "max": 0x3FFF}},
              "switches": {"0": {"name": "Bypass", "param_index": 1,
                                 "param_hash": "0c" * 6, "led_on": 14}}}
    result = backup.restore_plugin(roto, plugin)
    assert result["written"] == 2
    assert len(frames_of(transport, (0x03, 0x06))) == 1   # ADD_PLUGIN
    knob_writes = frames_of(transport, (0x03, 0x0B))
    assert len(knob_writes) == 1 and b"Freq A" in knob_writes[0]
    switch_writes = frames_of(transport, (0x03, 0x0C))
    assert len(switch_writes) == 1 and b"Bypass" in switch_writes[0]
    # official layout: base 0x25 payload for a switch with no step names
    assert (switch_writes[0][3] << 8 | switch_writes[0][4]) == 0x25


def test_plugin_switch_config_golden_layout():
    cfg = codec.PluginSwitchConfig(plugin_hash=b"\x01" * 8, control_index=3,
                                   mapped_param_index=5,
                                   mapped_param_hash=b"\x02" * 6,
                                   name="Bypass", led_on=14)
    p = cfg.payload()
    assert len(p) == 0x25
    assert p[0:8] == b"\x01" * 8 and p[8] == 3
    assert (p[9] << 8 | p[10]) == 5 and p[11:17] == b"\x02" * 6
    assert p[17] == 0 and p[18] == 0x7F           # single-byte min/max
    assert p[19:32] == b"Bypass" + b"\x00" * 7
    assert p[33] == 14                             # LED on colour


def test_snapshot_lands_in_library_in_sync():
    roto = make_fake_device()
    lib = SetupLibrary()
    n = backup.snapshot_into_library(backup.dump_setups(roto, range(2)), lib)
    assert n == 1
    s = lib.get(0)
    assert s["deployed"] is True and s["dirty"] is False
    assert s["knobs"]["0"]["name"] == "Cutoff"
    listed = lib.list()[0]
    assert listed["name"] == "Live Set" and listed["deployed"]
