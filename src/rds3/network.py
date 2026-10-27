"""Find RDS3s on the local network and set their address, without knowing their IP or subnet.

Both use UDP broadcasts on port 5001, which reach every device on the same Ethernet segment
(switch) whatever subnet it is configured for, and the RDS3 answers a host on another subnet by
broadcast too. They do not cross routers.
"""
from __future__ import annotations

import ipaddress
import select
import socket
import sys
import time
from dataclasses import dataclass

from google.protobuf.message import DecodeError

from ._proto import Packet

DISCOVERY_PORT = 5001
_BROADCAST = "255.255.255.255"
_RESEND_S = 0.4
_REBOOT_S = 3.0            # SetNetwork: save + reboot before the network comes back


class NetworkSetupError(RuntimeError):
    """The RDS3 refused a network setting, or did not answer."""


@dataclass(frozen=True)
class Device:
    """One device that answered discovery."""
    mac: str
    ip: str
    subnet: str
    gateway: str             # "0.0.0.0" = none
    dhcp: bool               # configured for DHCP (False = static)
    dhcp_fallback: bool      # DHCP got no answer; running on its fallback address
    board: str
    firmware: str
    serial: str
    control_port: int
    network_setup: bool      # accepts set_network() (firmware 3.0.0-rc4 or later)

    def __str__(self) -> str:
        prefix = ipaddress.IPv4Network(f"0.0.0.0/{self.subnet}").prefixlen
        mode = "DHCP (no server: fallback address)" if self.dhcp_fallback else ("DHCP" if self.dhcp else "static")
        gw = "" if self.gateway == "0.0.0.0" else f" gw {self.gateway}"
        return f"{self.board} {self.mac}  {self.ip}/{prefix}{gw}  {mode}  firmware {self.firmware}"


def discover(timeout: float = 2.0, interface_ip: str | None = None, all_boards: bool = False,
             address: str = _BROADCAST, port: int = DISCOVERY_PORT) -> list[Device]:
    """Every RDS3 that answers within `timeout` seconds, sorted by IP.

    The request goes out of every IPv4 interface (or only the one with `interface_ip`).
    `all_boards=True` also lists other Ocupoint devices (RF switches, Barracudas).
    """
    found: dict[str, Device] = {}
    for reply in _exchange(_discovery_request(), timeout, interface_ip, address, port, until=None):
        if reply.WhichOneof("message_id") != "discovery_response":
            continue
        device = _device(reply.discovery_response)
        if all_boards or device.board == "rds3":
            found[device.mac] = device
    return sorted(found.values(), key=lambda d: ipaddress.IPv4Address(d.ip))


def set_network(mac: str | Device, ip: str | None = None, subnet: str | int | None = None,
                gateway: str | None = None, dhcp: bool = False, interface_ip: str | None = None,
                wait: bool = True, timeout: float = 3.0,
                address: str = _BROADCAST, port: int = DISCOVERY_PORT) -> Device | None:
    """Give the RDS3 with this MAC a static address, or switch it to DHCP.

    Static: `ip`, `subnet` (e.g. "255.255.255.0" or 24) and optionally `gateway`.
    DHCP (`dhcp=True`): `ip`/`subnet`/`gateway` are optional and become the fallback address
    used when no DHCP server answers (default 169.254.x.y). The RDS3 saves the setting and
    reboots into it. With `wait=True` this waits for it to come back (up to 30 s) and returns
    it as rediscovered; otherwise returns None once the RDS3 has accepted the setting.
    """
    mac_bytes = _mac_bytes(mac.mac if isinstance(mac, Device) else mac)
    ip_b, subnet_b, gateway_b = _address(ip, subnet, gateway, dhcp)
    request = Packet()
    r = request.set_network_request
    r.mac, r.dhcp, r.ip, r.subnet, r.gateway = mac_bytes, dhcp, ip_b, subnet_b, gateway_b

    def matches(reply):
        kind = reply.WhichOneof("message_id")
        return kind == "error_response" or (kind == "set_network_response" and reply.set_network_response.mac == mac_bytes)

    replies = [p for p in _exchange(request, timeout, interface_ip, address, port, until=matches) if matches(p)]
    if not replies:
        raise NetworkSetupError(f"no RDS3 with MAC {_mac_text(mac_bytes)} answered (it needs firmware "
                                "3.0.0-rc4 or later, and must be on the same switch or Ethernet segment)")
    if replies[0].WhichOneof("message_id") == "error_response":
        raise NetworkSetupError(f"RDS3 refused the setting: {replies[0].error_response.detail}")
    if not wait:
        return None
    time.sleep(_REBOOT_S)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        for device in discover(1.0, interface_ip, True, address, port):
            if device.mac == _mac_text(mac_bytes) and (dhcp or device.ip == _ip_text(ip_b)):
                return device
    raise NetworkSetupError(f"RDS3 {_mac_text(mac_bytes)} accepted the setting but did not reappear within 30 s")


