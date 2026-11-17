"""
simulate.py
Generates multi-station factory scan events for a six-station SMT-style line.

Injected faults (the ground truth is returned so the SQL checks can be validated):
  * Connectivity gaps: a mid-line scan never reaches the database (the unit
    passes later stations, so the gap is detectable).
  * Integrity issues: a scan is re-sent 1-4 seconds later (duplicate row).
  * Root-cause defect pattern: machine PLC-03 fails ~4.5x more often.
  * Cycle-time drift: machine RF-02 is intermittently slow (outliers).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

STATIONS = [("SOLDER", 10), ("PLACE", 20), ("REFLOW", 30), ("AOI", 40), ("FTEST", 50), ("PACK", 60)]
MACHINES = {
    "SOLDER": ["SP-01", "SP-02"],
    "PLACE": ["PLC-01", "PLC-02", "PLC-03"],
    "REFLOW": ["RF-01", "RF-02"],
    "AOI": ["AOI-01"],
    "FTEST": ["FT-01", "FT-02"],
    "PACK": ["PK-01"],
}
BASE_FAIL = {"SOLDER": 0.02, "PLACE": 0.02, "REFLOW": 0.015, "AOI": 0.012, "FTEST": 0.02, "PACK": 0.004}
CYCLE_MEAN = {"SOLDER": 42, "PLACE": 65, "REFLOW": 118, "AOI": 30, "FTEST": 88, "PACK": 25}
CYCLE_SD = {"SOLDER": 4, "PLACE": 6, "REFLOW": 9, "AOI": 3, "FTEST": 7, "PACK": 2.5}
ROOT_CAUSE_MACHINE, ROOT_CAUSE_MULT = "PLC-03", 4.5
DRIFT_MACHINE, DRIFT_PROB, DRIFT_FACTOR = "RF-02", 0.06, 1.9
MISSING_RATE, DUPLICATE_RATE = 0.012, 0.008
START = datetime(2026, 7, 1, 6, 0, 0)


def simulate(n_units: int = 4000, days: int = 30, seed: int = 7):
    rng = np.random.default_rng(seed)
    arrivals = np.sort(rng.uniform(0, days * 86400, n_units))
    rows = []
    for u, offset in enumerate(arrivals):
        unit_id = f"U{u + 1:05d}"
        t = START + timedelta(seconds=float(offset))
        for station, seq in STATIONS:
            machine = str(rng.choice(MACHINES[station]))
            cyc = rng.normal(CYCLE_MEAN[station], CYCLE_SD[station])
            if machine == DRIFT_MACHINE and rng.random() < DRIFT_PROB:
                cyc *= DRIFT_FACTOR
            cyc = max(float(cyc), 1.0)
            scanned = t + timedelta(seconds=cyc)
            mult = ROOT_CAUSE_MULT if machine == ROOT_CAUSE_MACHINE else 1.0
            fail = rng.random() < BASE_FAIL[station] * mult
            rows.append({"unit_id": unit_id, "station": station, "seq": seq, "machine": machine,
                         "scanned_at": scanned, "cycle_seconds": round(cyc, 1),
                         "result": "FAIL" if fail else "PASS"})
            t = scanned + timedelta(seconds=float(rng.exponential(20)))
            if fail:  # failed units leave the line
                break

    df = pd.DataFrame(rows).sort_values(["unit_id", "seq"]).reset_index(drop=True)
    df["scanned_at"] = pd.to_datetime(df["scanned_at"])

    # Connectivity gaps: drop mid-line scans only (last scan of a unit is undetectable).
    is_last = df.groupby("unit_id").cumcount(ascending=False) == 0
    drop = (~is_last) & (rng.random(len(df)) < MISSING_RATE)
    # Integrity issues: re-sent scans, 1-4 seconds later.
    dup = (rng.random(len(df)) < DUPLICATE_RATE) & ~drop
    dupes = df[dup].copy()
    dupes["scanned_at"] = dupes["scanned_at"] + pd.to_timedelta(rng.uniform(1, 4, len(dupes)), unit="s")

    out = pd.concat([df[~drop], dupes], ignore_index=True).sort_values("scanned_at").reset_index(drop=True)
    out.insert(0, "scan_id", np.arange(1, len(out) + 1))
    truth = {
        "units": int(n_units),
        "missing_scans_injected": int(drop.sum()),
        "duplicate_scans_injected": int(dup.sum()),
        "root_cause_machine": ROOT_CAUSE_MACHINE,
        "drift_machine": DRIFT_MACHINE,
    }
    return out, truth
