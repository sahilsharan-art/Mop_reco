"""A compact, exact fingerprint of one run's results, for comparing two installations without moving the data.

    python scripts/run_fingerprint.py --run-id 2 --out fingerprint.json     (PostgreSQL only)

For every business date it records the row count, the money total and an MD5 over every row (sorted, amounts rounded to
2 decimals) of: r1_bill_lines (matched flag, tier, gateway, transaction ref, payment mode, date difference), store_day_agg,
mop_day_agg, digital_mode_agg and txn_day_agg. It also records the run's R1 summary, and the exceptions by type and status.
Run it on both sides and compare the two JSON files (scripts/compare_fingerprints.py): identical hashes = identical results.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from sqlalchemy import text  # noqa: E402

from mop_reco.db import Run, get_engine, get_session, is_postgres  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", type=int, required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--last", default=None, help="only business dates up to this ISO date (default: the run's last date)")
args = ap.parse_args()

engine = get_engine()
if not is_postgres(engine):
    sys.exit("PostgreSQL only (uses in-database MD5)")
session = get_session(engine)
run = session.get(Run, args.run_id)
if run is None:
    sys.exit(f"run {args.run_id} not found")
dates = json.loads(run.business_dates)
last = args.last or max(dates)
R = {"run_id": args.run_id, "status": run.status, "last_date": last}


def money(col):
    return f"round(coalesce({col},0)::numeric, 2)::text"


def per_date(sql_row, table, where_extra=""):
    q = text(f"""
        select business_date, count(*) n, round(sum(sum_col)::numeric,2)::text total,
               md5(string_agg(r, E'\\n' order by r collate "C")) h
        from (select business_date, {sql_row[1]} sum_col, {sql_row[0]} r from {table}
              where run_id = :run and business_date <= :last {where_extra}) x
        group by business_date order by business_date""")
    return {d: [n, t, h] for d, n, t, h in session.execute(q, {"run": args.run_id, "last": last})}


R["r1_bill_lines"] = per_date(
    ("concat_ws('|', coalesce(bill_no,''), coalesce(line_seq,0)::text, coalesce(mop,''), " + money("amount") +
     ", matched::int::text, coalesce(match_tier,''), coalesce(matched_gateway,''), coalesce(matched_txn_ref,''), "
     "coalesce(payment_mode,''), coalesce(date_diff_days,0)::text, coalesce(store,''))", "amount"), "r1_bill_lines")
R["store_day_agg"] = per_date(
    ("concat_ws('|', store, " + ", ".join(money(c) for c in (
        "total_sale", "digital_sale", "cash_sale", "opening_balance", "cash_deposit", "cash_received", "cash_receivable",
        "cash_expense_income", "closing_balance", "float_amount")) + ")", "total_sale"), "store_day_agg")
R["mop_day_agg"] = per_date(("concat_ws('|', store, bucket, " + money("sale") + ")", "sale"), "mop_day_agg")
R["digital_mode_agg"] = per_date(("concat_ws('|', store, mode, " + money("sale") + ")", "sale"), "digital_mode_agg")
R["txn_day_agg"] = per_date(
    ("concat_ws('|', coalesce(store,''), gateway, total_n::text, " + ", ".join(money(c) for c in (
        "total_v", "mapped_v", "pool_v", "consumed_v")) + ", mapped_n::text, coalesce(pool_n,0)::text, coalesce(consumed_n,0)::text)",
     "total_v"), "txn_day_agg")

notes = json.loads(run.notes or "{}")
R["r1_summary"] = notes.get("r1")
R["exceptions_open_by_type"] = {
    f"{t}|{s}": [n, a] for t, s, n, a in session.execute(text(
        "select type, status, count(*), round(coalesce(sum(amount),0)::numeric,2)::text from exceptions "
        "where business_date <= :last and status in ('open','under_investigation') group by type, status order by 1,2"), {"last": last})}
R["rows"] = {t: session.execute(text(f"select count(*) from {t} where run_id=:r"), {"r": args.run_id}).scalar()
             for t in ("r1_bill_lines", "store_day_agg", "mop_day_agg", "digital_mode_agg", "txn_day_agg", "cms_slips")}
Path(args.out).write_text(json.dumps(R, indent=1, sort_keys=True), encoding="utf-8")
print(f"fingerprint written: {args.out}  (run {args.run_id}, dates up to {last}, "
      f"{sum(v[0] for v in R['r1_bill_lines'].values()):,} bill lines)")
