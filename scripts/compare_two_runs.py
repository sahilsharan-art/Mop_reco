"""Compare two runs stored in the SAME database, table by table, for a range of business dates. Read-only.

    deploy\\run_in_app_env.ps1 scripts\\compare_two_runs.py --a 2 --b 3 --from 2026-04-01 --to 2026-10-31

Used to prove that a run read from a different SOURCE (the ERP instead of the Excel files) reproduces the earlier run over the dates where the two
sources agree. Compared per date and store, cell by cell, with a tolerance of Rs 0.005: store_day_agg (the cash book and sales, columns C..N),
mop_day_agg, digital_mode_agg, txn_day_agg, and the R1 result per date (lines, value, matched count and value). Exit code 0 only when every table
is identical over the range. Dates are arguments (CLAUDE.md rule 8).
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.db import get_engine, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--a", type=int, required=True, help="the earlier run id")
ap.add_argument("--b", type=int, required=True, help="the run to check against it")
ap.add_argument("--from", dest="lo", type=date.fromisoformat, required=True)
ap.add_argument("--to", dest="hi", type=date.fromisoformat, required=True)
args = ap.parse_args()
engine = init_db(get_engine())
LO, HI = args.lo.isoformat(), args.hi.isoformat()
TOL = 0.005


def frame(sql: str, run_id: int) -> pd.DataFrame:
    return pd.read_sql(text(sql), engine, params={"run": run_id, "lo": LO, "hi": HI})


def compare(name, sql, keys, money):
    a, b = frame(sql, args.a), frame(sql, args.b)
    for df in (a, b):
        for c in money:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        for k in keys:
            df[k] = df[k].astype(str)
    m = a.merge(b, on=keys, how="outer", suffixes=("_a", "_b"), indicator=True)
    only_a, only_b = int((m["_merge"] == "left_only").sum()), int((m["_merge"] == "right_only").sum())
    both = m[m["_merge"] == "both"]
    cells, worst = 0, {}
    for c in money:
        d = (both[c + "_a"] - both[c + "_b"]).abs()
        n = int((d > TOL).sum())
        cells += n
        if n:
            worst[c] = (n, round(float(d.max()), 2))
    ok = not (only_a or only_b or cells)
    print(f"{'OK  ' if ok else 'DIFF'} {name:34s} rows A={len(a):>8,} B={len(b):>8,} | only in A {only_a:,}, only in B {only_b:,} | differing cells {cells:,} {worst or ''}")
    if not ok and (only_a or only_b):
        print("       first only-in-A:", m[m["_merge"] == "left_only"][keys].head(3).values.tolist(), "| first only-in-B:",
              m[m["_merge"] == "right_only"][keys].head(3).values.tolist())
    return ok


print(f"Run {args.a} (A) against run {args.b} (B), business dates {LO} to {HI}\n")
RANGE = "run_id = :run and business_date >= :lo and business_date <= :hi"
results = [
    compare("store_day_agg (cash book, sales)",
            f"select business_date, store, total_sale, digital_sale, cash_sale, opening_balance, cash_deposit, cash_received, cash_receivable, "
            f"cash_expense_income, closing_balance, float_amount from store_day_agg where {RANGE}",
            ["business_date", "store"], ["total_sale", "digital_sale", "cash_sale", "opening_balance", "cash_deposit", "cash_received",
                                         "cash_receivable", "cash_expense_income", "closing_balance", "float_amount"]),
    compare("mop_day_agg", f"select business_date, store, bucket, sale from mop_day_agg where {RANGE}", ["business_date", "store", "bucket"], ["sale"]),
    compare("digital_mode_agg", f"select business_date, store, mode, sale from digital_mode_agg where {RANGE}", ["business_date", "store", "mode"], ["sale"]),
    compare("txn_day_agg",
            f"select business_date, coalesce(store,'') store, gateway, total_n, total_v, mapped_n, mapped_v, pool_n, pool_v, consumed_n, consumed_v "
            f"from txn_day_agg where {RANGE}", ["business_date", "store", "gateway"],
            ["total_n", "total_v", "mapped_n", "mapped_v", "pool_n", "pool_v", "consumed_n", "consumed_v"]),
    compare("R1 per date (lines, value, matched)",
            f"select business_date, count(*) n, sum(amount) amt, sum(case when matched then 1 else 0 end) matched_n, "
            f"sum(case when matched then amount else 0 end) matched_amt from r1_bill_lines where {RANGE} group by business_date",
            ["business_date"], ["n", "amt", "matched_n", "matched_amt"]),
    compare("R1 per date and gateway (matched)",
            f"select business_date, coalesce(matched_gateway,'') g, count(*) n, sum(amount) amt from r1_bill_lines where {RANGE} and matched group by 1, 2",
            ["business_date", "g"], ["n", "amt"]),
]
print("\nVERDICT:", "IDENTICAL over the whole range." if all(results) else "DIFFERENCES FOUND - explain each before relying on run B.")
sys.exit(0 if all(results) else 1)
