"""python -m rds3 discover | python -m rds3 set-network MAC (--ip IP --subnet MASK [--gateway GW] | --dhcp)"""
import argparse
import sys

from .network import NetworkSetupError, discover, set_network


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rds3", description="Find RDS3s and set their network address.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--interface", metavar="IP", help="only use the local interface with this address")
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("discover", parents=[common],
                               help="list the RDS3s on this network segment, whatever their subnet")
    find.add_argument("--timeout", type=float, default=2.0)
    find.add_argument("--all", action="store_true", help="also list other Ocupoint devices")
    setup = commands.add_parser("set-network", parents=[common], help="give an RDS3 a static address, or DHCP")
    setup.add_argument("mac", help="the RDS3's MAC, as listed by discover")
    setup.add_argument("--ip")
    setup.add_argument("--subnet", help="mask (255.255.255.0) or prefix length (24)")
    setup.add_argument("--gateway", help="optional")
    setup.add_argument("--dhcp", action="store_true", help="use DHCP (--ip/--subnet/--gateway become the fallback)")
    args = parser.parse_args(argv)

    if args.command == "discover":
        devices = discover(args.timeout, args.interface, all_boards=args.all)
        for device in devices:
            print(device)
        if not devices:
            print("No RDS3 answered. It must be on the same switch as this computer (broadcasts do not cross "
                  "routers); try --interface with this computer's address on that network.", file=sys.stderr)
        return 0 if devices else 1
    try:
        device = set_network(args.mac, args.ip, args.subnet, args.gateway, args.dhcp, args.interface)
    except (ValueError, NetworkSetupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"done: {device}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
