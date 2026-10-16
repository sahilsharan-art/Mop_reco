"""Fill `txn_day_agg` for a run that predates it, and prove it equals the slow calculation.

    python scripts/backfill_txn_day_agg.py [run_id] [--from 2026-04-01 --to 2026-08-31] [--no-verify]

Re-runs the chunked R1 engine over the warehouse Parquet (the same inputs the run matched -
proven identical to Run #11 by scripts/r1_chunked_check.py) WITHOUT persisting bill lines, takes
its per-(date, store, gateway) transaction totals, and writes them for `run_id` (default: the latest
completed run). Existing rows for that run are replaced. Then, unless --no-verify, compares the
Summary's Mapped / Refunded / Unmapped strip computed both ways over the range - the new
aggregate read and the original 3.7M-row pool read (slow: minutes, GBs) - and exits non-zero on
any difference in a count.

Adds rows to a derived table only; no source file, bill line or exception is touched.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import queries as q  # noqa: E402
from mop_reco import r1_chunked as ck  # noqa: E402
from mop_reco import runs  # noqa: E402
from mop_reco.db import Run, get_engine, get_session, init_db  # noqa: E402

WH = ROOT / "warehouse"


def per_date(source: str) -> dict:
    return {p.stem: str(p) for p in sorted((WH / source).glob("2???-??-??.parquet"))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", type=int)
    ap.add_argument("--from", dest="lo", type=date.fromisoformat, default=date(2026, 4, 1))
    ap.add_argument("--to", dest="hi", type=date.fromisoformat, default=date(2026, 8, 31))
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    engine = init_db(get_engine())
    session = get_session(engine)
    run = (session.get(Run, args.run_id) if args.run_id else
           session.query(Run).filter(Run.status == "completed").order_by(Run.id.desc()).first())
    if run is None or run.status != "completed":
        print("no such completed run")
        return 2
    print(f"run #{run.id} ({run.status}, started {run.started_at})", flush=True)

    t = time.perf_counter()
    result = ck.run_r1_chunked(per_date("pos_bill_wise"),
                               {"paytm": per_date("paytm_settlement"), "axis": per_date("axis_edc"),
                                "bajaj": per_date("bajaj")})
    print(f"engine pass: {time.perf_counter() - t:,.0f}s -> {len(result.txn_agg):,} aggregate rows", flush=True)
    n = runs._persist_txn_day_agg(session, run.id, result.txn_agg)
    print(f"wrote {n:,} txn_day_agg rows for run #{run.id}", flush=True)
    if args.no_verify:
        return 0

    t = time.perf_counter()
    fast = q.transaction_resolution_summary(session, args.lo, args.hi, None, run_id=run.id)
    t_fast = time.perf_counter() - t
    print(f"fast read: {t_fast:.3f}s", flush=True)
    t = time.perf_counter()
    slow = q.transaction_resolution_summary(session, args.lo, args.hi, None, run_id=None)
    print(f"slow original read: {time.perf_counter() - t:,.0f}s", flush=True)
    rc = 0
    for f in ("total_n", "mapped_n", "refunded_n", "refund_pending_n", "refund_released_n", "unmapped_n",
              "total_v", "mapped_v", "refunded_v", "unmapped_v"):
        a, b = getattr(fast, f), getattr(slow, f)
        same = (a == b) if f.endswith("_n") else abs(a - b) < 0.01
        rc |= int(not same)
        print(f"  {f:18s} fast {a:>18,.2f}  slow {b:>18,.2f}  {'OK' if same else 'DIFFERENT'}")
    print("RESULT:", "IDENTICAL" if rc == 0 else "DIFFERENCES FOUND")
    return rc


if __name__ == "__main__":
    sys.exit(main())
