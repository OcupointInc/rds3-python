# rds3: Python control for the RDS3 receiver

Controls the signal path, the 16 frontends and the internal calibration source of an RDS3 over
Ethernet (TCP port 5000).

```python
from rds3 import RDS3

with RDS3("192.168.1.135", calibration="RDS3-62EB988A0FDC.json") as r:
    r.set_frontends(True, attenuation_db=10)   # power on all 16 frontends, 0-31 dB
    r.set_mode("internal_cal")                 # through | internal_cal | external_cal
    r.set_level(-50)                           # dBm, at most -38
    r.cw(500)                                  # CW tone at IF 500 MHz
    r.sweep(400, 650, sweep_time_s=0.5)        # or a repeating sweep
    print(r.status())
    r.set_mode("through")
    r.off()                                    # frontends and cal source off
```

## Modes

| Mode | Receivers see | Internal cal source and its clocks |
|---|---|---|
| `through` | their antenna inputs | off |
| `external_cal` | the EXTERNAL CAL input, all 16 channels | off |
| `internal_cal` | the internal source, all 16 channels | on |

Changing mode never routes two sources at once: the receivers return to their antenna inputs
while the internal source starts, and it is routed only once it is running.

## Frontends

`set_frontends(on, attenuation_db)` powers all 16 frontends together; the attenuation (0-31 dB,
1 dB steps) is common to all of them. `set_attenuation(db)` changes it without touching the
signal path. Turning the frontends off also stops the internal source and returns to `through`.

## Internal cal source

* The LO is fixed at 11.8 GHz; the tone is set as an IF frequency, 50-1500 MHz.
* `cw(frequency_mhz)`: a continuous tone.
* `sweep(start_mhz, stop_mhz, sweep_time_s)`: a repeating sweep. The tone runs from `stop_mhz`
  down to `start_mhz` every `sweep_time_s` (1 us to 10 s), then restarts.
* `set_level(dbm)`: the level is the **external-cal input level that would put the same signal
  on the receivers**, at most -38 dBm. It is set in 0.25 dB steps from the unit's calibration
  table and holds across the IF band for CW. For a sweep, the level is set so no frequency in
  the band exceeds the request; the returned `level_min_dbm` is the lowest point.
* Settings made in another mode are kept and applied when `internal_cal` is selected.

Each unit has its own calibration file (equivalent level per IF frequency with no internal
attenuation). The level can only be set inside the calibrated frequency range.

## Requirements

RDS3 firmware 3.0.0-rc3 (Barracuda RF B4 with the /1 rework). Python 3.10+, `protobuf` 4.25+.
One controller at a time: settings made by another program are not tracked.
