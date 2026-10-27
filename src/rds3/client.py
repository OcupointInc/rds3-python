"""Customer control of an RDS3 receiver: signal path, frontends and the internal cal source.

Modes:
  through       antenna inputs on all 16 receivers; Barracuda cal source and its clocks off
  external_cal  the EXTERNAL CAL input routed to all 16 receivers; Barracuda off
  internal_cal  the Barracuda cal source routed to all 16 receivers, CW or swept

The internal source uses a fixed 11.8 GHz LO. Its level is the Barracuda's output level, before
the cal path (which has about 28 dB of insertion loss to each receiver input): -16.2 dBm at full
output down to -47.95 dBm. It is set with the internal attenuator from one average full-output
level (FULL_LEVEL_DBM), so it is approximate (a few dB across 400-650 MHz).
"""
from __future__ import annotations

import enum
import math
import socket
import threading
import time
from dataclasses import dataclass

from google.protobuf.json_format import MessageToDict

from ._proto import Packet

class Mode(str, enum.Enum):
    """Signal path for all 16 receivers."""
    THROUGH = "through"            # antenna inputs; internal cal source and its clocks off
    INTERNAL_CAL = "internal_cal"  # the internal cal source; powered and tuned only in this mode
    EXTERNAL_CAL = "external_cal"  # the EXTERNAL CAL input; internal cal source and its clocks off


MODES = tuple(Mode)
LO_MHZ = 11800
IF_MIN_MHZ, IF_MAX_MHZ = 50.0, 1500.0
# Barracuda output level with no internal attenuation: the 400-650 MHz average measured on the
# bench (-12.8 dBm at 400 MHz to -20.8 dBm at 650 MHz).
FULL_LEVEL_DBM = -16.2
ATTENUATOR_MAX_DB, ATTENUATOR_STEP_DB = 31.75, 0.25
MAX_LEVEL_DBM, MIN_LEVEL_DBM = FULL_LEVEL_DBM, FULL_LEVEL_DBM - ATTENUATOR_MAX_DB   # -16.2 to -47.95 dBm
CAL_PATH_LOSS_DB = 28.0   # Barracuda output to each receiver input (bench: through gain minus cal gain)
FRONTEND_ATTENUATION_MAX_DB = 31
SUPPORTED_FIRMWARE = ("3.0.0-rc3",)
RUNNING, FAULT = 4, 5
FAULTS = {1: "temperature sensor", 2: "over temperature", 3: "clock generator", 4: "synthesizer lock", 5: "GPIO"}
_BOTH_BANKS = 3


class RDS3Error(RuntimeError):
    """The RDS3 refused a request or reported a fault."""


@dataclass(frozen=True)
class Status:
    mode: Mode
    frontends_on: bool
    frontend_attenuation_db: int
    cal_source_running: bool
    fault: str | None
    level_dbm: float | None
    tone: dict | None
    firmware: str


