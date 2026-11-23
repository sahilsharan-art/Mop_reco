"""Shadow run: read the ERP and compare it with the file-sourced data already in the warehouse. Read-only, changes nothing.

    deploy\\run_in_app_env.ps1 scripts\\erp_shadow.py --source pos --from 2026-07-01 --to 2026-07-31
    deploy\\run_in_app_env.ps1 scripts\\erp_shadow.py --source ptc --from 2026-07-01 --to 2026-07-31

POS is read one day at a time (the DBA's rule: a half-open date range, 2 to 4 connections, off-peak where possible), put through the
SAME normaliser as the Excel file, and compared with that day's warehouse partition at bill x payment-mode level: lines, amounts, and above
all the payment REFERENCES parsed from the ERP's terminal reply against those parsed from the Excel file's. PTC is compared by voucher.

Nothing here is written anywhere except the console. Exit code 0 only when every date agrees; 1 when something differs; 2 when the ERP
view is not ready (a named reason). This is the evidence behind switching a source from the file to the ERP (CLAUDE.md 6.41).
Dates are arguments (CLAUDE.md rule 8). MOP_ROOT lets a copy run from elsewhere.
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(os.environ.get("MOP_ROOT") or Path(__file__).resolve().parents[1])
for _candidate in (Path(__file__).resolve().parents[1] / "app", ROOT / "app"):       # the code: next to this script, or under MOP_ROOT
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))

from mop_reco import erp, erp_shadow, erp_source  # noqa: E402
from mop_reco.config import load_config  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--source", choices=["pos", "ptc"], required=True)
ap.add_argument("--from", dest="d_from", required=True, help="first business date, YYYY-MM-DD")
ap.add_argument("--to", dest="d_to", required=True, help="last business date, YYYY-MM-DD")
ap.add_argument("--max-examples", type=int, default=10)
args = ap.parse_args()
d_from, d_to = date.fromisoformat(args.d_from), date.fromisoformat(args.d_to)
config = load_config()
WH = ROOT / "warehouse"


def read_warehouse_day(source: str, day: date) -> pd.DataFrame:
    f = WH / source / f"{day.isoformat()}.parquet"
    return pd.read_parquet(f) if f.is_file() else pd.DataFrame()


def show(rows, title):
    if rows:
        print(f"    {title}:")
        for r in rows:
            print("      ", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})


conn = erp.connect()
try:
    if args.source == "pos":
        erp_source.check_pos_schema(conn)
    else:
        erp_source.check_ptc_schema(conn)
except erp_source.ErpSchemaError as exc:
    print(f"NOT READY: {exc}")
    sys.exit(2)

print(f"Shadow run {args.source.upper()} {d_from} to {d_to}: ERP ({erp.describe()}) against the warehouse built from the files\n")

if args.source == "pos":
    started = time.time()
    totals = {k: 0 for k in ("days", "days_agree", "lines_erp", "lines_file", "only_in_erp", "only_in_file", "amount_differs", "ref_differs",
                             "lines_differ", "time_differs", "operator_differs", "parse_failed_erp", "parse_failed_file", "both")}
    amount_erp = amount_file = 0.0
    ref_by_gateway = {"paytm": 0, "axis": 0, "bajaj": 0}
    missing_days = []
    for raw in erp_source.iter_pos_raw(conn, d_from, d_to, chunk_days=1):
        pass_day = d_from + timedelta(days=totals["days"])
        erp_df = erp_source.normalise_pos(config, raw)
        file_df = read_warehouse_day("pos_bill_wise", pass_day)
        if file_df.empty:
            missing_days.append(pass_day)
        r = erp_shadow.compare_pos(erp_df, file_df, args.max_examples)
        totals["days"] += 1
        if totals["days"] % 10 == 0 or totals["days"] == (d_to - d_from).days + 1:
            print(f"  ... {totals['days']} of {(d_to - d_from).days + 1} dates compared, {time.time() - started:,.0f} s elapsed", flush=True)
        totals["days_agree"] += int(erp_shadow.pos_agrees(r))
        for k in ("only_in_erp", "only_in_file", "amount_differs", "ref_differs", "lines_differ", "time_differs", "operator_differs",
                  "parse_failed_erp", "parse_failed_file", "both"):
            totals[k] += r[k]
        totals["lines_erp"] += r["lines_erp"]
        totals["lines_file"] += r["lines_file"]
        amount_erp += r["amount_erp"]
        amount_file += r["amount_file"]
        for g, n in r["ref_differs_by_gateway"].items():
            ref_by_gateway[g] += n
        if not erp_shadow.pos_agrees(r):
            print(f"{pass_day}: DIFFERS  bills-modes only in ERP {r['only_in_erp']} / only in file {r['only_in_file']}, "
                  f"amount differs {r['amount_differs']}, reference differs {r['ref_differs']} {r['ref_differs_by_gateway']}")
            show(r["examples_only_erp"][:args.max_examples], "only in the ERP")
            show(r["examples_only_file"][:args.max_examples], "only in the file")
            show(r["examples_amount"][:args.max_examples], "amount differs")
            show(r["examples_refs"][:args.max_examples], "reference differs")
    print(f"\nDates compared: {totals['days']}   agree: {totals['days_agree']}   differ: {totals['days'] - totals['days_agree']}"
          + (f"   (no warehouse partition for {len(missing_days)} date(s): {[str(d) for d in missing_days[:5]]})" if missing_days else ""))
    print(f"Lines: ERP {totals['lines_erp']:,}  file {totals['lines_file']:,}  (difference {totals['lines_erp'] - totals['lines_file']:+,}; "
          f"{totals['lines_differ']:,} bill-modes carry a different number of lines with the same amount, e.g. split credit notes)")
    print(f"Amount: ERP {amount_erp:,.2f}  file {amount_file:,.2f}  difference {amount_erp - amount_file:+,.2f}")
    print(f"Bills x modes compared in both: {totals['both']:,}   only in ERP: {totals['only_in_erp']:,}   only in file: {totals['only_in_file']:,}")
    print(f"PAYMENT REFERENCES that differ: {sum(ref_by_gateway.values()):,}  by gateway {ref_by_gateway}   (terminal reply unparsable: ERP {totals['parse_failed_erp']:,}, file {totals['parse_failed_file']:,})")
    print(f"Informational: bill time differs on {totals['time_differs']:,}, till operator differs on {totals['operator_differs']:,}")
    agrees = totals["days"] == totals["days_agree"] and not missing_days
    print("\nVERDICT:", "AGREES - every date matches on bills, amounts and payment references." if agrees else "DIFFERENCES FOUND - explain every one before switching this source to the ERP.")
    conn.close()
    sys.exit(0 if agrees else 1)

# --- PTC ---------------------------------------------------------------------------------------------------------
erp_df = erp_source.read_ptc(conn, d_from, d_to)
parts = [read_warehouse_day("ptc", d_from + timedelta(days=i)) for i in range((d_to - d_from).days + 1)]
file_df = pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) else pd.DataFrame()
r = erp_shadow.compare_ptc(erp_df, file_df, args.max_examples)
print(f"Vouchers: ERP {r['vouchers_erp']:,}  file {r['vouchers_file']:,}   in both {r['both']:,}   only in ERP {r['only_in_erp']:,}   only in file {r['only_in_file']:,}")
print(f"Amount: ERP {r['amount_erp']:,.2f}  file {r['amount_file']:,.2f}   difference {r['amount_erp'] - r['amount_file']:+,.2f}")
print(f"Cash/CMS deposit total: ERP {r['deposit_total_erp']:,.2f}  file {r['deposit_total_file']:,.2f}")
print(f"Amount differs on {r['amount_differs']:,} voucher(s); mode/head/ledger differs on {r['class_differs']:,}")
show(r["examples_only_erp"], "only in the ERP")
show(r["examples_only_file"], "only in the file")
show(r["examples_amount"], "amount differs")
agrees = erp_shadow.ptc_agrees(r)
print("\nVERDICT:", "AGREES - every voucher matches." if agrees else "DIFFERENCES FOUND - explain every one before switching this source to the ERP.")
conn.close()
sys.exit(0 if agrees else 1)
