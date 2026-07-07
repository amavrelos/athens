"""Golden-byte tests for the serializer -- no hardware required.

These pin the exact wire bytes against the ROTO-CONTROL Serial API v1.2 spec, so
a regression in the codec fails loudly.
"""
from athens.protocol import codec
from athens.protocol.constants import (
    ControlMode, ControlType, KnobHaptic, Mode,
)


def h(s: str) -> bytes:
    return bytes.fromhex(s)


# --- GENERAL ---------------------------------------------------------------

def test_get_fw_version():
    assert codec.get_fw_version() == h("5A 01 01 00 00")


def test_get_mode():
    assert codec.get_mode() == h("5A 01 02 00 00")


def test_set_mode_mix_page1():
    assert codec.set_mode(Mode.MIX, page=1) == h("5A 01 03 00 02 02 00")


def test_set_mode_plugin_page2_uses_stride_of_8():
    assert codec.set_mode(Mode.PLUGIN, page=2) == h("5A 01 03 00 02 01 08")


def test_config_update_frames():
    assert codec.start_config_update() == h("5A 01 04 00 00")
    assert codec.end_config_update() == h("5A 01 05 00 00")


# --- MIDI ------------------------------------------------------------------

def test_set_setup():
    assert codec.set_setup(0x00) == h("5A 02 03 00 01 00")


def test_clear_midi_setup():
    assert codec.clear_midi_setup(0x01) == h("5A 02 0A 00 01 01")


def test_clear_control_config_knob():
    assert codec.clear_control_config(0x00, ControlType.KNOB, 0x03) == \
        h("5A 02 09 00 03 00 00 03")


def test_set_setup_name_padded_to_13():
    frame = codec.set_setup_name(0x00, "REAPER")
    assert frame[:5] == h("5A 02 04 00 0E")   # CL = 0x000E = 1 + 13
    assert frame[5] == 0x00                    # SI
    assert frame[6:] == b"REAPER" + b"\x00" * 7


def test_set_knob_control_config_length_and_layout():
    frame = codec.set_knob_control_config(codec.KnobConfig(
        setup_index=0, control_index=0, control_mode=ControlMode.CC_7BIT,
        channel=1, param=0x0A, min_value=0, max_value=0x7F, name="Cutoff",
        colour=5, haptic=KnobHaptic.KNOB_300,
    ))
    # header + CL: base payload is 0x1D (29) bytes, no step names
    assert frame[:5] == h("5A 02 07 00 1D")
    payload = frame[5:]
    assert len(payload) == 0x1D
    assert payload[0:5] == bytes((0x00, 0x00, 0x00, 0x01, 0x0A))  # SI CI CM CC CP
    assert payload[5:7] == h("00 00")   # NRPN addr
    assert payload[7:9] == h("00 00")   # min
    assert payload[9:11] == h("00 7F")  # max (big-endian)
    assert payload[11:24] == b"Cutoff" + b"\x00" * 7  # CN (13)
    assert payload[24] == 5             # colour
    assert payload[25] == 0x00          # haptic KNOB_300
    assert payload[26:28] == h("FF FF") # indents unused
    assert payload[28] == 0x00          # steps


def test_knob_config_with_steps_grows_by_13_each():
    cfg = codec.KnobConfig(setup_index=0, control_index=1, steps=3,
                           haptic=KnobHaptic.KNOB_N_STEP,
                           step_names=["Lo", "Mid", "Hi"])
    frame = codec.set_knob_control_config(cfg)
    expected_len = 0x1D + 3 * 0x0D
    assert (frame[3] << 8 | frame[4]) == expected_len
    assert len(frame) == 5 + expected_len


# --- responses / async -----------------------------------------------------

def test_decode_fw_version():
    data = bytes((1, 2, 3)) + b"abc1234"
    fw = codec.decode_fw_version(data)
    assert (fw.major, fw.minor, fw.patch) == (1, 2, 3)
    assert fw.git_commit == "abc1234"
    assert str(fw) == "1.2.3+abc1234"


def test_decode_mode_page_stride():
    assert codec.decode_mode(h("02 08")).mode == Mode.MIX
    assert codec.decode_mode(h("02 08")).page == 2


def test_parse_response_ok_and_error():
    ok = codec.parse_response(h("A5 00 01 02 03"))
    assert ok.ok and ok.data == h("01 02 03")
    err = codec.parse_response(h("A5 7F"))
    assert not err.ok


def test_parse_async_control_learned():
    ev = codec.parse_async(h("5A 02 0B 00 03 00 00 05"))
    assert (ev.cmd_type, ev.subtype) == (0x02, 0x0B)
    assert ev.data == h("00 00 05")


def test_parse_async_rejects_truncated_frame():
    try:
        codec.parse_async(h("5A 02 0B 00 03 00 00"))   # declares 3, carries 2
    except codec.ProtocolError:
        return
    assert False, "truncated async frame must raise"
