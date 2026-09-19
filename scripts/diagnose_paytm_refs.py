"""For each unmatched referenced-Paytm bill: find its reference in the Paytm
data regardless of date/amount, and report what the gateway actually says.

The Paytm transaction_id leads with YYYYMMDD, so the reference itself carries
the date the gateway assigned — which can be compared with the POS business
date. Read-only; changes no matching logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.db import R1BillLine, Run, get_session, init_db
from mop_reco.exceptions import date_from_paytm_txn_id
from mop_reco.io_ import clean_ref

WAREHOUSE = ROOT / "warehouse"
REFERENCED_PAYTM_MOPS = ("PAYTM EDC", "PaytmDQR", "PAYTM DQR MOREFUN")

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_colwidth", 40)

session = get_session(init_db())
run = session.query(Run).order_by(Run.started_at.desc()).first()

bills = pd.DataFrame([{
    "bill_date": r.business_date,
    "store": r.store,
    "bill_no": r.bill_no,
    "mop": r.mop,
    "bill_amount": float(r.amount or 0),
    "paytm_ref": r.paytm_ref,
} for r in session.query(R1BillLine).filter(
    R1BillLine.run_id == run.id,
    R1BillLine.matched.is_(False),
    R1BillLine.mop.in_(REFERENCED_PAYTM_MOPS),
).all()])

# Whole-month Paytm pool, indexed by transaction_id.
frames = []
for path in sorted((WAREHOUSE / "paytm_settlement").glob("*.parquet")):
    frames.append(pd.read_parquet(path, columns=[
        "business_date", "store", "amount", "transaction_id", "status",
        "transaction_type", "payment_mode", "settled_date"]))
paytm = pd.concat(frames, ignore_index=True)
paytm["transaction_id"] = paytm["transaction_id"].astype(str).str.strip()
paytm["business_date"] = paytm["business_date"].astype(str)
lookup = paytm.set_index("transaction_id")

print(f"Paytm pool: {len(paytm):,} rows, {paytm['business_date'].min()} .. "
      f"{paytm['business_date'].max()}")
print(f"Unmatched referenced-Paytm bills: {len(bills)}\n")

rows = []
for _, b in bills.iterrows():
    ref = clean_ref(b["paytm_ref"])
    ref_date = date_from_paytm_txn_id(ref)
    found = ref in lookup.index

    txn_date = txn_amount = txn_store = txn_status = txn_type = None
    if found:
        hit = lookup.loc[ref]
        if isinstance(hit, pd.DataFrame):
            hit = hit.iloc[0]
        txn_date = hit["business_date"]
        txn_amount = float(hit["amount"])
        txn_store = hit["store"]
        txn_status = hit["status"]
        txn_type = hit["transaction_type"]

    rows.append({
        "bill_date": b["bill_date"],
        "store": b["store"],
        "bill_no": b["bill_no"],
        "mop": b["mop"],
        "bill_amount": b["bill_amount"],
        "ref_date_in_id": str(ref_date) if ref_date else None,
        "days_off": (ref_date - pd.Timestamp(b["bill_date"]).date()).days if ref_date else None,
        "ref_in_paytm": found,
        "txn_date": txn_date,
        "txn_amount": txn_amount,
        "txn_store": txn_store,
        "txn_status": txn_status,
        "txn_type": txn_type,
    })

df = pd.DataFrame(rows)

print("=" * 78)
print("EACH BILL, AND WHAT THE GATEWAY ACTUALLY SAYS ABOUT ITS REFERENCE")
print("=" * 78)
print(df[["bill_date", "store", "bill_no", "mop", "bill_amount", "ref_date_in_id",
          "days_off", "ref_in_paytm", "txn_date", "txn_amount", "txn_status"]]
      .to_string(index=False))

print("\n" + "=" * 78)
print("THE REFERENCE'S OWN DATE vs THE BILL'S BUSINESS DATE")
print("=" * 78)
print(df.groupby("days_off").size().rename("bills").reset_index().to_string(index=False))
print("\n(days_off = date encoded in the Paytm reference minus the POS business date.")
print(" Tier 1 requires the SAME business date, so anything non-zero cannot match.)")

print("\n" + "=" * 78)
print("CAUSE")
print("=" * 78)


def cause(r):
    if not r["ref_in_paytm"]:
        if r["days_off"] not in (0, None):
            return (f"reference dated {r['days_off']:+d} day(s) from the bill AND absent "
                    "from the loaded Paytm data")
        return "reference_present_txn_not_found — searched, genuinely absent"
    if r["txn_date"] != r["bill_date"]:
        return (f"txn found but dated {r['txn_date']} vs bill {r['bill_date']} — "
                "same-date rule blocks the match")
    if abs(float(r["txn_amount"] or 0) - float(r["bill_amount"])) > 0.005:
        return "amount_mismatch"
    return "found, same date, same amount — DEFECT"


df["cause"] = df.apply(cause, axis=1)
summary = df.groupby("cause").agg(bills=("bill_no", "size"),
                                  value=("bill_amount", "sum")).reset_index()
print(summary.sort_values("bills", ascending=False).to_string(index=False))

out = ROOT / "logs" / "paytm_ref_diagnostic.csv"
df.to_csv(out, index=False)
print(f"\nwritten to {out}")
session.close()
