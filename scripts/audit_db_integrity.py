"""Audit Phase 3 (read-only): database integrity checks. Run with the application's own environment, e.g.
    deploy\\run_in_app_env.ps1 scripts\\audit_db_integrity.py [--run N]
Only SELECT statements are issued; nothing is written. Prints counts and names, never row contents that identify a person."""
import argparse
import sys
from pathlib import Path

import os  # noqa: E402

ROOT = Path(os.environ.get("MOP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "app"))

from sqlalchemy import text  # noqa: E402

from mop_reco.db import get_engine  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run", type=int, default=None, help="run id to check (default: latest completed)")
args = ap.parse_args()
engine = get_engine()
pg = engine.dialect.name == "postgresql"
conn = engine.connect()
failures = []


def one(sql, **p):
    return conn.execute(text(sql), p).scalar()


def rows(sql, **p):
    return conn.execute(text(sql), p).fetchall()


def check(label, bad_count, detail=""):
    ok = (bad_count == 0)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": {bad_count:,} {detail}"))
    if not ok:
        failures.append(label)


print(f"database: {engine.dialect.name}")
run = args.run or one("select max(id) from runs where status = 'completed'")
print(f"checking run #{run}")

print("\n== row counts")
tables = [r[0] for r in rows(("select table_name from information_schema.tables where table_schema = current_schema() order by 1") if pg
                             else "select name from sqlite_master where type = 'table' order by 1")]
for t in tables:
    print(f"  {t:28s} {one(f'select count(*) from {t}'):>12,}")

print("\n== keys and uniqueness")
check("exception_key is unique", one("select count(*) from (select exception_key from exceptions group by 1 having count(*) > 1) x"))
check("r1_bill_lines (run, bill_no, line_seq) is unique for this run",
      one("select count(*) from (select bill_no, line_seq from r1_bill_lines where run_id = :r group by 1, 2 having count(*) > 1) x", r=run),
      "duplicated (bill_no, line_seq) pairs")
check("r1_bill_lines has no null bill_no / line_seq / store for this run",
      one("select count(*) from r1_bill_lines where run_id = :r and (bill_no is null or line_seq is null or store is null)", r=run))
check("store_day_agg (run, date, store) is unique", one("select count(*) from (select 1 from store_day_agg where run_id = :r group by business_date, store having count(*) > 1) x", r=run))
check("date_total_agg has one row per date", one("select count(*) from (select 1 from date_total_agg where run_id = :r group by business_date having count(*) > 1) x", r=run))

print("\n== references")
check("every exception points at existing runs", one("select count(*) from exceptions e left join runs a on a.id = e.first_seen_run_id left join runs b on b.id = e.last_seen_run_id where a.id is null or b.id is null"))
check("every remark points at an existing exception", one("select count(*) from remarks r left join exceptions e on e.id = r.exception_id where e.id is null"))
check("every manual-match event points at a manual match", one("select count(*) from manual_match_events v left join manual_matches m on m.id = v.manual_match_id where m.id is null"))
check("every store-task event points at a task", one("select count(*) from store_task_events v left join store_tasks t on t.id = v.task_id where t.id is null"))
check("every bill line belongs to an existing run", one("select count(*) from r1_bill_lines l left join runs r on r.id = l.run_id where r.id is null"))

print("\n== value domains")
check("business_date is ISO text (YYYY-MM-DD) in r1_bill_lines",
      one("select count(*) from r1_bill_lines where run_id = :r and business_date !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'" if pg else
          "select count(*) from r1_bill_lines where run_id = :r and business_date not glob '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", r=run))
check("business_date is ISO text in exceptions",
      one("select count(*) from exceptions where business_date !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'" if pg else
          "select count(*) from exceptions where business_date not glob '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"))
check("exception status is one of open / under_investigation / closed / auto_resolved",
      one("select count(*) from exceptions where status not in ('open','under_investigation','closed','auto_resolved')"))
check("closed or auto-resolved exceptions carry a reason or a closer",
      one("select count(*) from exceptions where status in ('closed','auto_resolved') and reason_code is null and closed_by is null"),
      "closed with neither reason nor closer")
check("manual match lifecycle is a known state", one("select count(*) from manual_matches where lifecycle not in ('active','reversed','superseded','conflicted','orphaned')"))
check("matched bill lines name a gateway and a transaction", one("select count(*) from r1_bill_lines where run_id = :r and matched and (matched_gateway is null or matched_txn_ref is null)", r=run))
check("unmatched bill lines name no transaction", one("select count(*) from r1_bill_lines where run_id = :r and not matched and matched_txn_ref is not null", r=run))

