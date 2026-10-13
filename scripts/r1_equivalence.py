"""Prove the vectorized R1 matcher reproduces the per-row engine on REAL data.

Inputs are read straight from the warehouse Parquet (already-normalised by an
earlier run), so this never touches a raw source file, the masters, or the
database's writable tables.

    python scripts/r1_equivalence.py --from 2026-07-01 --to 2026-07-31 [--old] [--vs-run 11]

  --old       also run the frozen per-row engine (tests/reference_matcher.py)
              on the SAME frames and compare every output column + the
              consumed set. Slow by design (~1-3k bills/s): use a month, not
              the whole range.
  --vs-run N  compare the new matcher's result with the r1_bill_lines rows a
              completed run persisted (read-only), keyed on (bill_no,
              line_seq). Only meaningful if the warehouse still holds that
              run's inputs.

Prints bills/second, and exits non-zero on the first difference.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tests"))

from mop_reco import r1_bill_txn as new  # noqa: E402

WH = ROOT / "warehouse"
POS_COLS = ["bill_no", "line_seq", "store", "business_date", "mop", "amount", "mop_class",
            "in_sale_universe", "paytm_ref", "axis_ref", "bajaj_ref"]


def _files(source: str, lo=None, hi=None):
    out = []
    for p in sorted((WH / source).glob("2???-??-??.parquet")):
        d = date.fromisoformat(p.stem)
        if (lo is None or d >= lo) and (hi is None or d <= hi):
            out.append(p)
    return out


def _read(source: str, lo=None, hi=None, columns=None) -> pd.DataFrame:
    frames = [pd.read_parquet(p, columns=columns) for p in _files(source, lo, hi)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_inputs(lo: date, hi: date):
    t = time.perf_counter()
    pos = _read("pos_bill_wise", lo, hi, POS_COLS)
    digital = pos.loc[pos["in_sale_universe"] & pos["mop_class"].isin(new.DIGITAL_CLASSES)]
    digital = digital.copy().reset_index(drop=True)
    # The pool is date-agnostic (a reference is globally unique), so it is
    # the WHOLE warehouse for each gateway, never the bill range.
    paytm = _read("paytm_settlement")
    axis = _read("axis_edc")
    bajaj = _read("bajaj")
    pool = new.build_gateway_pool(paytm, axis, bajaj if len(bajaj) else None)
    pool = new.successful_acquiring(pool)
    print(f"loaded {len(digital):,} digital bill lines ({lo}..{hi}) and a pool of "
          f"{len(pool):,} SUCCESS/ACQUIRING transactions in {time.perf_counter() - t:.1f}s",
          flush=True)
    return digital, pool


def compare_frames(a: pd.DataFrame, b: pd.DataFrame, label_a: str, label_b: str) -> int:
    """NA-aware, column-by-column; returns the number of differing cells."""
    bad_total = 0
    for col in a.columns:
        if col not in b.columns:
            print(f"  column {col!r} missing from {label_b}")
            bad_total += 1
            continue
        x, y = a[col], b[col]
        xn, yn = x.isna().to_numpy(), y.isna().to_numpy()
        xo, yo = x.astype(object).to_numpy(), y.astype(object).to_numpy()
        eq = np.empty(len(xo), dtype=bool)
        both = ~xn & ~yn
        eq[~both] = xn[~both] == yn[~both]
        eq[both] = xo[both] == yo[both]
        bad = int((~eq).sum())
        if bad:
            i = int(np.flatnonzero(~eq)[0])
            print(f"  DIFF column {col!r}: {bad:,} cell(s), first row {i}: "
                  f"{label_a}={xo[i]!r} {label_b}={yo[i]!r}")
        bad_total += bad
    return bad_total


def run_new(digital, pool):
    t = time.perf_counter()
    t1, consumed = new.match_tier1(digital, pool)
    t_t1 = time.perf_counter() - t
    t = time.perf_counter()
    t2, consumed = new.match_tier2_edc_other(t1, pool, consumed)
    t_t2 = time.perf_counter() - t
    print(f"NEW  tier1 {t_t1:7.1f}s  tier2 {t_t2:6.1f}s  total {t_t1 + t_t2:7.1f}s  "
          f"= {len(digital) / max(t_t1 + t_t2, 1e-9):,.0f} bills/s", flush=True)
    return t2, consumed, t_t1 + t_t2


def _peak():
    import psutil
    return psutil.Process().memory_info().peak_wset / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="lo", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="hi", required=True, type=date.fromisoformat)
    ap.add_argument("--old", action="store_true")
    ap.add_argument("--vs-run", type=int)
    args = ap.parse_args()

    digital, pool = load_inputs(args.lo, args.hi)
    n_out, n_consumed, new_secs = run_new(digital, pool)
    print(f"     matched {int(n_out['matched'].sum()):,} of {len(n_out):,}; "
          f"consumed {len(n_consumed):,}", flush=True)
    rc = 0
    print(f"WHOLE-FRAME ENGINE: PEAK WORKING SET {_peak():,.0f} MB", flush=True)

    if args.old:
        import reference_matcher as ref
        t = time.perf_counter()
        o1, oc = ref.match_tier1(digital, pool)
        o2, oc = ref.match_tier2_edc_other(o1, pool, oc)
        old_secs = time.perf_counter() - t
        print(f"OLD  total {old_secs:7.1f}s  = {len(digital) / old_secs:,.0f} bills/s   "
              f"(speed-up x{old_secs / max(new_secs, 1e-9):,.0f})", flush=True)
        bad = compare_frames(o2, n_out, "old", "new")
        same_consumed = oc == n_consumed
        print(f"old vs new: {bad} differing cells; consumed sets "
              f"{'IDENTICAL' if same_consumed else 'DIFFER'}", flush=True)
        rc |= int(bad > 0 or not same_consumed)

    if args.vs_run:
        import sqlite3
        c = sqlite3.connect(f"file:{ROOT / 'db' / 'reco.sqlite'}?mode=ro", uri=True)
        q = ("select bill_no, line_seq, matched, match_tier, matched_gateway, "
             "matched_txn_ref, date_diff_days, match_note, payment_mode "
             "from r1_bill_lines where run_id=? and business_date between ? and ?")
        db = pd.read_sql_query(q, c, params=(args.vs_run, str(args.lo), str(args.hi)))
        mine = n_out[["bill_no", "line_seq", "matched", "match_tier", "matched_gateway",
                      "matched_txn_ref", "date_diff_days", "match_note",
                      "matched_payment_mode"]].rename(columns={"matched_payment_mode": "payment_mode"})
        mine["matched"] = mine["matched"].astype(int)
        mine["line_seq"] = mine["line_seq"].astype(float)
        db["line_seq"] = db["line_seq"].astype(float)
        print(f"run #{args.vs_run}: {len(db):,} persisted lines; new: {len(mine):,}", flush=True)
        joined = db.merge(mine, on=["bill_no", "line_seq"], how="outer",
                          suffixes=("_db", "_new"), indicator=True)
        only = {k: v for k, v in joined["_merge"].value_counts().to_dict().items() if v}
        print(f"  key overlap: {only}", flush=True)
        both = joined[joined["_merge"] == "both"]
        bad = 0
        for col in ["matched", "match_tier", "matched_gateway", "matched_txn_ref",
                    "date_diff_days", "match_note", "payment_mode"]:
            x, y = both[f"{col}_db"], both[f"{col}_new"]
            xn, yn = x.isna().to_numpy(), y.isna().to_numpy()
            xo, yo = x.astype(object).to_numpy(), y.astype(object).to_numpy()
            eq = np.where(xn | yn, xn == yn, xo == yo)
            n_bad = int((~eq.astype(bool)).sum())
            bad += n_bad
            print(f"  {col:18s}: {n_bad:,} differing", flush=True)
        rc |= int(bad > 0 or bool(set(only) - {"both"}))

        # Unmapped transactions: engine residual vs the run's persisted
        # unmapped_gateway_transaction exceptions (key = gateway|transaction_id),
        # net of the transactions an ACTIVE manual match consumed.
        unmapped = pool.loc[~pool.index.isin(n_consumed)]
        eng = set(unmapped["gateway"] + "|" + unmapped["txn_ref"].astype(str))
        persisted = {r[0] for r in c.execute(
            "select exception_key from exceptions where type='unmapped_gateway_transaction' "
            "and last_seen_run_id=?", (args.vs_run,))}
        manual = {f"{r[0]}|{r[1]}" for r in c.execute(
            "select gateway, transaction_ref from manual_matches where lifecycle='active'")}
        eng_net = eng - manual
        print(f"unmapped: engine residual {len(eng):,}; active manual matches consume "
              f"{len(eng & manual)}; engine net {len(eng_net):,}; persisted in run "
              f"#{args.vs_run}: {len(persisted):,}", flush=True)
        print(f"  engine-only: {len(eng_net - persisted)}  persisted-only: {len(persisted - eng_net)}",
              flush=True)
        for k in sorted(eng_net - persisted)[:5]:
            print(f"    engine-only  {k}")
        for k in sorted(persisted - eng_net)[:5]:
            print(f"    persisted-only {k}")
        rc |= int(eng_net != persisted)
    return rc


if __name__ == "__main__":
    sys.exit(main())
