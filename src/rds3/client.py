"""Customer control of an RDS3 receiver: signal path, frontends and the internal cal source.

Modes:
  through       antenna inputs on all 16 receivers; Barracuda cal source and its clocks off
  external_cal  the EXTERNAL CAL input routed to all 16 receivers; Barracuda off
  internal_cal  the Barracuda cal source routed to all 16 receivers, CW or swept

The internal source uses a fixed 11.8 GHz LO. Its level is the external-cal input level that
would produce the same signal at the receivers, at most -38 dBm, set through a per-unit
calibration table.
"""
from __future__ import annotations

import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Literal

from google.protobuf.json_format import MessageToDict

from ._proto import Packet
from .calibration import ATTENUATOR_MAX_DB, ATTENUATOR_STEP_DB, Calibration

Mode = Literal["through", "internal_cal", "external_cal"]
MODES = ("through", "internal_cal", "external_cal")
LO_MHZ = 11800
IF_MIN_MHZ, IF_MAX_MHZ = 50.0, 1500.0
MAX_LEVEL_DBM = -38.0
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

    >>> with RDS3("192.168.1.135", calibration="RDS3-62EB988A0FDC.json") as r:
    ...     r.set_frontends(True, attenuation_db=10)
    ...     r.set_mode("internal_cal")
    ...     r.set_level(-50)
    ...     r.cw(500)
    """

    def __init__(self, host: str, port: int = 5000, timeout: float = 5.0,
                 calibration: Calibration | str | None = None, check_firmware: bool = True):
        self.host, self.port, self.timeout = host, port, timeout
        self.calibration = Calibration.load(calibration) if isinstance(calibration, str) else calibration
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
            return "through"
        return "external_cal" if f.external_calibration else "internal_cal"

    def status(self) -> Status:
        f, v = self._fixture(), self._status().get("v2") or {}
        fault = int(v.get("fault") or 0)
        return Status(mode=self._read_mode(), frontends_on=f.frontend_enabled,
                      frontend_attenuation_db=f.attenuation_db,
                      cal_source_running=int(v.get("state") or 0) == RUNNING,
                      fault=FAULTS.get(fault, f"code {fault}") if fault else None,
                      level_dbm=self._level_dbm, tone=self._tone, firmware=self.firmware)

    # -- frontends -----------------------------------------------------------------------
    def set_frontends(self, on: bool, attenuation_db: int | None = None) -> None:
        """Power all 16 frontends on or off; attenuation is common to all of them (0-31 dB).
        Turning them off also stops the internal cal source and returns to through mode."""
        f = self._fixture()
        db = f.attenuation_db if attenuation_db is None else int(attenuation_db)
        if not 0 <= db <= FRONTEND_ATTENUATION_MAX_DB:
            raise ValueError(f"frontend attenuation must be 0-{FRONTEND_ATTENUATION_MAX_DB} dB")
        if not on:
            self._fixture(frontend_enabled=False, attenuation_db=db, calibration_banks=0)
            self._mode = "through"
            return
        self._fixture(frontend_enabled=True, attenuation_db=db, calibration_banks=f.calibration_banks,
                      external_calibration=f.external_calibration)

    def set_attenuation(self, attenuation_db: int) -> None:
        """Frontend attenuation (0-31 dB, all 16 channels); the signal path is unchanged."""
        if not self._fixture().frontend_enabled:
            raise RDS3Error("turn the frontends on first")
        self.set_frontends(True, attenuation_db)

    # -- signal path ---------------------------------------------------------------------
    def set_mode(self, mode: Mode, timeout: float = 5.0) -> None:
        """Select through, internal_cal or external_cal. Only internal_cal powers the Barracuda
        and the clock outputs that feed it; the other modes turn them off."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        f = self._fixture()
        if not f.frontend_enabled:
            raise RDS3Error("turn the frontends on first")
        if mode != "internal_cal":
            self._fixture(frontend_enabled=True, attenuation_db=f.attenuation_db,
                          calibration_banks=0 if mode == "through" else _BOTH_BANKS,
                          external_calibration=mode == "external_cal")
            self._send("set_channels_enabled_request", "set_channels_enabled_response", enabled=False)
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
        self._mode = "internal_cal"
        if self._tone and self._level_dbm is not None:
            self._apply_tone()
        self._fixture(frontend_enabled=True, attenuation_db=f.attenuation_db,
                      calibration_banks=_BOTH_BANKS, external_calibration=False)

    def off(self) -> None:
        """Stop the internal cal source and power the frontends down."""
        self.set_frontends(False)

    # -- internal cal source -------------------------------------------------------------
    def set_level(self, level_dbm: float) -> float:
        """Internal-cal level as the equivalent external-cal input level, at most -38 dBm.
        Returns the level actually set (0.25 dB steps). Applies now in internal_cal mode,
        otherwise when internal_cal is next selected."""
        if not math.isfinite(level_dbm) or level_dbm > MAX_LEVEL_DBM:
            raise ValueError(f"level must be at most {MAX_LEVEL_DBM:g} dBm")
        if self.calibration is None:
            raise RDS3Error("no level calibration loaded for this unit")
        self._level_dbm = float(level_dbm)
        if self._tone:
            self._apply_tone()
        return self._level_dbm

    def cw(self, frequency_mhz: float) -> dict:
        """A continuous tone at this IF frequency (MHz)."""
        self._check_if(frequency_mhz)
        self._tone = {"kind": "cw", "frequency_mhz": float(frequency_mhz)}
        return self._apply_tone()

    def sweep(self, start_mhz: float, stop_mhz: float, sweep_time_s: float) -> dict:
        """A repeating sweep between two IF frequencies (MHz) every sweep_time_s seconds.
        With the 11.8 GHz high-side LO the tone runs from stop_mhz down to start_mhz, then
        restarts. The level is chosen so no point in the band exceeds the requested level."""
        self._check_if(start_mhz); self._check_if(stop_mhz)
        if stop_mhz <= start_mhz:
            raise ValueError("stop must be above start")
        if not 1e-6 <= sweep_time_s <= 10.0:
            raise ValueError("sweep time must be 1 us to 10 s")
        self._tone = {"kind": "sweep", "start_mhz": float(start_mhz), "stop_mhz": float(stop_mhz),
                      "sweep_time_s": float(sweep_time_s)}
        return self._apply_tone()

    @staticmethod
    def _check_if(mhz: float) -> None:
        if not IF_MIN_MHZ <= mhz <= IF_MAX_MHZ:
            raise ValueError(f"IF frequency must be {IF_MIN_MHZ:g}-{IF_MAX_MHZ:g} MHz")

    def _attenuation_for(self, tone: dict) -> tuple[float, float, float]:
        """(attenuation dB, level at the lowest point, level at the highest point)."""
        cal = self.calibration
        if tone["kind"] == "cw":
            lo = hi = cal.level_at_0db(tone["frequency_mhz"])
        else:
            hi = cal.peak_level_at_0db(tone["start_mhz"], tone["stop_mhz"])
            lo = min(cal.level_at_0db(tone["start_mhz"]), cal.level_at_0db(tone["stop_mhz"]),
                     *[p for f, p in zip(cal.frequency_mhz, cal.level_at_0db_dbm)
                       if tone["start_mhz"] <= f <= tone["stop_mhz"]])
        target = self._level_dbm
        attenuation = math.ceil((hi - target) / ATTENUATOR_STEP_DB - 1e-9) * ATTENUATOR_STEP_DB
        if attenuation < 0:
            raise RDS3Error(f"{target:g} dBm is above what the source reaches here ({hi:.1f} dBm)")
        if attenuation > ATTENUATOR_MAX_DB:
            raise RDS3Error(f"{target:g} dBm is below the lowest level here ({hi - ATTENUATOR_MAX_DB:.1f} dBm)")
        return attenuation, lo - attenuation, hi - attenuation

    def _apply_tone(self) -> dict:
        tone = dict(self._tone)
        if self._level_dbm is not None and self.calibration is not None:
            attenuation, lo, hi = self._attenuation_for(tone)
            tone.update(level_dbm=round(hi, 2), level_min_dbm=round(lo, 2), attenuator_db=attenuation)
        if self._mode != "internal_cal":
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
