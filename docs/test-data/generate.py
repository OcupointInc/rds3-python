"""Regenerate plots and derived CSVs: python docs/test-data/generate.py.

Requires matplotlib (only for this documentation script).
"""
import csv
import json
import math
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
CHANNELS = set(range(1, 17))


def read_csv(name):
    with (ROOT / name).open(newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(name, fields, rows):
    with (ROOT / name).open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)


cal = json.loads((ROOT / "fixture_cal.json").read_text())
metadata = json.loads((ROOT / "metadata.json").read_text())
freq, gain = cal["frequency_mhz"], cal["s21_db"]
assert len(freq) == len(gain) and all(a < b for a, b in zip(freq, freq[1:]))


def fixture_at(mhz):
    assert freq[0] <= mhz <= freq[-1], "Fixture correction must not extrapolate"
    k = min(max(bisect_left(freq, mhz), 1), len(freq) - 1)
    return gain[k - 1] + (gain[k] - gain[k - 1]) * (mhz - freq[k - 1]) / (freq[k] - freq[k - 1])


through = defaultdict(list)
for row in read_csv("through_path.csv"):
    channel, mhz, raw = int(row["channel"]), float(row["frequency_mhz"]), float(row["s21_raw_db"])
    assert math.isfinite(raw)
    through[channel].append((mhz, raw, raw - fixture_at(mhz)))
assert set(through) == CHANNELS
for points in through.values():
    points.sort()
    assert len(points) == 201 and len({p[0] for p in points}) == 201
write_csv("through_path_corrected.csv", ["channel", "frequency_mhz", "s21_raw_db", "fixture_corrected_s21_db"],
          ([channel, mhz, raw, round(corrected, 6)]
           for channel in sorted(through) for mhz, raw, corrected in through[channel]))

passes = defaultdict(dict)
for row in read_csv("internal_cal_raw.csv"):
    n, channel, mhz = int(row["pass"]), int(row["channel"]), float(row["if_mhz"])
    key = (channel, mhz)
    assert key not in passes[n], "Duplicate point in internal-cal pass"
    assert math.isfinite(float(row["power_dbm"]))
    passes[n][key] = float(row["power_dbm"])
expected = {(ch, mhz) for ch in CHANNELS for mhz in metadata["internal_cal"]["points_mhz"]}
complete = sorted(n for n, points in passes.items() if set(points) == expected)
assert complete, "No complete 16-channel calibration passes"
internal = defaultdict(list)
for channel, mhz in sorted(expected):
    values = [passes[n][channel, mhz] for n in complete]
    internal[channel].append((mhz, median(values), min(values), max(values)))
write_csv("internal_cal_summary.csv", ["channel", "frequency_mhz", "median_dbm", "min_dbm", "max_dbm", "complete_passes"],
          ([channel, mhz, value, low, high, len(complete)]
           for channel in sorted(internal) for mhz, value, low, high in internal[channel]))

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.spines.top": False,
                     "axes.spines.right": False, "savefig.facecolor": "white"})
colors = plt.get_cmap("tab20")


def decorate(ax, title, ylabel):
    ax.set_title(title, loc="left", fontsize=13, pad=12)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.2)


fig, axes = plt.subplots(2, 1, figsize=(11, 8))
for channel, points in sorted(through.items()):
    axes[0].plot([p[0] for p in points], [p[1] for p in points], color=colors(channel - 1), label=f"CH{channel}")
    band = [p for p in points if 400 <= p[0] <= 650]
    axes[1].plot([p[0] for p in band], [p[2] for p in band], color=colors(channel - 1))
decorate(axes[0], "Through path — measured S21, 10–1000 MHz", "S21 (dB), raw")
decorate(axes[1], "400–650 MHz — representative switch-fixture loss removed", "S21 (dB), corrected")
axes[0].set_xlim(10, 1000)
axes[1].set_xlim(400, 650)
axes[0].legend(ncol=8, loc="lower center", bbox_to_anchor=(0.5, 1.16), fontsize=9, frameon=False)
fig.suptitle("RDS3 · all 16 channels · frontend attenuation 0 dB · 26 Oct 2026", y=0.995, fontsize=14)
fig.tight_layout(rect=(0, 0, 1, 0.96), h_pad=2)
fig.savefig(ROOT / "through_path.png", dpi=160)
plt.close(fig)

fig, ax = plt.subplots(figsize=(11, 5.5))
for channel, points in sorted(internal.items()):
    ax.plot([p[0] for p in points], [p[1] for p in points], color=colors(channel - 1), label=f"CH{channel}")
decorate(ax, f"Barracuda internal cal — median of {len(complete)} complete passes", "Power at analyzer (dBm)")
ax.set_xlim(400, 650)
ax.legend(ncol=8, loc="lower center", bbox_to_anchor=(0.5, 1.14), fontsize=9, frameon=False)
fig.suptitle("RDS3 · all 16 channels · frontend and Barracuda attenuation 0 dB · 27 Oct 2026", y=0.99, fontsize=13)
fig.text(0.5, 0.015, "Includes output switch, divider and cable loss. Final partial pass excluded; no fixture correction applied.",
         ha="center", fontsize=10, color="#555555")
fig.tight_layout(rect=(0, 0.04, 1, 0.95))
fig.savefig(ROOT / "internal_cal.png", dpi=160)
plt.close(fig)
print(f"Through path: {sum(map(len, through.values()))} points across 16 channels.")
print(f"Internal cal: complete passes {complete}; excluded partial passes {sorted(set(passes) - set(complete))}.")
