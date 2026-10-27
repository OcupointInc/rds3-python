"""RDS3 example: every control the library offers, in order."""
import time

from rds3 import RDS3, Mode

rds3 = RDS3("192.168.1.249")          # the RDS3's IP address

# Frontends: power all 16 on (they start at 31 dB attenuation), then set the attenuation
# (0-31 dB, common to all channels).
rds3.set_frontend_power(True)
rds3.set_attenuation(0)

# Through: the receivers see their antenna inputs.
rds3.set_mode(Mode.THROUGH)

# External cal: the receivers see the EXTERNAL CAL input.
rds3.set_mode(Mode.EXTERNAL_CAL)

# Internal cal: turns on the internal cal source and routes it to all 16 receivers.
rds3.set_mode(Mode.INTERNAL_CAL)

# Power level is the internal cal source's output, -16.2 to -47.95 dBm. The cal path has about
# 28 dB of insertion loss, so -20 dBm here is about -48 dBm at each receiver input.

# A CW tone at 500 MHz, -20 dBm.
rds3.cw(500, level_dbm=-20)
time.sleep(5)

# A repeating sweep from 400 to 650 MHz every 0.5 s, -30 dBm.
rds3.sweep(400, 650, sweep_time_s=0.5, level_dbm=-30)
time.sleep(5)

print(rds3.status())

# Back to the antenna inputs (turns the internal cal source off), then frontends off.
rds3.set_mode(Mode.THROUGH)
rds3.off()
rds3.close()
