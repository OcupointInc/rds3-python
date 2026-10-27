"""Walk an RDS3 through every library function and check what it reports back.

    python examples/hardware_check.py 192.168.1.249

Leaves the receivers on through (antenna inputs) with the internal cal source off. The internal
source is used at -36 dBm Barracuda output (about -64 dBm at each receiver input).
"""
import sys
import time

from rds3 import CAL_PATH_LOSS_DB, Mode, RDS3

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}", flush=True)


def main(host):
    r = RDS3(host)
    s = r.status()
    check("connect", True, f"firmware {r.firmware}, mode {s.mode.value}, frontends {'on' if s.frontends_on else 'off'}")

    r.set_frontends(True, attenuation_db=20)
    s = r.status()
    check("frontends on at 20 dB", s.frontends_on and s.frontend_attenuation_db == 20, f"{s.frontend_attenuation_db} dB")
    r.set_attenuation(0)
    check("frontend attenuation 0 dB", r.status().frontend_attenuation_db == 0)

    r.set_mode(Mode.EXTERNAL_CAL)
    s = r.status()
    check("external cal", s.mode is Mode.EXTERNAL_CAL and not s.cal_source_running, "internal source off")

    t0 = time.monotonic()
    r.set_mode(Mode.INTERNAL_CAL)
    s = r.status()
    check("internal cal", s.mode is Mode.INTERNAL_CAL and s.cal_source_running and not s.fault,
          f"source locked in {time.monotonic() - t0:.1f} s")

    tone = r.cw(500, level_dbm=-36)
    check("CW 500 MHz at -36 dBm", tone["applied"] and r.status().cal_source_running,
          f"attenuator {tone['attenuator_db']} dB, about {tone['level_dbm'] - CAL_PATH_LOSS_DB:.0f} dBm at each receiver")
    tone = r.sweep(400, 650, sweep_time_s=0.5, level_dbm=-36)
    check("sweep 400-650 MHz in 0.5 s", tone["applied"] and r.status().cal_source_running)
    tone = r.cw(450)
    check("CW 450 MHz, level kept", tone["applied"] and abs(tone["level_dbm"] + 36) <= 0.25 and r.status().level_dbm == -36)

    r.set_mode(Mode.THROUGH)
    s = r.status()
    check("through", s.mode is Mode.THROUGH and not s.cal_source_running, "internal source and its clocks off")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "192.168.1.249"))