print("\n== money")
if pg:
    for t, col in [("store_day_agg", "digital_sale"), ("store_day_agg", "cash_sale"), ("store_day_agg", "opening_balance"), ("store_day_agg", "closing_balance"),
                   ("store_day_agg", "cash_received"), ("r1_bill_lines", "amount"), ("exceptions", "amount")]:
        where = "run_id = :r and " if t != "exceptions" else ""
        check(f"{t}.{col} has at most 2 decimals", one(f"select count(*) from {t} where {where}{col} is not null and {col} <> round({col}, 2)", r=run), "rows with sub-paisa digits")
check("store_day_agg: C = D + E on every row", one("select count(*) from store_day_agg where run_id = :r and abs(total_sale - digital_sale - cash_sale) > 0.005", r=run))
check("store_day_agg: K = I - J on every row", one("select count(*) from store_day_agg where run_id = :r and abs(cash_receivable - (cash_deposit - cash_received)) > 0.005", r=run))
check("store_day_agg: M = H + E - I + L on every row", one("select count(*) from store_day_agg where run_id = :r and abs(closing_balance - (opening_balance + cash_sale - cash_deposit + cash_expense_income)) > 0.005", r=run))
check("store_day_agg: N = H - I on every row", one("select count(*) from store_day_agg where run_id = :r and abs(float_amount - (opening_balance - cash_deposit)) > 0.005", r=run))
dsum, bsum = one("select coalesce(sum(digital_sale),0) from store_day_agg where run_id = :r", r=run), one("select coalesce(sum(amount),0) from r1_bill_lines where run_id = :r", r=run)
check("sum of digital sale in the sheet equals sum of bill-line amounts", 0 if abs(float(dsum) - float(bsum)) < 0.005 else 1, f"sheet {float(dsum):,.2f} vs lines {float(bsum):,.2f}")
check("carry-forward: opening(D) = closing(D-1) for every store after its first day", one(
    "select count(*) from store_day_agg a join store_day_agg b on b.run_id = a.run_id and b.store = a.store and "
    + ("b.business_date = to_char(a.business_date::date + 1, 'YYYY-MM-DD') " if pg else "b.business_date = date(a.business_date, '+1 day') ")
    + "where a.run_id = :r and abs(b.opening_balance - a.closing_balance) > 0.005", r=run))

print("\n== runs and history")
check("no run is stuck in 'running' for over 12 hours",
      one(("select count(*) from runs where status = 'running' and coalesce(heartbeat_at, started_at) < now() - interval '12 hours'" if pg else
           "select count(*) from runs where status = 'running'")), "stuck runs")
print("  runs:", [(r[0], r[1]) for r in rows("select id, status from runs order by id")])
check("audit trail tables are not empty where history exists", 0 if (one("select count(*) from auth_events") > 0) else 1, "auth_events is empty")

print("\n== indexes")
if pg:
    idx = rows("select tablename, indexname, indexdef from pg_indexes where schemaname = current_schema() order by 1, 2")
    seen, dup = {}, []
    for t, n, d in idx:
        cols = d.split("(", 1)[1].rsplit(")", 1)[0] if "(" in d else d
        key = (t, cols, "UNIQUE" in d)
        if key in seen:
            dup.append((t, seen[key], n))
        seen[key] = n
    check("no two indexes on a table have an identical column list and uniqueness", len(dup), f"duplicate index pairs: {dup}")
    expected = ["ix_r1_page", "ix_r1_matched_date", "ix_r1_mop_date", "ix_r1_matched_txn", "ux_manual_txn_active", "ux_manual_bill_active", "ux_refund_txn", "ux_store_day_agg"]
    have = {n for _, n, _ in idx}
    check("every documented index exists", len([e for e in expected if e not in have]), f"missing: {[e for e in expected if e not in have]}")
    print("  largest relations:")
    for n, sz in rows("select relname, pg_size_pretty(pg_total_relation_size(c.oid)) from pg_class c join pg_namespace s on s.oid = c.relnamespace "
                      "where s.nspname = current_schema() and c.relkind = 'r' order by pg_total_relation_size(c.oid) desc limit 6"):
        print(f"    {n:28s} {sz}")

print("\n== summary")
print(f"{len(failures)} check(s) failed" + (": " + "; ".join(failures) if failures else " - none"))
sys.exit(1 if failures else 0)
