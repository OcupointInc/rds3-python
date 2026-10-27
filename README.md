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

## Finding the RDS3 and setting its address

You don't need to know the RDS3's IP address or subnet. Connect it to the same switch as
your computer and run:

```
python -m rds3 discover
```

```
rds3 7a:c5:ba:d8:51:d6  169.254.81.214/16  DHCP (no server: fallback address)  firmware 3.0.0-rc4
```

This lists every RDS3 on that switch, whatever subnet it is set to. To give it a static
address on your network (it saves the setting and reboots, about 5 s):

```
python -m rds3 set-network 7a:c5:ba:d8:51:d6 --ip 10.20.30.40 --subnet 255.255.255.0 --gateway 10.20.30.1
```

`--dhcp` switches it back to DHCP. The same from Python:

```python
import rds3

for device in rds3.discover():
    print(device)
rds3.set_network("7a:c5:ba:d8:51:d6", ip="10.20.30.40", subnet="255.255.255.0", gateway="10.20.30.1")
r = rds3.RDS3("10.20.30.40")
```

* Discovery uses UDP broadcasts on port 5001. These reach every device on the same switch,
  whatever its subnet, but routers don't pass them on.
* With several network adapters, the request goes out of each one. `--interface IP` (or
  `interface_ip=`) uses only the adapter with that address.
* A new RDS3 uses DHCP. If no DHCP server answers within about a minute, it uses its last
  saved address, or a 169.254.x.y link-local address if it has none. It tries DHCP again
  when the cable is reconnected or it is power cycled.
* Firmware 3.0.0-rc4 or later is needed for this. Earlier firmware answers discovery only
  from its own subnet and has no `set-network`.

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

RDS3 firmware 3.0.0-rc3 or 3.0.0-rc4 (Barracuda RF B4 with the /1 rework); network setup needs rc4. Python 3.10+, `protobuf` 4.25+.
One controller at a time: settings made by another program are not tracked.
