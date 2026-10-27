"""RDS3 UDP discovery / SetNetwork responder on 127.0.0.1, following discovery.c (3.0.0-rc4)."""
import socket
import threading

from rds3._proto import Packet


class FakeDiscovery:
    def __init__(self, board="rds3", mac=bytes.fromhex("7ac5bad851d6"), ip=(169, 254, 81, 214),
                 subnet=(255, 255, 0, 0), refuse=None):
        self.board, self.mac, self.refuse = board, mac, refuse
        self.ip, self.subnet, self.gateway, self.dhcp, self.fallback = bytes(ip), bytes(subnet), bytes(4), True, True
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                data, peer = self.sock.recvfrom(2048)
            except OSError:
                return
            req = Packet.FromString(data)
            kind = req.WhichOneof("message_id")
            self.requests.append(kind)
            reply = Packet()
            if kind == "discovery_request":
                r = reply.discovery_response
                r.board, r.firmware_version, r.mac, r.ip, r.control_port = self.board, "3.0.0-rc4", self.mac, self.ip, 5000
                r.network_setup, r.dhcp, r.dhcp_fallback, r.subnet, r.gateway = True, self.dhcp, self.fallback, self.subnet, self.gateway
            elif kind == "set_network_request":
                m = req.set_network_request
                if m.mac != self.mac:
                    continue                                   # another device's request
                if self.refuse:
                    reply.error_response.code = 3; reply.error_response.detail = self.refuse
                else:
                    reply.set_network_response.mac, reply.set_network_response.rebooting = self.mac, True
                    # Saved and rebooted: the new setting is in force at the next discovery.
                    self.dhcp, self.fallback = m.dhcp, False
                    if not m.dhcp:
                        self.ip, self.subnet, self.gateway = m.ip, m.subnet, m.gateway
            else:
                continue
            self.sock.sendto(reply.SerializeToString(), peer)

    def close(self):
        self.sock.close()
