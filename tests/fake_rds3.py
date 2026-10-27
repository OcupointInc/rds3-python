"""A minimal RDS3 on a local socket that follows the firmware's rules (3.0.0-rc3)."""
import socket
import threading

from rds3._proto import Packet
from rds3.client import _read_packet


class FakeRDS3:
    def __init__(self, firmware="3.0.0-rc3", divider=1, lock=True):
        self.firmware, self.divider, self.lock = firmware, divider, lock
        self.frontend, self.att, self.banks, self.external = False, 31, 0, False
        self.rf, self.fault, self.dsa_q, self.lo, self.pll, self.chirp = False, 0, 127, 0, 0, None
        self.log = []
        self.server = socket.socket(); self.server.bind(("127.0.0.1", 0)); self.server.listen()
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        with conn:
            while True:
                try:
                    request = Packet.FromString(_read_packet(conn, 5))
                except OSError:
                    return
                conn.sendall(self._handle(request).SerializeToString())

    def _error(self, detail):
        p = Packet(); p.error_response.code = 2; p.error_response.detail = detail; return p

    def _handle(self, req):
        kind = req.WhichOneof("message_id"); m = getattr(req, kind); self.log.append(kind)
        reply = Packet()
        running = self.rf and not self.fault
        if kind == "get_config_request":
            reply.get_config_response.firmware_version = self.firmware
        elif kind == "get_status_request":
            s = reply.get_status_response
            s.v2.feedback_divider = self.divider
            s.v2.state = 5 if self.fault else (4 if self.rf else 0); s.v2.fault = self.fault
            s.cal_attenuation_db = self.dsa_q // 4
        elif kind == "rds3_control_request":
            if m.apply:
                if not m.frontend_enabled:
                    self.frontend, self.banks, self.rf = False, 0, False
                else:
                    if m.calibration_banks and not m.external_calibration and not running:
                        return self._error("RDS3 requires carrier, valid settings and locked internal RF")
                    self.frontend, self.att, self.banks, self.external = True, m.attenuation_db, m.calibration_banks, m.external_calibration
            r = reply.rds3_control_response
            r.frontend_enabled, r.attenuation_db, r.calibration_banks, r.external_calibration = self.frontend, self.att, self.banks, self.external
        elif kind == "set_channels_enabled_request":
            if m.enabled and not self.frontend:
                return self._error("frontends off")
            self.rf = m.enabled and self.frontend
            if self.rf and not self.lock:
                self.fault, self.rf = 4, False
            if not m.enabled:
                self.banks = 0          # rds3.c rf_off(): calibration is dropped before RF goes off
                self.dsa_q = 127
            reply.set_channels_enabled_response.SetInParent()
        elif kind in ("set_dsa_attenuation_request", "set_lo_frequency_request", "set_pll_frequency_request", "set_chirp_request"):
            if not running:
                return self._error("V2 frontend off, transitioning, or faulted")
            if kind == "set_dsa_attenuation_request": self.dsa_q = m.quarter_db
            elif kind == "set_lo_frequency_request": self.lo = m.frequency_mhz
            elif kind == "set_pll_frequency_request": self.pll, self.chirp = m.frequency_mhz, None
            else:
                self.chirp = (m.start_freq_mhz, m.deviation_mhz, m.ramp_time_us, m.mode, m.enabled)
                reply.set_chirp_response.locked = True
                return reply
            getattr(reply, kind.replace("_request", "_response")).SetInParent()
        else:
            return self._error("unsupported")
        return reply

    def close(self):
        self.server.close()
