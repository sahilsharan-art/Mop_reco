"""Compare the ERP's POS bill lines with the application's own copy (the warehouse, built from the Excel exports). Read-only.

    deploy\\run_in_app_env.ps1 scripts\\erp_compare_pos.py --from 2026-07-01 --to 2026-07-31
    deploy\\run_in_app_env.ps1 scripts\\erp_compare_pos.py --from 2026-07-20 --to 2026-07-20 --detail-days 1

Step 1 - per date x payment mode: line count and amount total on each side; every difference is listed.
Step 2 - for the first --detail-days dates that differ: per bill x payment mode, which side holds extra or different lines.

This is the shadow-run check of docs/ERP_INTEGRATION_BRIEF.md section 8: a source may be switched from the Excel file to the ERP only
when this agrees, with every difference explained. It prints bill numbers, payment modes and amounts only - no terminal replies, no
card or customer data. Dates are arguments (CLAUDE.md rule 8). ERP queries filter the date column directly (a half-open range, never a
function on the column), as the DBA asked.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(os.environ.get("MOP_ROOT") or Path(__file__).resolve().parents[1])     # MOP_ROOT: run a copy from elsewhere
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import erp  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--from", dest="d_from", required=True, help="first business date, YYYY-MM-DD")
ap.add_argument("--to", dest="d_to", required=True, help="last business date, YYYY-MM-DD")
ap.add_argument("--detail-days", type=int, default=2, help="how many differing dates to break down to bill level")
ap.add_argument("--max-rows", type=int, default=40, help="most differing rows to print per table")
args = ap.parse_args()
d_from, d_to = date.fromisoformat(args.d_from), date.fromisoformat(args.d_to)
WAREHOUSE = ROOT / "warehouse" / "pos_bill_wise"

ERP_TABLE = "gold_mop.pos_bill_lines"
conn = erp.connect()


def erp_by_date_mop(day_from: date, day_to: date) -> pd.DataFrame:
    rows = erp.run_select(
        conn,
        f"select business_date::date as d, mop, count(*) as n, round(sum(amount)::numeric, 2) as v from {ERP_TABLE} "
        "where business_date >= %s and business_date < %s group by 1, 2",
        [datetime.combine(day_from, datetime.min.time()), datetime.combine(day_to + timedelta(days=1), datetime.min.time())])
    return pd.DataFrame(rows, columns=["d", "mop", "erp_n", "erp_v"]).astype({"erp_v": float})


def erp_by_bill(day: date) -> pd.DataFrame:
    rows = erp.run_select(
        conn,
        f"select bill_no, mop, count(*) as n, round(sum(amount)::numeric, 2) as v from {ERP_TABLE} "
        "where business_date >= %s and business_date < %s group by 1, 2",
        [datetime.combine(day, datetime.min.time()), datetime.combine(day + timedelta(days=1), datetime.min.time())])
    return pd.DataFrame(rows, columns=["bill_no", "mop", "erp_n", "erp_v"]).astype({"erp_v": float})


def warehouse_day(day: date) -> pd.DataFrame:
    f = WAREHOUSE / f"{day.isoformat()}.parquet"
    if not f.is_file():
        return pd.DataFrame(columns=["bill_no", "mop", "amount"])
    return pd.read_parquet(f, columns=["bill_no", "mop", "amount"])


days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
print(f"Comparing {d_from} to {d_to} ({len(days)} dates): ERP {ERP_TABLE} vs the application's warehouse copy")

erp_df = pd.concat([erp_by_date_mop(m_from, min(m_from + timedelta(days=30), d_to))
                    for m_from in [d_from + timedelta(days=31 * k) for k in range((len(days) + 30) // 31)]], ignore_index=True)
erp_df = erp_df.groupby(["d", "mop"], as_index=False).sum(numeric_only=True)

wh_frames = []
for day in days:
    w = warehouse_day(day)
    if len(w):
        g = w.groupby("mop", as_index=False).agg(wh_n=("amount", "size"), wh_v=("amount", "sum"))
        g.insert(0, "d", day)
        wh_frames.append(g)
wh_df = pd.concat(wh_frames, ignore_index=True) if wh_frames else pd.DataFrame(columns=["d", "mop", "wh_n", "wh_v"])
wh_df["wh_v"] = wh_df["wh_v"].astype(float).round(2)

m = erp_df.merge(wh_df, on=["d", "mop"], how="outer").fillna({"erp_n": 0, "wh_n": 0, "erp_v": 0.0, "wh_v": 0.0})
m["dn"] = (m["erp_n"] - m["wh_n"]).astype(int)
m["dv"] = (m["erp_v"] - m["wh_v"]).round(2)

print(f"\nTOTAL   lines: ERP {int(m.erp_n.sum()):,}  warehouse {int(m.wh_n.sum()):,}  difference {int(m.dn.sum()):+,}"
      f"   |   amount: ERP {m.erp_v.sum():,.2f}  warehouse {m.wh_v.sum():,.2f}  difference {m.dv.sum():+,.2f}")
print("\nBy payment mode (whole range):")
by_mop = m.groupby("mop", as_index=False).agg(erp_n=("erp_n", "sum"), wh_n=("wh_n", "sum"), dn=("dn", "sum"),
                                              erp_v=("erp_v", "sum"), wh_v=("wh_v", "sum"), dv=("dv", "sum"))
print(by_mop.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

m["month"] = pd.to_datetime(m["d"]).dt.strftime("%Y-%m")
print("\nBy month:")
by_month = m.groupby("month", as_index=False).agg(erp_n=("erp_n", "sum"), wh_n=("wh_n", "sum"), dn=("dn", "sum"),
                                                   erp_v=("erp_v", "sum"), wh_v=("wh_v", "sum"), dv=("dv", "sum"))
print(by_month.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
by_day = m.groupby("d", as_index=False).agg(erp_n=("erp_n", "sum"), wh_n=("wh_n", "sum"), dn=("dn", "sum"),
                                            erp_v=("erp_v", "sum"), wh_v=("wh_v", "sum"), dv=("dv", "sum"))
worst = by_day.reindex(by_day["dv"].abs().sort_values(ascending=False).index).head(10)
print("\nTen dates with the largest amount difference (all modes together):")
print(worst.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

diff = m[(m.dn != 0) | (m.dv != 0)].sort_values(["d", "mop"])
print(f"\nDate x mode rows that differ: {len(diff)} of {len(m)}  (dates affected: {diff['d'].nunique()} of {len(days)})")
if len(diff):
    print(diff.head(args.max_rows).to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    if len(diff) > args.max_rows:
        print(f"  ... {len(diff) - args.max_rows} more")

for day in sorted(diff["d"].unique())[:max(0, args.detail_days)]:
    e = erp_by_bill(day)
    w = warehouse_day(day).groupby(["bill_no", "mop"], as_index=False).agg(wh_n=("amount", "size"), wh_v=("amount", "sum"))
    w["wh_v"] = w["wh_v"].astype(float).round(2)
    b = e.merge(w, on=["bill_no", "mop"], how="outer").fillna({"erp_n": 0, "wh_n": 0, "erp_v": 0.0, "wh_v": 0.0})
    b = b[(b.erp_n != b.wh_n) | ((b.erp_v - b.wh_v).abs() > 0.004)]
    only_erp = int(((b.wh_n == 0) & (b.erp_n > 0)).sum())
    only_wh = int(((b.erp_n == 0) & (b.wh_n > 0)).sum())
    print(f"\nBill level, {day}: {len(b)} bill x mode combinations differ "
          f"({only_erp} only in the ERP, {only_wh} only in the warehouse, {len(b) - only_erp - only_wh} in both with different lines or amounts)")
    if len(b):
        print(b.sort_values("bill_no").head(args.max_rows).to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

conn.close()
