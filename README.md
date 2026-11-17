# Factory Traceability & Connectivity Analytics Pipeline

An end-to-end pipeline that simulates multi-station factory scan events, loads them into SQLite, and uses SQL checks to find:

- **Connectivity gaps:** a unit reached a later station but has no scan at an earlier one.
- **Integrity issues:** more than one scan for the same unit at the same station.
- **Defect hotspots:** machines whose fail rate stands out against their station peers.
- **Cycle-time outliers:** scans more than three standard deviations from their station's mean.

## Run it

```bash
pip install -r requirements.txt
python run_pipeline.py
```

## Layout

```
simulate.py              # six-station line simulator with injected faults
run_pipeline.py          # load to SQLite, run SQL checks, write outputs
sql/
  01_missing_scans.sql       # connectivity gaps (expected vs present stations)
  02_duplicate_scans.sql     # repeated scans per unit and station
  03_machine_defect_rates.sql# first-scan fail rates by machine (window function)
  04_station_health.sql      # expected vs scanned units per station
templates/dashboard.html # interactive dashboard template
outputs/                 # summary.json, CSV tables, dashboard.png, dashboard.html
data/                    # scan_events.csv and factory.db
```

## Results (simulated, seed 7)

| Measure | Value |
|---|---|
| Units / scan events | 4,000 / 22,580 |
| Missing scans detected | 222 (injected 222) |
| Duplicate scans detected | 165 (injected 165) |
| Cycle-time outliers (|z| > 3) | 162, of which 110 on RF-02 |
| Root-cause machine | PLC-03 at PLACE: 9.8% fail rate, about 5x its peers |

The SQL checks recover every injected fault. The simulator plants mid-line gaps only, since a missing final scan leaves no trace in the data. A gap at the last station is therefore undetectable by design, and the README calls this out rather than hiding it.

## Method notes

- **Missing scans:** a unit's expected stations run from the first station through its furthest scanned station. Any expected station with no event is a gap. Failed units stop at the failing station, so later stations are not expected.
- **Duplicates:** grouped by unit and station, with the gap between first and last scan reported in seconds. In the simulation, re-sends land 1–4 seconds after the original.
- **Defect rates:** computed on the first scan per unit and station (window function), so re-sends do not inflate counts.
- **Peer comparison:** each machine's fail rate divided by the pooled fail rate of the other machines at its station.
- **Outliers:** z-score within station, computed in pandas.

All data is simulated; the fault rates and root-cause machine are planted for demonstration.