def _discovery_request():
    p = Packet()
    p.discovery_request.SetInParent()
    return p


def _device(r) -> Device:
    return Device(mac=_mac_text(r.mac), ip=_ip_text(r.ip), subnet=_ip_text(r.subnet), gateway=_ip_text(r.gateway),
                  dhcp=r.dhcp, dhcp_fallback=r.dhcp_fallback, board=r.board, firmware=r.firmware_version,
                  serial=r.serial, control_port=r.control_port or 5000, network_setup=r.network_setup)


def _exchange(request, timeout, interface_ip, address, port, until):
    """Send `request` from every interface (resending every 0.4 s) and yield replies until
    `timeout`, or until a reply satisfies `until`."""
    payload = request.SerializeToString()
    receiver = _udp_socket()
    receiver.bind(("", 0))
    local_port = receiver.getsockname()[1]
    senders = [receiver]
    # A socket bound to each interface address on the same port makes the request leave
    # by that interface; replies are broadcast to the port, so `receiver` hears them all
    # (unicast replies reach the bound socket instead, hence select over all).
    for ip in ([interface_ip] if interface_ip else _local_ips()):
        s = _udp_socket()
        try:
            s.bind((ip, local_port))
        except OSError:
            s.close()
            continue
        senders.append(s)
    if interface_ip and len(senders) == 1:
        receiver.close()
        raise OSError(f"no local interface has the address {interface_ip}")
    try:
        deadline, next_send = time.monotonic() + timeout, 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                return
            if now >= next_send:
                for s in senders[1:] or senders:
                    try:
                        s.sendto(payload, (address, port))
                    except OSError:
                        pass                    # an interface that cannot send (down, no route)
                next_send = now + _RESEND_S
            ready, _, _ = select.select(senders, [], [], max(0.0, min(deadline, next_send) - now))
            for s in ready:
                try:
                    reply = Packet.FromString(s.recv(2048))
                except (OSError, DecodeError):
                    continue
                yield reply
                if until is not None and until(reply):
                    return
    finally:
        for s in senders:
            s.close()


def _udp_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return s


def _local_ips() -> list[str]:
    """The host's IPv4 interface addresses, loopback excluded."""
    ips: set[str] = set()
    if sys.platform.startswith("linux"):
        import fcntl
        import struct
        for _, name in socket.if_nameindex():
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))  # SIOCGIFADDR
                ips.add(socket.inet_ntoa(packed[20:24]))
            except OSError:
                pass                            # no IPv4 address on this interface
            finally:
                probe.close()
    try:
        ips.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def _address(ip, subnet, gateway, dhcp):
    if not dhcp and (ip is None or subnet is None):
        raise ValueError("a static setting needs ip and subnet")
    if ip is None:
        if subnet is not None or gateway is not None:
            raise ValueError("give ip with subnet/gateway")
        return bytes(4), bytes(4), bytes(4)
    if subnet is None:
        raise ValueError("give subnet with ip")
    host = ipaddress.IPv4Address(ip)
    try:
        network = ipaddress.IPv4Network(f"{host}/{subnet}", strict=False)
    except ValueError as exc:
        raise ValueError(f"invalid subnet {subnet!r}") from exc
    if not 8 <= network.prefixlen <= 30:
        raise ValueError("subnet must be /8 to /30")
    if host in (network.network_address, network.broadcast_address):
        raise ValueError(f"{host} is the network or broadcast address of {network}")
    gw = ipaddress.IPv4Address(gateway or "0.0.0.0")
    if int(gw) and (gw not in network or gw in (host, network.network_address, network.broadcast_address)):
        raise ValueError(f"gateway {gw} is not another host on {network}")
    return host.packed, network.netmask.packed, gw.packed


def _mac_bytes(text: str) -> bytes:
    digits = "".join(c for c in text if c not in ":-. ")
    if len(digits) != 12:
        raise ValueError(f"invalid MAC {text!r}")
    return bytes.fromhex(digits)


def _mac_text(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _ip_text(b: bytes) -> str:
    return str(ipaddress.IPv4Address(b)) if len(b) == 4 else "0.0.0.0"
