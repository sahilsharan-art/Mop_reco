"""Run the one-date-at-a-time R1 engine on the REAL warehouse, compare it with a
completed run's persisted rows (read-only) and report time + peak memory.

    python scripts/r1_chunked_check.py --from 2026-04-01 --to 2026-08-31 --vs-run 11

Peak memory is the process's peak working set (psutil), measured in a fresh
interpreter per invocation so 1-month and 6-month figures are comparable.
"""

from __future__ import annotations

import argparse
import tracemalloc
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import r1_chunked as ck  # noqa: E402

WH = ROOT / "warehouse"


def _dates(source: str, lo=None, hi=None) -> dict:
    out = {}
    for p in sorted((WH / source).glob("2???-??-??.parquet")):
        d = date.fromisoformat(p.stem)
        if (lo is None or d >= lo) and (hi is None or d <= hi):
            out[p.stem] = str(p)
    return out


def peak_mb() -> float:
    return psutil.Process().memory_info().peak_wset / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="lo", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="hi", required=True, type=date.fromisoformat)
    ap.add_argument("--vs-run", type=int)
    ap.add_argument("--chunk-rows", type=int, default=None,
                    help="cap on digital lines per chunk (default: adaptive from available RAM; 0 = whole months)")
    ap.add_argument("--trace", action="store_true", help="tracemalloc the python heap (slows the run ~3x)")
    ap.add_argument("--pool-window", type=int, default=None,
                    help="restrict the pool to bill range +/- N days (memory/time comparison only; "
                         "a restricted pool cannot be compared with a run that saw the whole pool)")
    args = ap.parse_args()

    bill_files = _dates("pos_bill_wise", args.lo, args.hi)
    # The pool is the WHOLE warehouse for each gateway: a reference is
    # date-agnostic. Memory must still not depend on it - only on one date.
    from datetime import timedelta
    plo = args.lo - timedelta(days=args.pool_window) if args.pool_window is not None else None
    phi = args.hi + timedelta(days=args.pool_window) if args.pool_window is not None else None
    pool_files = {"paytm": _dates("paytm_settlement", plo, phi), "axis": _dates("axis_edc", plo, phi),
                  "bajaj": _dates("bajaj", plo, phi)}
    print(f"{len(bill_files)} bill dates {args.lo}..{args.hi}; pool files "
          f"{ {g: len(v) for g, v in pool_files.items()} }; peak at start {peak_mb():.0f} MB",
          flush=True)

    c = (sqlite3.connect(f"file:{ROOT / 'db' / 'reco.sqlite'}?mode=ro", uri=True)
         if args.vs_run else None)
    cols = ["matched", "match_tier", "matched_gateway", "matched_txn_ref", "date_diff_days",
            "match_note", "payment_mode"]
    diff = {k: 0 for k in cols}
    seen = {"lines": 0, "missing_in_db": 0, "missing_in_chunked": 0, "unmatched": 0}

    def on_day(d, frame):
        """Streamed: compare this one date with the run's persisted rows, keep counts only."""
        seen["unmatched"] += int((~frame["matched"]).sum())
        if c is None:
            return
        db = pd.read_sql_query(
            "select bill_no, line_seq, matched, match_tier, matched_gateway, matched_txn_ref, "
            "date_diff_days, match_note, payment_mode from r1_bill_lines "
            "where run_id=? and business_date=?", c, params=(args.vs_run, d))
        mine = frame[["bill_no", "line_seq", "matched", "match_tier", "matched_gateway",
                      "matched_txn_ref", "date_diff_days", "match_note",
                      "matched_payment_mode"]].rename(columns={"matched_payment_mode": "payment_mode"})
        mine["matched"] = mine["matched"].astype(int)
        mine["line_seq"] = mine["line_seq"].astype(float)
        db["line_seq"] = db["line_seq"].astype(float)
        j = db.merge(mine, on=["bill_no", "line_seq"], how="outer", suffixes=("_db", "_new"),
                     indicator=True)
        seen["lines"] += int((j["_merge"] == "both").sum())
        seen["missing_in_chunked"] += int((j["_merge"] == "left_only").sum())
        seen["missing_in_db"] += int((j["_merge"] == "right_only").sum())
        both = j[j["_merge"] == "both"]
        for col in cols:
            x, y = both[f"{col}_db"], both[f"{col}_new"]
            xn, yn = x.isna().to_numpy(), y.isna().to_numpy()
            xo, yo = x.astype(object).to_numpy(), y.astype(object).to_numpy()
            eq = np.where(xn | yn, xn == yn, xo == yo).astype(bool)
            diff[col] += int((~eq).sum())

    t = time.perf_counter()
    if args.trace:
        tracemalloc.start()
    phases: dict = {}

    def on_progress(msg):
        if msg.startswith("Matching in"):
            print(f"  {msg}", flush=True)
        # cumulative peak working set at the LAST message of each phase
        phase = ("index" if msg.startswith(("Indexing", "Pool indexed")) else
                 "pass A (Tier 1)" if msg.startswith("Tier 1") else "pass B (Tier 2 + persist)")
        phases[phase] = (round(peak_mb()), round(psutil.Process().memory_info().wset / 1e6), msg)

    res = ck.run_r1_chunked(bill_files, pool_files, on_day=on_day, on_progress=on_progress,
                            chunk_rows=args.chunk_rows)
    for ph, (pk, cur, msg) in phases.items():
        print(f"  phase end: {ph:26s} peak so far {pk:5d} MB, current {cur:5d} MB   [{msg}]", flush=True)
    secs = time.perf_counter() - t
    n = res.summary["digital_bill_lines"]
    import pyarrow as pa
    print(f"CHUNKED: {n:,} lines in {secs:,.1f}s = {n / secs:,.0f} bills/s; "
          f"PEAK WORKING SET {peak_mb():,.0f} MB (arrow pool peak "
          f"{pa.default_memory_pool().max_memory() / 1e6:,.0f} MB, python heap peak "
          f"{(tracemalloc.get_traced_memory()[1] / 1e6 if args.trace else float('nan')):,.0f} MB)", flush=True)
    print(f"  summary: { {k: v for k, v in res.summary.items() if k != 'unmapped_txns_by_gateway'} }",
          flush=True)
    rc = 0
    if c is not None:
        print(f"vs run #{args.vs_run}: compared {seen['lines']:,} lines; "
              f"only-in-db {seen['missing_in_chunked']}, only-in-chunked {seen['missing_in_db']}",
              flush=True)
        for col in cols:
            print(f"  {col:18s}: {diff[col]:,} differing", flush=True)
        rc |= int(any(diff.values()) or seen["missing_in_chunked"] or seen["missing_in_db"])
        um = res.unmapped_txns
        eng = set(um["gateway"] + "|" + um["txn_ref"].astype(str))
        persisted = {r[0] for r in c.execute(
            "select exception_key from exceptions where type='unmapped_gateway_transaction' "
            "and last_seen_run_id=?", (args.vs_run,))}
        print(f"unmapped: chunked {len(eng):,} vs persisted {len(persisted):,}; "
              f"only-chunked {len(eng - persisted)}, only-persisted {len(persisted - eng)}", flush=True)
        rc |= int(eng != persisted and args.pool_window is None)
        lk = {f"paytm|{x}" for x in res.linkage_exceptions["transaction_id"]}
        lp = {r[0] for r in c.execute(
            "select exception_key from exceptions where type='original_txn_not_found' "
            "and last_seen_run_id=?", (args.vs_run,))}
        print(f"original_txn_not_found: chunked {len(lk)} vs persisted {len(lp)}", flush=True)
        rc |= int(lk != lp and args.pool_window is None)
    print(f"unmatched digital lines: {seen['unmatched']:,}", flush=True)
    print("RESULT:", "IDENTICAL" if rc == 0 else "DIFFERENCES FOUND", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
