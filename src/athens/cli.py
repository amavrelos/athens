"""Command-line entry points.

    roto-reaper list-ports              # find the ROTO serial + MIDI ports
    roto-reaper handshake --port PORT   # Phase 0: prove the serial link
    roto-reaper mock-run                # run the serial bridge with no hardware
    roto-reaper run --port PORT [--no-midi]  # live bridge (MIDI on by default)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time


def _cmd_list_ports(_args) -> int:
    try:
        from serial.tools import list_ports
        print("Serial ports:")
        for p in list_ports.comports():
            print(f"  {p.device:20} {p.description}")
    except ImportError:
        print("pyserial not installed", file=sys.stderr)
    try:
        from .roto.midi import list_ports as midi_ports
        ins, outs = midi_ports()
        print("MIDI inputs: ", ", ".join(ins) or "(none)")
        print("MIDI outputs:", ", ".join(outs) or "(none)")
    except Exception as exc:  # noqa: BLE001 - informational only
        print(f"MIDI: unavailable ({exc})")
    return 0


def _cmd_handshake(args) -> int:
    from .roto.client import RotoControl
    from .roto.transport import SerialTransport

    roto = RotoControl(SerialTransport(args.port))
    try:
        fw = roto.firmware_version()
        mode = roto.mode()
        setup = roto.current_setup()
        print(f"Connected to ROTO-CONTROL on {args.port}")
        print(f"  firmware : {fw}")
        print(f"  mode     : {mode}")
        print(f"  setup    : #{setup.index} {setup.name!r}")
        return 0
    finally:
        roto.close()


def _cmd_mock_run(_args) -> int:
    from .bridge.bridge import Bridge
    from .daw.mock import MockDawBackend
    from .roto.client import RotoControl
    from .roto.transport import LoopbackTransport

    transport = LoopbackTransport()
    roto = RotoControl(transport)
    bridge = Bridge(roto, MockDawBackend())
    bridge.start()

    print(f"\n{len(transport.sent)} frames sent to (loopback) ROTO:")
    for frame in transport.sent:
        print("  " + frame.hex(" "))

    # simulate a knob turn and a button press arriving from the device
    print("\nsimulating knob 0 -> 0.25 and button 0 press:")
    bridge._on_control_value(bridge.cfg.channel, bridge.cfg.base_cc, 0.25)
    bridge._on_control_value(bridge.cfg.channel, bridge.cfg.button_base_cc, 1.0)
    bridge.stop()
    return 0


def _cmd_logic_mock(_args) -> int:
    """Exercise the Logic-dialect handshake + a full sweep learn offline."""
    from .bridge.logic_bridge import LogicBridge
    from .daw.source import MockSysexSource
    from .roto.logic_client import RotoLogicClient
    from .roto.sysex_client import LoopbackMidiPort
    from .sysex import codec
    from .sysex.constants import (
        LOGIC_COMMAND_STATUS, General, Group, Mixer, Plugin,
    )

    enums = {int(Group.GENERAL): General, int(Group.MIXER): Mixer,
             int(Group.PLUGIN): Plugin}

    def label(frame: bytes) -> str:
        if frame[0] != 0xF0:
            if frame[0] == LOGIC_COMMAND_STATUS:
                return f"CMD cc={frame[1]:#04x} v={frame[2]}"
            return f"CC {frame[0]:#04x} {frame[1]}={frame[2]}"
        m = codec.parse_sysex(frame)
        try:
            cmd = enums[m.group](m.command).name
        except (KeyError, ValueError):
            cmd = f"{m.command:#04x}"
        return f"{Group(m.group).name}/{cmd} {m.data.hex(' ')}"

    port = LoopbackMidiPort()
    client = RotoLogicClient(port)
    source = MockSysexSource()
    bridge = LogicBridge(client, source, synchronous=True)
    bridge.start()

    def show(title):
        print(f"\n{title}:")
        for f in port.sent:
            print("  " + label(f))
        port.sent.clear()

    show("DAW -> device on start")
    port.inject(codec.build_sysex(Group.GENERAL, General.PING_DAW))
    show("device PING -> daw_id=3 reply")
    port.inject(codec.build_sysex(Group.GENERAL, General.ROTO_DAW_CONNECTED))
    show("device CONNECTED -> command ack + mixer")
    port.inject(codec.build_sysex(Group.PLUGIN, Plugin.SET_PLUGIN_MODE, b"\x00"))
    show("device PLUGIN mode -> populate (details + values)")

    port.inject(codec.build_sysex(Group.PLUGIN, Plugin.SET_DEVICE_LEARN, b"\x01"))
    source.simulate_param_touch(0, 1)     # user grabs "Gain A"
    show("learn armed + param moved -> sweep trigger")

    def sweep_value(step: int):
        port.inject(codec.build_sysex(
            Group.PLUGIN, Plugin.PLUGIN_PARAM_SWEEP_VALUE,
            bytes((0x0E, 0x21, 0x01, step, step))))

    for step in (0x00, 0x7F, 0x00, 0x02, 0x04, 0x06, 0x08, 0x0A):
        sweep_value(step)
        source.simulate_device_param(0, 1, step / 127.0, f"{step / 1.27:.1f}%")
    show("device sweep ramp -> applied + pulled, then LEARN_PARAM")
    port.inject(codec.build_sysex(Group.PLUGIN, Plugin.PLUGIN_LEARN_COMPLETE))
    show("device LEARN_COMPLETE -> value restored")
    bridge.stop()
    return 0


def _cmd_logic_run(args) -> int:
    """Live Logic-dialect bridge: ROTO over USB-MIDI <-> REAPER (OSC + feed)."""
    from .bridge.logic_bridge import LogicBridge
    from .roto.logic_client import RotoLogicClient
    from .roto.sysex_client import MidoMidiPort

    port = MidoMidiPort(args.midi_in, args.midi_out)
    bridge = LogicBridge(RotoLogicClient(port), _real_source(),
                         minimal=getattr(args, "minimal", False))
    bridge.start()
    print("Logic-dialect bridge running; Ctrl-C to stop")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


def _cmd_bringup(args) -> int:
    """First-contact probe session: read-only checks + the first backup."""
    from .bringup import run_bringup

    roto = None
    if args.port:
        from .roto.client import RotoControl
        from .roto.transport import SerialTransport
        roto = RotoControl(SerialTransport(args.port))
    midi_port = None
    if not args.no_midi:
        try:
            from .roto.sysex_client import MidoMidiPort
            midi_port = MidoMidiPort(args.midi_in, args.midi_out)
        except Exception as exc:  # noqa: BLE001 - probe what we can
            print(f"WARNING: USB-MIDI unavailable ({exc}); "
                  "running serial-only steps", file=sys.stderr)
    try:
        report = run_bringup(roto=roto, port=midi_port, out_dir=args.out,
                             skip_backup=args.skip_backup)
    finally:
        if roto:
            roto.close()
        if midi_port:
            midi_port.close()
    return 0 if all(r.ok for r in report.results) else 1


def _real_source(daw: str = "auto"):
    """DAW source for real mode. The bridge speaks the same device dialect
    whatever the DAW — sources only translate DAW state."""
    from .daw.detect import detect_daw, make_source
    if daw == "auto":
        daw = detect_daw()
    return make_source(daw)


def _cmd_ui(args) -> int:
    """Open the desktop app (pywebview window over the local API)."""
    from .ui.shell import launch
    return launch(host=args.host, port=args.port,
                  view=args.view, daw=getattr(args, "daw", "auto"))


def _cmd_serve(args) -> int:
    """Run the local UI API (WebSocket JSON-RPC). Starts DISCONNECTED — use the
    `connect` RPC (or the device pill in the UI) once the ROTO is plugged in."""
    from .api.service import BridgeService
    from .api.ws import serve
    from .ui.shell import _library_path

    mode = getattr(args, "daw", "auto")
    service = BridgeService(source=_real_source(mode), daw=mode,
                            library_path=_library_path(), auto_connect=True)
    service.start()
    print(f"real API (disconnected — call `connect`) on "
          f"ws://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        serve(service, args.host, args.port)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return 0


def _cmd_run(args) -> int:
    from .bridge.bridge import Bridge
    from .daw.reaper import ReaperBackend
    from .roto.client import RotoControl
    from .roto.transport import SerialTransport

    midi = None
    if not args.no_midi:
        try:
            from .roto.midi import MidiIO
            midi = MidiIO()
        except Exception as exc:  # noqa: BLE001 - degrade loudly, keep serial up
            print(f"WARNING: USB-MIDI unavailable ({exc}); motors, LEDs and "
                  "knob input are DISABLED — labels/colours only.",
                  file=sys.stderr)
    roto = RotoControl(SerialTransport(args.port), midi=midi)
    bridge = Bridge(roto, ReaperBackend())
    bridge.start()
    print("bridge running; Ctrl-C to stop")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


def _cmd_proxy(args) -> int:
    from .roto.proxy import run_link_proxy
    return run_link_proxy(tap=args.tap, port_name=args.port_name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="roto-reaper", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-ports").set_defaults(func=_cmd_list_ports)

    hs = sub.add_parser("handshake")
    hs.add_argument("--port", required=True)
    hs.set_defaults(func=_cmd_handshake)

    sub.add_parser("mock-run").set_defaults(func=_cmd_mock_run)
    sub.add_parser("logic-mock").set_defaults(func=_cmd_logic_mock)

    lrun = sub.add_parser("logic-run",
                          help="live Logic-dialect (daw_id=3) bridge to REAPER")
    lrun.add_argument("--midi-in"), lrun.add_argument("--midi-out")
    lrun.add_argument("--minimal", action="store_true",
                      help="bisect aid: only the hardware-verified feature set")
    lrun.set_defaults(func=_cmd_logic_run)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--daw", default="auto",
                       choices=["auto", "reaper", "cubase", "protools", "system"],
                       help="DAW adapter; 'auto' (default) senses which feed is "
                            "live. system = control the Mac itself, no DAW")
    serve.set_defaults(func=_cmd_serve)

    bring = sub.add_parser("bringup")
    bring.add_argument("--port", help="serial port (enables backup + serial steps)")
    bring.add_argument("--midi-in"), bring.add_argument("--midi-out")
    bring.add_argument("--no-midi", action="store_true")
    bring.add_argument("--out", default="bringup",
                       help="directory for the report + backup files")
    bring.add_argument("--skip-backup", action="store_true")
    bring.set_defaults(func=_cmd_bringup)

    ui = sub.add_parser("ui")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--view", default=None,
                    choices=["live", "plugin", "device", "setups",
                             "library", "settings", "diag"],
                    help="open the app on this page (e.g. --view diag)")
    ui.add_argument("--daw", default="auto",
                    choices=["auto", "reaper", "cubase", "protools", "system"],
                    help="DAW adapter; 'auto' (default) senses which feed is "
                         "live. system = control the Mac itself, no DAW")
    ui.set_defaults(func=_cmd_ui)

    run = sub.add_parser("run")
    run.add_argument("--port", required=True)
    run.add_argument("--no-midi", action="store_true",
                     help="skip USB-MIDI (labels only; no motors/values)")
    run.set_defaults(func=_cmd_run)

    proxy = sub.add_parser("proxy",
        help="cross-DAW Link: proxy a native DAW's ROTO surface (Ableton/"
             "Bitwig/Logic) to share learned plugin maps")
    proxy.add_argument("--tap", action="store_true",
                       help="observe only: log the plugin identities the DAW "
                            "sends, without rewriting the stream")
    proxy.add_argument("--port-name", default="Athens Link",
                       help="virtual MIDI port name the DAW points at")
    proxy.set_defaults(func=_cmd_proxy)

    args = parser.parse_args(argv)
    # -v raises only OUR loggers: python-osc's dispatcher logs every unmatched
    # address to the root logger at DEBUG, which floods the output useless
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.verbose:
        logging.getLogger("athens").setLevel(logging.DEBUG)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
