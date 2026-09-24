#!/usr/bin/env python3
"""``cansend`` / ``candump`` equivalents for the containerized CAN bus.

The Linux ``can-utils`` only speak SocketCAN, which does not exist on macOS or
Windows hosts. This tool speaks whatever python-can interface the HVAC
simulator uses, so the same commands work on every platform:

* default: ``udp_multicast`` (a UDP multicast group inside the compose
  network; pure userspace, no kernel support needed)
* ``CAN_BUSTYPE=socketcan CAN_CHANNEL=vcan0`` for a real/virtual Linux CAN
  interface

Usage (via compose)::

    podman-compose --profile can-tools -f docker-compose.yml run --rm can-tools send 321#160A01
    podman-compose --profile can-tools -f docker-compose.yml run --rm can-tools dump --decode

``send`` takes the ``cansend`` frame syntax ``<ID hex>#<DATA hex>``; IDs longer
than three hex digits are sent as 29-bit extended IDs. ``dump`` prints frames
in a ``candump``-like format and, with ``--decode``, renders known frames by
signal name using the ``hvac.dbc`` contract shared with the simulator.
"""

import argparse
import os
import sys

import can

BUSTYPE = os.getenv("CAN_BUSTYPE", "udp_multicast")
CHANNEL = os.getenv("CAN_CHANNEL", "239.74.163.2")
DBC_PATH = os.getenv("CAN_DBC_PATH", "/usr/local/share/hvac.dbc")


def make_bus() -> can.BusABC:
    return can.Bus(interface=BUSTYPE, channel=CHANNEL)


def parse_frame(spec: str) -> can.Message:
    """Parse ``<ID>#<HEXDATA>`` (cansend syntax) into a python-can message."""
    ident, sep, data = spec.partition("#")
    if not sep:
        raise ValueError(f"invalid frame {spec!r}, expected <ID>#<HEXDATA>, e.g. 321#160A01")
    return can.Message(
        arbitration_id=int(ident, 16),
        data=bytes.fromhex(data) if data else b"",
        is_extended_id=len(ident) > 3,
    )


def load_dbc():
    """Load the DBC for ``--decode``; returns ``None`` (with a warning) on failure."""
    try:
        import cantools

        return cantools.database.load_file(DBC_PATH)
    except Exception as exc:
        print(f"cannot load DBC {DBC_PATH}: {exc}", file=sys.stderr)
        return None


def decode_with_dbc(db, msg: can.Message) -> str:
    """Render ``  NAME: sig=val ...`` for frames the DBC knows, else ``""``."""
    try:
        message = db.get_message_by_frame_id(msg.arbitration_id)
        signals = message.decode(bytes(msg.data))
    except Exception:
        return ""
    rendered = " ".join(f"{k}={v}" for k, v in signals.items())
    return f"  {message.name}: {rendered}"


def cmd_send(args: argparse.Namespace) -> int:
    message = parse_frame(args.frame)
    with make_bus() as bus:
        bus.send(message)
    print(f"sent {args.frame} on {BUSTYPE}:{CHANNEL}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    db = load_dbc() if args.decode else None
    received = 0
    with make_bus() as bus:
        print(f"listening on {BUSTYPE}:{CHANNEL} (ctrl-c to stop)", flush=True)
        while args.count is None or received < args.count:
            msg = bus.recv(timeout=args.timeout)
            if msg is None:
                print(f"no frame within {args.timeout}s, exiting", file=sys.stderr)
                return 1
            data = " ".join(f"{b:02X}" for b in msg.data)
            decoded = decode_with_dbc(db, msg) if db is not None else ""
            print(
                f"({msg.timestamp:.6f})  {CHANNEL}  {msg.arbitration_id:03X}   [{msg.dlc}]  {data}{decoded}",
                flush=True,
            )
            received += 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send", help="send one frame (cansend syntax)")
    send_parser.add_argument("frame", help="frame as <ID>#<HEXDATA>, e.g. 321#160A01")
    send_parser.set_defaults(func=cmd_send)

    dump_parser = subparsers.add_parser("dump", help="print received frames (candump-like)")
    dump_parser.add_argument("--count", type=int, default=None, help="exit after N frames (default: run forever)")
    dump_parser.add_argument("--timeout", type=float, default=30.0, help="per-frame receive timeout in seconds")
    dump_parser.add_argument("--decode", action="store_true", help="decode known frames via the hvac.dbc contract")
    dump_parser.set_defaults(func=cmd_dump)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
