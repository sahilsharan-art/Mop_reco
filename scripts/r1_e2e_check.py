"""End-to-end check of the chunked run against Run #11, in a SCRATCH database.

Runs the real `runs.run_reconciliation` (chunked R1, per-date persistence,
digital-mode aggregate, exception register, manual overlay) into a throwaway
SQLite file. Only two things are stubbed, and only because they cannot be
reproduced from today's source folders (a September PTC file, refreshed
masters and a re-saved pickup-location master all post-date Run #11):

  * the four R1 ingests - replaced by reports that point at the warehouse
    Parquet Run #11's ingest wrote (no source file is opened);
  * the cash-side aggregates (store_day_agg / mop_day_agg) - not R1.

The real database is opened read-only and never written.

    python scripts/r1_e2e_check.py --from 2026-04-01 --to 2026-08-31 --vs-run 11
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import pandas as pd
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import ingest as ingest_mod  # noqa: E402
from mop_reco import masters as masters_mod  # noqa: E402
from mop_reco import runs, store_sheet_source  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402
from mop_reco.ingest import LoadReport  # noqa: E402

WH = ROOT / "warehouse"


def _dates(source, lo=None, hi=None):
    out = {}
    for p in sorted((WH / source).glob("2???-??-??.parquet")):
        d = date.fromisoformat(p.stem)
        if (lo is None or d >= lo) and (hi is None or d <= hi):
            out[p.stem] = str(p)
    return out


def peak_mb():
    return psutil.Process().memory_info().peak_wset / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="lo", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="hi", required=True, type=date.fromisoformat)
    ap.add_argument("--vs-run", type=int, required=True)
    ap.add_argument("--only-range", type=int, default=None, metavar="WINDOW_DAYS",
                    help="a TRUE short run: only bill dates in --from/--to and pool dates within "
                         "WINDOW_DAYS of them take part (memory/time measurement; not comparable with Run #11)")
    ap.add_argument("--compare-only", type=Path, help="skip the run; compare an existing scratch DB")
    args = ap.parse_args()

    from datetime import timedelta
    if args.only_range is None:
        pos = _dates("pos_bill_wise")      # ALL loaded dates take part in matching,
        paytm, axis, bajaj = _dates("paytm_settlement"), _dates("axis_edc"), _dates("bajaj")
    else:
        w = timedelta(days=args.only_range)
        pos = _dates("pos_bill_wise", args.lo, args.hi)
        paytm, axis, bajaj = (_dates("paytm_settlement", args.lo - w, args.hi + w),
                              _dates("axis_edc", args.lo - w, args.hi + w),
                              _dates("bajaj", args.lo - w, args.hi + w))

    def stub(name, paths, frame_len=0):
        def fn(*a, **k):
            rep = LoadReport(source=name, status="ok", warehouse_paths=paths)
            return pd.DataFrame({"x": range(frame_len)}), rep
        return fn

    ingest_mod.ingest_pos_bill_wise = stub("pos_bill_wise", pos)
    ingest_mod.ingest_paytm = stub("paytm_settlement", paytm)
    ingest_mod.ingest_axis_edc = stub("axis_edc", axis)
    ingest_mod.ingest_bajaj = stub("bajaj", bajaj, frame_len=1)
    # The masters pre-flight (correctly) refuses today: PICKUP LOCATION MASTER.xlsx has lost its
    # LOCATION column. It is not part of what this script compares, so it is stubbed here only.
    masters_mod.preflight_masters = lambda config, session=None, raise_on_failure=True: []
    store_sheet_source.persist_aggregates = lambda *a, **k: {"store_days": 0, "dates": 0, "elapsed": 0.0}
    store_sheet_source.persist_mop_day_agg = lambda *a, **k: {"rows": 0, "elapsed": 0.0}

    if args.compare_only:
        scratch = args.compare_only
        new_run = 1
    else:
        tmp = Path(tempfile.mkdtemp(prefix="r1e2e_"))
        scratch = tmp / "scratch.sqlite"
        engine = get_engine(scratch)
        init_db(engine)
        session = get_session(engine)

        t = time.perf_counter()
        out = runs.run_reconciliation(load_config(), session, date_from=args.lo, date_to=args.hi,
                                      triggered_by="r1_e2e_check")
        secs = time.perf_counter() - t
        print(f"run_reconciliation: {secs:,.1f}s, PEAK WORKING SET {peak_mb():,.0f} MB", flush=True)
        print(f"timings: {out['timings']}", flush=True)
        new_run = out["run_id"]
        session.close()

    if args.only_range is not None:
        print("RESULT: (short run - no comparison with Run #11)", flush=True)
        return 0
    real = ROOT / "db" / "reco.sqlite"
    c = sqlite3.connect(f"file:{Path(scratch).as_posix()}?mode=ro", uri=True)
    c.execute(f"ATTACH DATABASE 'file:{real.as_posix()}?mode=ro' AS real")
    rc = 0

    def compare(label, cols, new_where, old_where, table):
        nonlocal rc
        t0 = time.perf_counter()
        cl = ", ".join(cols)
        a = c.execute(f"select count(*) from (select {cl} from main.{table} where {new_where} "
                      f"except select {cl} from real.{table} where {old_where})").fetchone()[0]
        b = c.execute(f"select count(*) from (select {cl} from real.{table} where {old_where} "
                      f"except select {cl} from main.{table} where {new_where})").fetchone()[0]
        n_new = c.execute(f"select count(*) from main.{table} where {new_where}").fetchone()[0]
        n_old = c.execute(f"select count(*) from real.{table} where {old_where}").fetchone()[0]
        ok = (a == 0 and b == 0 and n_new == n_old)
        rc |= int(not ok)
        print(f"{label:28s}: new {n_new:,} / run#{args.vs_run} {n_old:,}; only-new {a:,}, "
              f"only-old {b:,}  -> {'IDENTICAL' if ok else 'DIFFERENT'}  ({time.perf_counter() - t0:.0f}s)",
              flush=True)

    compare("r1_bill_lines",
            ["business_date", "store", "bill_no", "line_seq", "mop", "mop_class", "amount",
             "paytm_ref", "axis_ref", "bajaj_ref", "matched", "match_tier", "matched_gateway",
             "matched_txn_ref", "status", "date_diff_days", "match_note", "payment_mode",
             "bill_time", "created_by"],
            f"run_id={new_run}", f"run_id={args.vs_run}", "r1_bill_lines")
    compare("digital_mode_agg", ["business_date", "store", "mode", "sale"],
            f"run_id={new_run}", f"run_id={args.vs_run}", "digital_mode_agg")
    compare("reconciliation_results(R1)", ["business_date", "store", "payload"],
            f"run_id={new_run} and reconciliation='R1'",
            f"run_id={args.vs_run} and reconciliation='R1'", "reconciliation_results")
    # Exceptions: the register is cumulative in the real DB, so compare what Run #11
    # itself saw (last_seen_run_id) against everything the scratch run created.
    compare("exceptions(key,type,amount)",
            ["exception_key", "type", "business_date", "store", "amount", "transaction_time"],
            "1=1", f"last_seen_run_id={args.vs_run}", "exceptions")

    n1 = json.loads(c.execute("select notes from main.runs where id=?", (new_run,)).fetchone()[0])["r1"]
    n0 = json.loads(c.execute("select notes from real.runs where id=?", (args.vs_run,)).fetchone()[0])["r1"]
    diff = {k: (n0.get(k), n1.get(k)) for k in set(n0) | set(n1) if n0.get(k) != n1.get(k)}
    print(f"R1 summary vs run #{args.vs_run}: "
          f"{'IDENTICAL' if not diff else f'differs only in {diff}'}", flush=True)
    print("RESULT:", "IDENTICAL" if rc == 0 else "DIFFERENCES FOUND", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
