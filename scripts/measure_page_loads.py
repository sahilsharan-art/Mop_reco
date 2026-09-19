"""Measured page-load cost for the three views with a full month loaded.

Times the exact database work each page does, which is what a page load costs
once Streamlit has rendered its shell. Run from the project root:
    python scripts/measure_page_loads.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.db import Exception_, R1BillLine, ReconciliationResult, Run, get_session, init_db

session = get_session(init_db())


def timed(label, fn, repeat=3):
    """Best of `repeat` — a page load is what the user waits for, not an average
    polluted by first-touch disk caching."""
    times = []
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    print(f"  {label:<52} {min(times)*1000:>8.0f} ms   (n={result if isinstance(result, int) else '—'})")
    return min(times)


run = session.query(Run).order_by(Run.started_at.desc()).first()
print(f"run #{run.id}  {run.started_at:%Y-%m-%d %H:%M}  status={run.status}")
detail_total = session.query(R1BillLine).filter_by(run_id=run.id).count()
print(f"full month loaded: {detail_total:,} bill-line rows\n")

print("SUMMARY view")
total = 0.0
total += timed("run header (latest run lookup)",
               lambda: session.query(Run).order_by(Run.started_at.desc()).first() and 1)
total += timed("r1 summary + control totals (from run.notes)",
               lambda: len(json.loads(run.notes)))
total += timed("store x date result rows",
               lambda: session.query(ReconciliationResult)
               .filter_by(run_id=run.id, reconciliation="R1").count())


def summary_rows():
    rows = (session.query(ReconciliationResult)
            .filter_by(run_id=run.id, reconciliation="R1").all())
    return len(rows)


total += timed("  ...materialising those rows for the table", summary_rows)
print(f"  {'SUMMARY TOTAL':<52} {total*1000:>8.0f} ms\n")

print("TRANSACTIONS view (page 1, 50 rows)")
t_total = 0.0
t_total += timed("distinct business_date / status / mop for filters",
                 lambda: (len(session.query(R1BillLine.business_date).filter_by(run_id=run.id).distinct().all())
                          + len(session.query(R1BillLine.status).filter_by(run_id=run.id).distinct().all())
                          + len(session.query(R1BillLine.mop).filter_by(run_id=run.id).distinct().all())))
t_total += timed("count(*) for the pager",
                 lambda: session.query(R1BillLine).filter_by(run_id=run.id).count())
t_total += timed("fetch one 50-row page (ordered, offset 0)",
                 lambda: len(session.query(R1BillLine).filter_by(run_id=run.id)
                             .order_by(R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no)
                             .limit(50).offset(0).all()))
t_total += timed("fetch a deep page (offset 500,000)",
                 lambda: len(session.query(R1BillLine).filter_by(run_id=run.id)
                             .order_by(R1BillLine.business_date, R1BillLine.store, R1BillLine.bill_no)
                             .limit(50).offset(500_000).all()))
t_total += timed("filtered: one date + status, count + page",
                 lambda: (session.query(R1BillLine)
                          .filter_by(run_id=run.id, business_date="2026-07-20", status="Tier 1").count(),
                          len(session.query(R1BillLine)
                              .filter_by(run_id=run.id, business_date="2026-07-20", status="Tier 1")
                              .limit(50).all()))[0])
print(f"  {'TRANSACTIONS TOTAL (typical first load)':<52} {t_total*1000:>8.0f} ms\n")

print("EXCEPTIONS view (open only)")
e_total = 0.0
e_total += timed("distinct type / status for filters",
                 lambda: (len(session.query(Exception_.type).distinct().all())
                          + len(session.query(Exception_.status).distinct().all())))
e_total += timed("all open exceptions, ordered",
                 lambda: len(session.query(Exception_)
                             .filter(Exception_.status.in_(("open", "under_investigation")))
                             .order_by(Exception_.business_date, Exception_.type).all()))
print(f"  {'EXCEPTIONS TOTAL':<52} {e_total*1000:>8.0f} ms")

session.close()
