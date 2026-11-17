"""
run_pipeline.py
Simulate scan events -> load into SQLite -> run SQL integrity and defect
checks -> write CSV/JSON outputs, a matplotlib dashboard, and an HTML dashboard.

Run with:  python run_pipeline.py
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from simulate import STATIONS, simulate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
DATA, OUT, SQL = ROOT / "data", ROOT / "outputs", ROOT / "sql"
DB = DATA / "factory.db"
DATA.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)


def build_db(events: pd.DataFrame) -> sqlite3.Connection:
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    pd.DataFrame(STATIONS, columns=["station", "seq"]).to_sql("stations", con, index=False)
    events.to_sql("scan_events", con, index=False)
    con.execute("CREATE INDEX idx_unit_station ON scan_events(unit_id, station)")
    con.commit()
    return con


def run_sql(con: sqlite3.Connection, name: str) -> pd.DataFrame:
    return pd.read_sql_query((SQL / f"{name}.sql").read_text(), con)


def main() -> dict:
    events, truth = simulate()
    events_out = events.copy()
    events_out["scanned_at"] = events_out["scanned_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
    events_out.to_csv(DATA / "scan_events.csv", index=False)
    con = build_db(events_out)

    # ---- SQL checks ----
    missing = run_sql(con, "01_missing_scans")
    dupes = run_sql(con, "02_duplicate_scans")
    machines = run_sql(con, "03_machine_defect_rates")
    health = run_sql(con, "04_station_health")
    con.close()

    # ---- Traceability health per station ----
    health["coverage"] = health["scanned_units"] / health["expected_units"]
    miss_by_station = missing.groupby("station").size().rename("missing")
    dup_by_station = dupes.groupby("station").apply(lambda g: int((g["scan_count"] - 1).sum())).rename("duplicates")
    health = health.set_index("station").join(miss_by_station, how="left").join(dup_by_station, how="left")
    health[["missing", "duplicates"]] = health[["missing", "duplicates"]].fillna(0).astype(int)
    health = health.reset_index()
    health["fail_rate"] = health["fail_rows"] / health["scan_rows"]

    # ---- Defect hotspots: compare each machine to its station's other machines ----
    station_tot = machines.groupby("station")[["fails", "units"]].sum()
    machines = machines.merge(station_tot, left_on="station", right_index=True, suffixes=("", "_st"))
    def ratio(row):
        others_fails = row["fails_st"] - row["fails"]
        others_units = row["units_st"] - row["units"]
        other_rate = others_fails / others_units if others_units else np.nan
        return row["fail_rate"] / other_rate if other_rate else np.nan
    machines["ratio_vs_peers"] = machines.apply(ratio, axis=1).round(2)
    machines = machines.drop(columns=["fails_st", "units_st"]).sort_values("fail_rate", ascending=False)

    # ---- Cycle-time outliers (z-score per station) ----
    ev = events.copy()
    stats = ev.groupby("station")["cycle_seconds"].agg(["mean", "std"])
    ev = ev.join(stats, on="station")
    ev["z"] = (ev["cycle_seconds"] - ev["mean"]) / ev["std"]
    outliers = ev[ev["z"].abs() > 3].sort_values("z", key=np.abs, ascending=False)
    cyc = ev.groupby("station")["cycle_seconds"].describe(percentiles=[0.05, 0.5, 0.95])
    cyc = cyc.reindex([s for s, _ in STATIONS])
    cyc_out_counts = outliers.groupby("station").size()
    cycle_stats = [
        {"station": s,
         "p5": round(float(r["5%"]), 1), "p50": round(float(r["50%"]), 1), "p95": round(float(r["95%"]), 1),
         "mean": round(float(r["mean"]), 1), "outliers": int(cyc_out_counts.get(s, 0))}
        for s, r in cyc.iterrows()
    ]

    # ---- Validation against planted faults ----
    detected_missing = int(len(missing))
    detected_dup_rows = int((dupes["scan_count"] - 1).sum())
    validation = {
        "missing_injected": truth["missing_scans_injected"],
        "missing_detected": detected_missing,
        "duplicates_injected": truth["duplicate_scans_injected"],
        "duplicates_detected": detected_dup_rows,
    }

    # ---- Persist tables ----
    health.to_csv(OUT / "station_health.csv", index=False)
    machines.to_csv(OUT / "machine_defects.csv", index=False)
    missing.to_csv(OUT / "missing_scans.csv", index=False)
    dupes.to_csv(OUT / "duplicate_scans.csv", index=False)
    outliers[["scan_id", "unit_id", "station", "machine", "cycle_seconds", "z"]].round(2).to_csv(
        OUT / "cycle_outliers.csv", index=False)

    kpi = {
        "units": truth["units"],
        "scan_events": int(len(events_out)),
        "missing": detected_missing,
        "duplicates": detected_dup_rows,
        "outliers": int(len(outliers)),
        "root_cause_machine": truth["root_cause_machine"],
        "root_cause_rate": float(machines.loc[machines.machine == truth["root_cause_machine"], "fail_rate"].iloc[0]),
        "overall_fail_rate": float(events.groupby("unit_id").apply(lambda g: (g.result == "FAIL").any()).mean()),
    }
    summary = {
        "kpi": kpi,
        "validation": validation,
        "health": health[["station", "seq", "expected_units", "scanned_units", "coverage",
                          "missing", "duplicates", "fail_rate"]].to_dict("records"),
        "machines": machines[["station", "seq", "machine", "units", "fails", "fail_rate", "ratio_vs_peers"]]
            .to_dict("records"),
        "cycle": cycle_stats,
        "outliers": outliers.head(20)[["unit_id", "station", "machine", "cycle_seconds", "z"]]
            .round(2).to_dict("records"),
        "missing": missing.head(60).to_dict("records"),
        "duplicates": dupes.head(60).to_dict("records"),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    render_png(health, machines, ev, truth)
    render_html(summary)
    return summary


def render_png(health, machines, ev, truth):
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    fig.suptitle("Factory Traceability & Connectivity Health (July 2026)", fontsize=15, weight="bold")

    ax = axes[0, 0]
    cov = health["coverage"] * 100
    colors = ["#2a9d8f" if c >= 99 else "#e76f51" for c in cov]
    ax.bar(health["station"], cov, color=colors)
    ax.set_ylim(90, 100.5)
    ax.set_title("Traceability coverage by station (%)")
    for i, (c, m, d) in enumerate(zip(cov, health["missing"], health["duplicates"])):
        ax.text(i, c + 0.15, f"{c:.1f}%\n-{m} / +{d}", ha="center", fontsize=8)
    ax.set_ylabel("scanned / expected units")

    ax = axes[0, 1]
    mm = machines.sort_values("fail_rate")
    bar_colors = ["#e76f51" if m == truth["root_cause_machine"] else "#8ab6d6" for m in mm["machine"]]
    ax.barh(mm["machine"], mm["fail_rate"] * 100, color=bar_colors)
    ax.set_title("Defect rate by machine (%)")
    for y, (r, rat) in enumerate(zip(mm["fail_rate"] * 100, mm["ratio_vs_peers"])):
        ax.text(r + 0.1, y, f"{r:.1f}%  ({rat}x peers)", va="center", fontsize=8)
    ax.set_xlim(0, mm["fail_rate"].max() * 100 * 1.45)

    ax = axes[1, 0]
    data = [ev.loc[ev.station == s, "cycle_seconds"].values for s, _ in STATIONS]
    ax.boxplot(data, tick_labels=[s for s, _ in STATIONS], showfliers=True,
               flierprops=dict(marker="o", markersize=3, markerfacecolor="#e76f51", markeredgecolor="none"))
    ax.set_title("Cycle time by station (s), outliers in red")
    ax.set_yscale("log")

    ax = axes[1, 1]
    ax.axis("off")
    ax.set_title("Integrity summary")
    lines = [
        f"Units simulated: {truth['units']:,}",
        f"Scan events: {len(ev):,}",
        f"Missing scans detected: {int(health['missing'].sum()):,}",
        f"Duplicate scans detected: {int(health['duplicates'].sum()):,}",
        f"Cycle-time outliers (|z| > 3): {len(ev[ev['z'].abs() > 3]):,}",
        "",
        f"Root-cause machine: {truth['root_cause_machine']}",
        f"  fail rate {machines.loc[machines.machine == truth['root_cause_machine'], 'fail_rate'].iloc[0]*100:.1f}% "
        f"vs {machines.loc[machines.machine != truth['root_cause_machine'], 'fail_rate'].median()*100:.1f}% median peer",
        f"Drift machine: {truth['drift_machine']} (intermittent slow cycles)",
    ]
    ax.text(0, 0.95, "\n".join(lines), va="top", family="monospace", fontsize=10)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(OUT / "dashboard.png", dpi=150)
    plt.close(fig)


def render_html(summary):
    tpl = (ROOT / "templates" / "dashboard.html").read_text()
    html = tpl.replace("__DATA__", json.dumps(summary, default=str))
    (OUT / "dashboard.html").write_text(html)


if __name__ == "__main__":
    s = main()
    k = s["kpi"]
    print(f"Units {k['units']:,} | scans {k['scan_events']:,} | missing {k['missing']} | "
          f"duplicates {k['duplicates']} | outliers {k['outliers']}")
    print("Validation:", s["validation"])
    print(f"Root-cause machine {k['root_cause_machine']} fail rate {k['root_cause_rate']*100:.1f}%")
