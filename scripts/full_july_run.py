"""Full-July Phase 1 run with measured timings and peak memory.

Run from the project root:  python scripts/full_july_run.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import psutil

from mop_reco.config import load_config
from mop_reco.db import Exception_, R1BillLine, get_session, init_db
from mop_reco.runs import run_reconciliation

DATE_FROM = date(2026, 7, 1)
DATE_TO = date(2026, 7, 31)


class PeakMemory(threading.Thread):
    """Samples RSS while the run works — peak allocation is what matters here,
    not the value at the end, by which time everything has been freed."""

    def __init__(self, interval=0.25):
        super().__init__(daemon=True)
        self.proc = psutil.Process()
        self.interval = interval
        self.peak_mb = 0.0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            rss = self.proc.memory_info().rss / 1_048_576
            self.peak_mb = max(self.peak_mb, rss)
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=2)


def main():
    config = load_config()
    engine = init_db()
    session = get_session(engine)

    baseline_mb = psutil.Process().memory_info().rss / 1_048_576
    monitor = PeakMemory()
    monitor.start()

    t0 = time.perf_counter()
    outcome = run_reconciliation(config, session, date_from=DATE_FROM, date_to=DATE_TO,
                                 triggered_by="full-july-measurement")
    wall = time.perf_counter() - t0

    monitor.stop()

    print("\n" + "=" * 72)
    print("FULL JULY RUN")
    print("=" * 72)
    print(f"run_id                : {outcome['run_id']}")
    print(f"TOTAL WALL TIME       : {wall:.1f}s  ({wall/60:.1f} min)")
    print(f"baseline RSS          : {baseline_mb:.0f} MB")
    print(f"PEAK RSS              : {monitor.peak_mb:.0f} MB")
    print(f"peak above baseline   : {monitor.peak_mb - baseline_mb:.0f} MB")

    print("\n--- STAGE BREAKDOWN ---")
    for stage, secs in outcome["timings"].items():
        pct = secs / wall * 100
        print(f"  {stage:<24} {secs:>8.2f}s  ({pct:4.1f}%)")
    accounted = sum(outcome["timings"].values())
    print(f"  {'(unaccounted)':<24} {wall - accounted:>8.2f}s")

    print("\n--- ROWS PER SOURCE ---")
    for source, report in outcome["load_reports"].items():
        dates = (f"{min(report.business_dates)} .. {max(report.business_dates)}"
                 if report.business_dates else "—")
        print(f"  {source:<20} {report.rows_read:>10,}  status={report.status:<18} {dates}")
        print(f"  {'':<20} control_total={report.control_total:,.0f}")

    print("\n--- R1 RESULTS (full month) ---")
    for k, v in outcome["r1"].items():
        print(f"  {k:<26} {v}")

    print("\n--- EXCEPTION REGISTER ---")
    print(f"  sync: {outcome['exceptions']}")
    by_type = Counter(t for (t,) in session.query(Exception_.type).all())
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<34} {n:>7,}")
    print(f"  {'TOTAL':<34} {sum(by_type.values()):>7,}")

    by_status = Counter(s for (s,) in session.query(Exception_.status).all())
    print(f"  by status: {dict(by_status)}")

    detail_rows = session.query(R1BillLine).count()
    print(f"\n  r1_bill_lines rows persisted: {detail_rows:,}")

    db_mb = (ROOT / "db" / "reco.sqlite").stat().st_size / 1_048_576
    print(f"  db/reco.sqlite size: {db_mb:.0f} MB")

    session.close()

    (ROOT / "logs" / "full_july_metrics.json").write_text(
        json.dumps({
            "wall_seconds": wall,
            "peak_rss_mb": monitor.peak_mb,
            "baseline_rss_mb": baseline_mb,
            "timings": outcome["timings"],
            "r1": outcome["r1"],
            "exceptions_by_type": dict(by_type),
            "rows_per_source": {k: v.rows_read for k, v in outcome["load_reports"].items()},
            "detail_rows": detail_rows,
            "db_mb": db_mb,
        }, indent=2, default=str), encoding="utf-8")
    print("\nmetrics written to logs/full_july_metrics.json")


if __name__ == "__main__":
    main()
