"""Internal-cal level calibration.

The level is defined as the EXTERNAL CAL input level that would put the same signal on the
receivers: a table of that equivalent level, per IF frequency, with the Barracuda output
attenuator at 0 dB. Attenuation then lowers the level dB for dB (the HMC1119 is 0.25 dB steps).
Tables are measured per unit and stored as JSON (see ``Calibration.load``).
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from pathlib import Path

ATTENUATOR_MAX_DB = 31.75
ATTENUATOR_STEP_DB = 0.25


@dataclass(frozen=True)
class Calibration:
    unit: str
    measured: str
    frequency_mhz: tuple[float, ...]
    level_at_0db_dbm: tuple[float, ...]

    @classmethod
    def from_dict(cls, data: dict) -> "Calibration":
        f, p = [float(v) for v in data["frequency_mhz"]], [float(v) for v in data["level_at_0db_dbm"]]
        if len(f) < 2 or len(f) != len(p) or any(b <= a for a, b in zip(f, f[1:])):
            raise ValueError("calibration needs at least two increasing frequencies, one level each")
        return cls(str(data.get("unit", "")), str(data.get("measured", "")), tuple(f), tuple(p))

    @classmethod
    def load(cls, path: str | Path) -> "Calibration":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @property
    def range_mhz(self) -> tuple[float, float]:
        return self.frequency_mhz[0], self.frequency_mhz[-1]

    def level_at_0db(self, mhz: float) -> float:
        """Equivalent level (dBm) with no Barracuda attenuation, linearly interpolated."""
        f, p = self.frequency_mhz, self.level_at_0db_dbm
        if not f[0] <= mhz <= f[-1]:
            raise ValueError(f"{mhz:g} MHz is outside the calibrated range {f[0]:g}-{f[-1]:g} MHz")
        k = min(max(bisect.bisect_left(f, mhz), 1), len(f) - 1)
        return p[k - 1] + (p[k] - p[k - 1]) * (mhz - f[k - 1]) / (f[k] - f[k - 1])

    def peak_level_at_0db(self, start_mhz: float, stop_mhz: float) -> float:
        """Highest equivalent level across a band (table points inside it plus its edges)."""
        lo, hi = min(start_mhz, stop_mhz), max(start_mhz, stop_mhz)
        inside = [p for f, p in zip(self.frequency_mhz, self.level_at_0db_dbm) if lo <= f <= hi]
        return max(inside + [self.level_at_0db(lo), self.level_at_0db(hi)])
