"""Golden-byte tests for the native DAW SysEx codec.

Pinned against the format reverse-read from ROTO-SETUP's Ableton Remote Script.
"""
from athens.sysex import codec
from athens.sysex.constants import METER_LEVEL_RED, METER_LEVEL_YELLOW


def h(s: str) -> bytes:
    return bytes.fromhex(s)


HDR = "F0 00 22 03 02"   # start + manufacturer + device id


def test_daw_started():
    assert codec.daw_started() == h(f"{HDR} 0A 01 F7")


def test_daw_ping_response_logic():
    # we announce daw_id = LOGIC_PRO (3) so the device runs the Logic dialect
    assert codec.daw_ping_response() == h(f"{HDR} 0A 03 03 F7")


def test_num_tracks_is_14bit():
    # 200 = 0xC8 -> msb 0x01, lsb 0x48
    assert codec.num_tracks(200) == h(f"{HDR} 0A 04 01 48 F7")


def test_first_track():
    assert codec.first_track(0) == h(f"{HDR} 0A 05 00 00 F7")


def test_track_details_layout():
    frame = codec.track_details(3, "Bass", colour=5, foldable=False)
    assert frame[:7] == h(f"{HDR} 0A 07")
    body = frame[7:-1]
    assert body[0:2] == h("00 03")                       # u14 index
    assert body[2:15] == b"Bass" + b"\x00" * 9           # 13-byte name
    assert body[15] == 5                                  # colour
    assert body[16] == 0                                  # foldable
    assert frame[-1] == 0xF7


def test_track_details_end():
    assert codec.track_details_end() == h(f"{HDR} 0A 08 F7")


def test_transport_status_play():
    assert codec.transport_status(playing=True) == \
        h(f"{HDR} 0A 0B 01 00 00 00 00 00 00 00 F7")


def test_transport_status_record_and_loop():
    f = codec.transport_status(recording=True, loop=True)
    assert f == h(f"{HDR} 0A 0B 00 00 01 00 01 00 00 00 F7")


def test_param_value_formatted_string():
    frame = codec.param_value(0, "1.2 kHz")
    assert frame[:7] == h(f"{HDR} 0A 18")
    assert frame[7] == 0                                  # button flag
    assert frame[8] == 0                                  # index
    assert frame[9:22] == b"1.2 kHz" + b"\x00" * 6        # 13-byte value string
    assert frame[-1] == 0xF7


def test_param_value_button_flag():
    assert codec.param_value(2, "On", is_button=True)[7:9] == h("01 02")


def test_vu_meter_points():
    # Logic's thresholds from the connect preamble (logic-mix capture)
    assert codec.vu_meter_points(METER_LEVEL_YELLOW, METER_LEVEL_RED) == \
        h(f"{HDR} 0C 0B 6A 78 F7")


def test_daw_select_track_is_mixer_group():
    frame = codec.daw_select_track(1, "Kick", colour=7)
    assert frame[5:7] == h("0C 04")                       # MIXER, DAW_SELECT_TRACK


# --- parsing device -> DAW -------------------------------------------------

def test_parse_sysex_roundtrip():
    msg = codec.parse_sysex(codec.track_details(5, "Vox", 3))
    assert msg.group == 0x0A and msg.command == 0x07
    assert codec.parse_u14(msg.data) == 5


def test_decode_select_track_14bit():
    # device asks DAW to select track 300 (0x12C) -> msb 0x02 lsb 0x2C
    frame = h(f"{HDR} 0A 09 02 2C F7")
    msg = codec.parse_sysex(frame)
    assert (msg.group, msg.command) == (0x0A, 0x09)
    assert codec.decode_select_track(msg.data) == 300


def test_parse_rejects_foreign_sysex():
    try:
        codec.parse_sysex(h("F0 7E 00 06 01 F7"))   # a universal identity reply
    except codec.ProtocolError:
        return
    assert False, "should reject non-ROTO sysex"


def test_encode_string_replaces_non_ascii_instead_of_bit_folding():
    # bit-folding & 0x7F would turn 'Bär' into 'Bdr' (0xE4 & 0x7F == 'd')
    assert codec.encode_string("Bär")[:3] == b"B?r"
    # and U+0100 (multiple of 128) must not become an embedded NUL
    assert b"\x00" not in codec.encode_string("AĀB").rstrip(b"\x00")


# --- PLUGIN (VST-follow) ---------------------------------------------------

def test_hashes_are_deterministic_and_7bit():
    import hashlib
    assert codec.param_hash("Cutoff") == \
        bytes(b & 0x7F for b in hashlib.sha1(b"Cutoff").digest()[:6])
    dh = codec.device_hash("EQ Eight")
    assert len(dh) == 8 and all(b < 0x80 for b in dh)
    assert len(codec.param_hash("x")) == 6


def test_plugin_details_layout():
    frame = codec.plugin_details(0, "EQ Eight", codec.device_hash("EQ Eight"))
    assert frame[5:7] == h("0B 05")                  # PLUGIN / PLUGIN_DETAILS
    body = frame[7:-1]
    assert body[0] == 0                              # device index
    assert len(body[1:9]) == 8                       # hash
    assert body[9] == 1                              # enabled
    assert body[10:23] == b"EQ Eight" + b"\x00" * 5  # name[13]
    assert body[23] == 0 and body[24] == 0           # rack kind, macro pages


def test_learn_param_layout_and_value():
    frame = codec.learn_param(5, "Cutoff", 1.0, codec.param_hash("Cutoff"))
    assert frame[5:7] == h("0B 0A")                  # PLUGIN / LEARN_PARAM
    body = frame[7:-1]
    assert body[0:2] == h("00 05")                   # u14 param index
    assert len(body[2:8]) == 6                        # param hash
    assert body[8] == 0                              # macro flag
    assert body[9] == 0                              # haptic (centre indent)
    assert body[10] == 0                             # quantised steps
    assert body[11:13] == h("7F 7F")                 # value 1.0 -> 0x3FFF
    assert body[13:26] == b"Cutoff" + b"\x00" * 7    # name[13]


def test_set_mapped_control_name_layout():
    frame = codec.set_mapped_control_name(2, "Res", codec.param_hash("Res"))
    assert frame[5:7] == h("0B 0F")                  # PLUGIN / SET_MAPPED_CTL_NAME
    assert frame[7:9] == h("00 02")                  # u14 index
    assert frame[9:15] == codec.param_hash("Res")    # 6-byte hash


def test_decode_control_mapped():
    # device maps knob 4 to param 300 (macro), hash 010203040506
    data = h("02 2C 01 02 03 04 05 06 00 04 01")
    cm = codec.decode_control_mapped(data)
    assert cm.param_index == 300
    assert cm.param_hash == h("01 02 03 04 05 06")
    assert cm.control_kind == 0 and cm.control_index == 4
    assert cm.is_macro is True
