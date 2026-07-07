"""Bring-up harness: full scripted session against fakes + pure analysers."""
import json
import tempfile
from pathlib import Path

from athens.bringup import (
    ScriptedUI, analyse_buttons, analyse_encoders, analyse_touch, run_bringup,
)
from athens.roto.sysex_client import LoopbackMidiPort

from tests.test_backup import make_fake_device


# --- analysers ----------------------------------------------------------------

def test_analyse_encoders_pairs_msb_with_plus32():
    events = [(12, 64), (44, 3), (13, 50), (45, 9)]
    a = analyse_encoders(events)
    assert a["msb_ccs"] == [12, 13]
    assert a["paired_lsb"] == {12: 44, 13: 45}
    assert a["is_14bit"] is True and a["nrpn_seen"] is False


def test_analyse_encoders_detects_partial_pairing_and_nrpn():
    a = analyse_encoders([(12, 64), (13, 50), (45, 9), (99, 1), (98, 44), (6, 7)])
    assert a["paired_lsb"] == {13: 45} and a["is_14bit"] is False
    assert a["nrpn_seen"] is True
    assert 99 in a["other_ccs"]


def test_analyse_touch_and_buttons():
    t = analyse_touch([(52, 127), (54, 0), (40, 1)])
    assert t["touch_knobs"] == [0, 2] and t["stray_ccs"] == [40]
    b = analyse_buttons([(102, 127), (102, 0), (110, 127)], [(60, 100)])
    assert b["button_ccs"] == [102, 110] and b["button_notes"] == [60]


# --- full scripted session ------------------------------------------------------

def _scripted_run(tmp):
    port = LoopbackMidiPort()

    def cc(ccnum, val):
        port.inject(bytes((0xBF, ccnum, val)))

    ui = ScriptedUI(actions=[
        # handshake prompt: device pings, we auto-answer, device connects
        lambda: (port.inject(bytes.fromhex("F000220302 0A02 F7".replace(" ", ""))),
                 port.inject(bytes.fromhex("F000220302 0A0C F7".replace(" ", "")))),
        # knobs prompt: two encoders, one with a 14-bit LSB pair
        lambda: (cc(12, 64), cc(44, 3), cc(13, 50)),
        # touch prompt
        lambda: (cc(52, 127), cc(53, 127)),
        # buttons prompt: one CC button + one note button
        lambda: (cc(102, 127), port.inject(bytes((0x9F, 60, 100)))),
    ])
    report = run_bringup(roto=make_fake_device(), port=port, ui=ui,
                         out_dir=str(tmp), stamp="test")
    return port, ui, report


def test_full_session_all_steps_pass_and_report_saved():
    with tempfile.TemporaryDirectory() as d:
        port, ui, report = _scripted_run(Path(d))
        by_name = {r.name: r for r in report.results}
        assert [r.name for r in report.results] == \
            ["serial", "backup", "handshake", "knobs", "touch", "buttons"]
        assert all(r.ok for r in report.results), by_name

        # serial + backup facts
        assert "2.0" in by_name["serial"].details["firmware"] or \
            by_name["serial"].details["firmware"]
        assert by_name["backup"].details["setups"] == 1
        assert by_name["backup"].details["plugins"] == 1
        backup_file = Path(by_name["backup"].details["file"])
        assert backup_file.exists()
        data = json.loads(backup_file.read_text())
        assert data["setups"]["0"]["name"] == "Live Set" if "0" in data["setups"] \
            else data["setups"][0]["name"] == "Live Set"

        # handshake: ping seen, masquerade auto-answered, connected
        hs = by_name["handshake"].details
        assert hs["ping_seen"] and hs["connected_seen"]
        assert hs["daw_id_masquerade"] == "accepted"
        # the client actually sent DAW_PING_RESP with daw_id=3 (Logic Pro)
        assert any(f[:8] == bytes.fromhex("F000220302 0A03 03".replace(" ", ""))
                   for f in port.sent)

        # probes
        assert by_name["knobs"].details["paired_lsb"] == {12: 44}
        assert by_name["touch"].details["touch_knobs"] == [0, 1]
        assert by_name["buttons"].details["button_ccs"] == [102]
        assert by_name["buttons"].details["button_notes"] == [60]

        # report file
        assert (Path(d) / "bringup-test.json").exists()


def test_session_without_ports_still_saves_report():
    with tempfile.TemporaryDirectory() as d:
        report = run_bringup(roto=None, port=None, ui=ScriptedUI(),
                             out_dir=d, stamp="empty")
        assert len(report.results) == 6
        assert not any(r.ok for r in report.results)
        assert (Path(d) / "bringup-empty.json").exists()
