# rds3: Python control for the RDS3 receiver

Controls the signal path, the 16 frontends and the internal calibration source of an RDS3 over
Ethernet (TCP port 5000).

```python
from rds3 import RDS3, Mode

with RDS3("192.168.1.249") as r:
    r.set_frontend_power(True)                          # power on all 16 frontends (starts at 31 dB)
    r.set_attenuation(10)                          # frontend attenuation, 0-31 dB
    r.set_mode(Mode.INTERNAL_CAL)                  # THROUGH | INTERNAL_CAL | EXTERNAL_CAL
    # Level = Barracuda output, -16.2 to -47.95 dBm. The cal path has about 28 dB of insertion
    # loss, so -20 dBm here puts about -48 dBm at each receiver input.
    r.cw(500, level_dbm=-20)                       # CW tone at 500 MHz
    r.sweep(400, 650, sweep_time_s=0.5, level_dbm=-20)   # or a repeating sweep
    print(r.status())
    r.set_mode(Mode.THROUGH)
    r.off()                                        # frontends and cal source off
```

## Modes

| Mode | Receivers see | Internal cal source and its clocks |
|---|---|---|
| `Mode.THROUGH` | their antenna inputs | off |
| `Mode.EXTERNAL_CAL` | the EXTERNAL CAL input, all 16 channels | off |
| `Mode.INTERNAL_CAL` | the internal source, all 16 channels | on |

Changing mode never routes two sources at once: the receivers return to their antenna inputs
while the internal source starts, and it is routed only once it is running.

## Frontends

`set_frontend_power(on)` powers all 16 frontends on or off together. Powering on starts at 31 dB
attenuation. `set_attenuation(db)` sets the attenuation (0-31 dB, 1 dB steps, common to all 16
channels) on frontends that are on, without touching the signal path. Turning the frontends off
also stops the internal source and returns to through.

## Internal cal source

* The LO is fixed at 11.8 GHz; the tone is set as an IF frequency, 50-1500 MHz.
* `cw(frequency_mhz, level_dbm=None)`: a continuous tone.
* `sweep(start_mhz, stop_mhz, sweep_time_s, level_dbm=None)`: a repeating sweep. The tone runs from `stop_mhz`
  down to `start_mhz` every `sweep_time_s` (1 us to 10 s), then restarts.
* `level_dbm` (or `set_level(dbm)` on its own): the **Barracuda's output level**, -16.2 dBm (full
  output) down to -47.95 dBm, in 0.25 dB steps of its internal attenuator. The cal path to each
  receiver has about 28 dB of insertion loss (`CAL_PATH_LOSS_DB`), so the receiver inputs see about
  28 dB less. The level is set from one average full-output figure (`FULL_LEVEL_DBM`, -16.2 dBm),
  so it is approximate: about 3 dB above the setting at 400 MHz and about 4.5 dB below at 650 MHz.
* Settings made in another mode are kept and applied when `internal_cal` is selected.

## Requirements

RDS3 firmware 3.0.0-rc3 (Barracuda RF B4 with the /1 rework). Python 3.10+, `protobuf` 4.25+.
One controller at a time: settings made by another program are not tracked.
