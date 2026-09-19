"""One-time backfill: populate digital_mode_agg (and the payment_mode
column on r1_bill_lines) for a run that completed before the Card/UPI
correctness fix (2026-09-19, docs/MOP_PAYMENT_MODE.md). A run from here on
writes digital_mode_agg itself, as its own stage in
`runs.run_reconciliation()` — this script exists only to bring an EXISTING
run's data forward without a full re-reconciliation.

Unlike backfill_mop_day_agg.py, this cannot rebuild everything from
r1_bill_lines alone: an old run's matched Paytm bill lines carry
matched_gateway and matched_txn_ref but no payment_mode (that column did
not exist yet), so this script reads the warehouse's own paytm_settlement
Parquet once to look the payment_mode up by transaction_id — the same
source `r1_bill_txn.build_gateway_pool()` reads at run time, never the
raw file.

Run from the project root:  python scripts/backfill_digital_mode_agg.py [run_id]
Defaults to the latest run when no run_id is given.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco.db import R1BillLine, Run, get_session, init_db  # noqa: E402

WAREHOUSE = ROOT / "warehouse"


def _read_all(source: str, columns=None) -> pd.DataFrame:
    folder = WAREHOUSE / source
    if not folder.exists():
        return pd.DataFrame(columns=columns or [])
    files = [p for p in sorted(folder.glob("*.parquet")) if not p.stem.startswith("_")]
    if not files:
        return pd.DataFrame(columns=columns or [])
    return pd.concat([pd.read_parquet(p, columns=columns) for p in files], ignore_index=True)


session = get_session(init_db())

run_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
run = (session.query(Run).get(run_id) if run_id
      else session.query(Run).order_by(Run.started_at.desc()).first())
if run is None:
    print("No run found.")
    sys.exit(1)

rows = session.query(
    R1BillLine.id, R1BillLine.business_date, R1BillLine.store, R1BillLine.mop,
    R1BillLine.amount, R1BillLine.matched, R1BillLine.matched_gateway,
    R1BillLine.matched_txn_ref,
).filter(R1BillLine.run_id == run.id).all()

if not rows:
    print(f"run #{run.id} has no bill lines — nothing to backfill.")
    sys.exit(0)

bill_lines = pd.DataFrame(rows, columns=[
    "id", "business_date", "store", "mop", "amount", "matched",
    "matched_gateway", "matched_txn_ref",
])
print(f"Backfilling run #{run.id}: {len(bill_lines)} bill line(s)")

paytm = _read_all("paytm_settlement", ["transaction_id", "payment_mode"])
paytm["transaction_id"] = paytm["transaction_id"].astype(str).str.strip()
paytm_pm = dict(zip(paytm["transaction_id"], paytm["payment_mode"]))

is_paytm_matched = bill_lines["matched"] & (bill_lines["matched_gateway"] == "paytm")
refs = bill_lines.loc[is_paytm_matched, "matched_txn_ref"].astype(str).str.strip()
bill_lines["matched_payment_mode"] = pd.NA
bill_lines.loc[is_paytm_matched, "matched_payment_mode"] = refs.map(paytm_pm)

not_found = is_paytm_matched & bill_lines["matched_payment_mode"].isna()
if not_found.any():
    print(f"WARNING: {int(not_found.sum())} matched Paytm bill line(s) had no "
         "payment_mode in the warehouse (their transaction_id was not found in "
         "paytm_settlement) — folded into Unclassified, not guessed.")

# Persist payment_mode back onto r1_bill_lines for traceability (the column
# a normal run populates at match time) — matches paytm rows only; Axis/
# Bajaj never need this column to be classified (see classify_digital_mode).
updates = [
    {"id": int(bid), "payment_mode": (None if pd.isna(pm) else str(pm))}
    for bid, pm in zip(bill_lines.loc[is_paytm_matched, "id"],
                       bill_lines.loc[is_paytm_matched, "matched_payment_mode"])
]
for i in range(0, len(updates), 5000):
    session.bulk_update_mappings(R1BillLine, updates[i:i + 5000])
session.commit()
print(f"r1_bill_lines.payment_mode backfilled for {len(updates)} Paytm row(s)")

report = src.persist_digital_mode_agg(session, run.id, bill_lines)
print(f"digital_mode_agg: {report['rows']} rows, "
     f"other_matched_count={report['other_matched_count']}, "
     f"other_matched_value={report['other_matched_value']:,.2f}")

session.close()