class RDS3:
    """One RDS3 on the network. Not thread-safe across processes; one controller at a time.

    >>> with RDS3("192.168.1.249") as r:
    ...     r.set_frontends(True)
    ...     r.set_attenuation(10)
    ...     r.set_mode(Mode.INTERNAL_CAL)
    ...     r.cw(500, level_dbm=-30)
    """

    def __init__(self, host: str, port: int = 5000, timeout: float = 5.0,
                 check_firmware: bool = True):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._mode: Mode = "through"
        self._level_dbm: float | None = None
        self._tone: dict | None = None
        self.firmware = self._config().get("firmwareVersion", "")
        if check_firmware and self.firmware not in SUPPORTED_FIRMWARE:
            raise RDS3Error(f"RDS3 firmware {self.firmware or 'unknown'} is not supported; install "
                            f"{' or '.join(SUPPORTED_FIRMWARE)}")
        if check_firmware and (self._status().get("v2") or {}).get("feedbackDivider") != 1:
            raise RDS3Error("this RDS3's Barracuda is not configured for the 11.8 GHz LO plan")
        self._mode = self._read_mode()

    # -- connection ----------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None

    def __enter__(self) -> "RDS3":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, packet, expect: str):
        body = packet.SerializeToString()
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self._sock.settimeout(self.timeout)
                    self._sock.sendall(body)
                    reply = Packet.FromString(_read_packet(self._sock, self.timeout))
                    break
                except OSError:
                    if self._sock is not None:
                        self._sock.close()
                        self._sock = None
                    if attempt == 2:
                        raise
        kind = reply.WhichOneof("message_id")
        if kind == "error_response":
            raise RDS3Error(reply.error_response.detail or "request refused")
        if kind != expect:
            raise RDS3Error(f"unexpected reply {kind!r} (expected {expect})")
        return getattr(reply, expect)

    def _send(self, field: str, expect: str, **values):
        packet = Packet()
        message = getattr(packet, field)
        message.SetInParent()
        for key, value in values.items():
            setattr(message, key, value)
        return self._request(packet, expect)

    def _status(self) -> dict:
        return MessageToDict(self._send("get_status_request", "get_status_response"))

    def _config(self) -> dict:
        return MessageToDict(self._send("get_config_request", "get_config_response"))

    def _fixture(self, **apply):
        values = dict(apply=True, **apply) if apply else {}
        return self._send("rds3_control_request", "rds3_control_response", **values)

    # -- state ---------------------------------------------------------------------------
    def _read_mode(self) -> Mode:
        f = self._fixture()
        if not f.calibration_banks:
            return Mode.THROUGH
        return Mode.EXTERNAL_CAL if f.external_calibration else Mode.INTERNAL_CAL

    def status(self) -> Status:
        f, v = self._fixture(), self._status().get("v2") or {}
        fault = int(v.get("fault") or 0)
        return Status(mode=self._read_mode(), frontends_on=f.frontend_enabled,
                      frontend_attenuation_db=f.attenuation_db,
                      cal_source_running=int(v.get("state") or 0) == RUNNING,
                      fault=FAULTS.get(fault, f"code {fault}") if fault else None,
                      level_dbm=self._level_dbm, tone=self._tone, firmware=self.firmware)

    # -- frontends -----------------------------------------------------------------------
    def set_frontends(self, on: bool) -> None:
        """Power all 16 frontends on or off. Powering on starts at 31 dB attenuation (already-on
        frontends keep theirs). Turning them off also stops the internal cal source and returns
        to through mode."""
        f = self._fixture()
        if not on:
            self._fixture(frontend_enabled=False, attenuation_db=f.attenuation_db, calibration_banks=0)
            self._mode = Mode.THROUGH
        elif not f.frontend_enabled:
            self._fixture(frontend_enabled=True, attenuation_db=FRONTEND_ATTENUATION_MAX_DB, calibration_banks=0)
            self._mode = Mode.THROUGH

    def set_attenuation(self, attenuation_db: int) -> None:
        """Frontend attenuation, 0-31 dB in 1 dB steps, common to all 16 channels. The frontends
        must be on; the signal path is unchanged."""
        db = int(attenuation_db)
        if db != attenuation_db or not 0 <= db <= FRONTEND_ATTENUATION_MAX_DB:
            raise ValueError(f"frontend attenuation must be a whole number of dB, 0-{FRONTEND_ATTENUATION_MAX_DB}")
        f = self._fixture()
        if not f.frontend_enabled:
            raise RDS3Error("turn the frontends on first")
        self._fixture(frontend_enabled=True, attenuation_db=db, calibration_banks=f.calibration_banks,
                      external_calibration=f.external_calibration)

    # -- signal path ---------------------------------------------------------------------
    def set_mode(self, mode: Mode | str, timeout: float = 5.0) -> None:
        """Select Mode.THROUGH, Mode.INTERNAL_CAL or Mode.EXTERNAL_CAL. Only INTERNAL_CAL powers
        and tunes the internal cal source and the clock outputs that feed it; the other modes
        turn them off."""
        try:
            mode = Mode(mode)
        except ValueError:
            raise ValueError(f"mode must be one of {', '.join(m.value for m in Mode)}") from None
        f = self._fixture()
        if not f.frontend_enabled:
            raise RDS3Error("turn the frontends on first")
        if mode is not Mode.INTERNAL_CAL:
            # Source off first: the firmware drops any cal routing whenever it turns RF off.
            self._send("set_channels_enabled_request", "set_channels_enabled_response", enabled=False)
            self._fixture(frontend_enabled=True, attenuation_db=f.attenuation_db,
                          calibration_banks=0 if mode is Mode.THROUGH else _BOTH_BANKS,
                          external_calibration=mode is Mode.EXTERNAL_CAL)
            self._mode = mode
            return
        # Break first: no source reaches the receivers while the Barracuda starts.
        self._fixture(frontend_enabled=True, attenuation_db=f.attenuation_db, calibration_banks=0)
        self._send("set_channels_enabled_request", "set_channels_enabled_response", enabled=True)
        deadline = time.monotonic() + timeout
        while True:
            v = self._status().get("v2") or {}
            state, fault = int(v.get("state") or 0), int(v.get("fault") or 0)
            if state == RUNNING:
                break
            if state == FAULT or fault:
                self._send("set_channels_enabled_request", "set_channels_enabled_response", enabled=False)
                raise RDS3Error(f"internal cal source fault: {FAULTS.get(fault, f'code {fault}')}")
            if time.monotonic() > deadline:
                self._send("set_channels_enabled_request", "set_channels_enabled_response", enabled=False)
                raise RDS3Error("internal cal source did not start")
            time.sleep(0.05)
        # Lowest level until a tone and level are applied, then route it.
        self._send("set_dsa_attenuation_request", "set_dsa_attenuation_response",
                   quarter_db=int(ATTENUATOR_MAX_DB / ATTENUATOR_STEP_DB))
        self._mode = Mode.INTERNAL_CAL
        if self._tone and self._level_dbm is not None:
            self._apply_tone()
        self._fixture(frontend_enabled=True, attenuation_db=f.attenuation_db,
                      calibration_banks=_BOTH_BANKS, external_calibration=False)

    def off(self) -> None:
        """Stop the internal cal source and power the frontends down."""
        self.set_frontends(False)

    # -- internal cal source -------------------------------------------------------------
    def set_level(self, level_dbm: float) -> float:
        """Barracuda output level, -47.95 to -16.2 dBm (approximate: one average across the band).
        Each receiver input sees about CAL_PATH_LOSS_DB (28 dB) less. Returns the level requested. Applies now in internal_cal mode,
        otherwise when internal_cal is next selected."""
        self._take_level(level_dbm)
        if self._tone:
            self._apply_tone()
        return self._level_dbm

    def _take_level(self, level_dbm: float | None) -> None:
        if level_dbm is not None:
            if not math.isfinite(level_dbm) or not MIN_LEVEL_DBM <= level_dbm <= MAX_LEVEL_DBM:
                raise ValueError(f"level must be {MIN_LEVEL_DBM:g} to {MAX_LEVEL_DBM:g} dBm")
            self._level_dbm = float(level_dbm)

    def cw(self, frequency_mhz: float, level_dbm: float | None = None) -> dict:
        """A continuous tone at this frequency (MHz, 50-1500), optionally with its level (dBm,
        the Barracuda output level, -47.95 to -16.2)."""
        self._check_if(frequency_mhz)
        self._take_level(level_dbm)
        self._tone = {"kind": "cw", "frequency_mhz": float(frequency_mhz)}
        return self._apply_tone()

    def sweep(self, start_mhz: float, stop_mhz: float, sweep_time_s: float,
              level_dbm: float | None = None) -> dict:
        """A repeating sweep between two IF frequencies (MHz) every sweep_time_s seconds.
        With the 11.8 GHz high-side LO the tone runs from stop_mhz down to start_mhz, then
        restarts."""
        self._check_if(start_mhz); self._check_if(stop_mhz)
        if stop_mhz <= start_mhz:
            raise ValueError("stop must be above start")
        if not 1e-6 <= sweep_time_s <= 10.0:
            raise ValueError("sweep time must be 1 us to 10 s")
        self._take_level(level_dbm)
        self._tone = {"kind": "sweep", "start_mhz": float(start_mhz), "stop_mhz": float(stop_mhz),
                      "sweep_time_s": float(sweep_time_s)}
        return self._apply_tone()

    @staticmethod
    def _check_if(mhz: float) -> None:
        if not IF_MIN_MHZ <= mhz <= IF_MAX_MHZ:
            raise ValueError(f"IF frequency must be {IF_MIN_MHZ:g}-{IF_MAX_MHZ:g} MHz")

    def _apply_tone(self) -> dict:
        tone = dict(self._tone)
        if self._level_dbm is not None:
            steps = round((FULL_LEVEL_DBM - self._level_dbm) / ATTENUATOR_STEP_DB)
            attenuation = min(max(steps * ATTENUATOR_STEP_DB, 0.0), ATTENUATOR_MAX_DB)
            tone.update(level_dbm=round(FULL_LEVEL_DBM - attenuation, 2), attenuator_db=attenuation)
        if self._mode is not Mode.INTERNAL_CAL:
            tone["applied"] = False   # stored; applied when internal_cal is selected
            return tone
        if self._level_dbm is None:
            raise RDS3Error("set a level first (set_level)")
        self._send("set_lo_frequency_request", "set_lo_frequency_response", frequency_mhz=LO_MHZ)
        if tone["kind"] == "cw":
            self._send("set_pll_frequency_request", "set_pll_frequency_response",
                       frequency_mhz=int(round(LO_MHZ - tone["frequency_mhz"])))
        else:   # the synthesizer ramps upward: RF from LO - stop to LO - start
            start_rf = int(round(LO_MHZ - tone["stop_mhz"]))
            span = int(round(tone["stop_mhz"] - tone["start_mhz"]))
            reply = self._send("set_chirp_request", "set_chirp_response", start_freq_mhz=start_rf,
                               deviation_mhz=span, ramp_time_us=max(1, int(round(tone["sweep_time_s"] * 1e6))),
                               mode=0, enabled=True)
            if not reply.locked:
                raise RDS3Error("the sweep did not lock")
        self._send("set_dsa_attenuation_request", "set_dsa_attenuation_response",
                   quarter_db=int(round(tone["attenuator_db"] / ATTENUATOR_STEP_DB)))
        tone["applied"] = True
        return tone


def _read_packet(sock: socket.socket, timeout: float) -> bytes:
    """One length-delimited Packet field (tag, varint length, body)."""
    deadline, frame = time.monotonic() + timeout, bytearray()

    def read(n):
        target = len(frame) + n
        while len(frame) < target:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("RDS3 reply timed out")
            sock.settimeout(left)
            chunk = sock.recv(target - len(frame))
            if not chunk:
                raise OSError("RDS3 closed the connection")
            frame.extend(chunk)

    def varint():
        value = 0
        for shift in range(0, 35, 7):
            read(1)
            value |= (frame[-1] & 0x7F) << shift
            if not frame[-1] & 0x80:
                return value
        raise OSError("invalid reply header")

    if varint() & 7 != 2:
        raise OSError("invalid reply")
    read(varint())
    return bytes(frame)
