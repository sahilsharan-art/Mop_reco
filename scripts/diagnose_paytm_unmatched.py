"""Diagnostic: why is any PAYTM EDC / PaytmDQR / PAYTM DQR MOREFUN bill unmatched?

Those bills are raised only after the payment succeeds, with the gateway
reference written into the bill at that moment — so an unmatched one is our
defect or a missing input, not a business outcome.

Read-only. Changes no matching logic. Run from the project root:
    python scripts/diagnose_paytm_unmatched.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.db import R1BillLine, Run, get_session, init_db
from mop_reco.formatting import money_table
from mop_reco.io_ import clean_ref

WAREHOUSE = ROOT / "warehouse"

# The MOPs that must always carry a reference. EDC OTHER is excluded on
# purpose — having no reference there is expected and is not the question.
REFERENCED_PAYTM_MOPS = ("PAYTM EDC", "PaytmDQR", "PAYTM DQR MOREFUN")

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)


def rule(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


session = get_session(init_db())
run = session.query(Run).order_by(Run.started_at.desc()).first()
print(f"run #{run.id}  {run.started_at:%Y-%m-%d %H:%M}  status={run.status}")

# --- the unmatched population --------------------------------------------
unmatched = pd.DataFrame([{
    "business_date": r.business_date,
    "store": r.store,
    "bill_no": r.bill_no,
    "line": r.line_seq,
    "mop": r.mop,
    "amount": float(r.amount or 0),
    "status": r.status,
    "paytm_ref": r.paytm_ref,
    "axis_ref": r.axis_ref,
    "bajaj_ref": r.bajaj_ref,
} for r in session.query(R1BillLine).filter(
    R1BillLine.run_id == run.id,
    R1BillLine.matched.is_(False),
    R1BillLine.mop.in_(REFERENCED_PAYTM_MOPS),
).all()])

if unmatched.empty:
    print("\nNo unmatched bills on a referenced Paytm MOP. Nothing to diagnose.")
    raise SystemExit(0)

# --- (a) count and value, by date and by MOP -----------------------------
rule("(a) COUNT AND VALUE — by MOP, then by business date")
by_mop = unmatched.groupby("mop").agg(bills=("bill_no", "size"),
                                      value=("amount", "sum")).reset_index()
by_mop["value"] = by_mop["value"].map(money_table)
print(by_mop.to_string(index=False))
print()
by_date = unmatched.groupby(["business_date", "mop"]).agg(
    bills=("bill_no", "size"), value=("amount", "sum")).reset_index()
by_date["value"] = by_date["value"].map(money_table)
print(by_date.to_string(index=False))
print(f"\nTOTAL: {len(unmatched)} bills, Rs {unmatched['amount'].sum():,.2f}")

# --- (b) was a Paytm reference extracted? --------------------------------
rule("(b) WAS A PAYTM REFERENCE EXTRACTED FROM TPEDCRESPONSE?")
unmatched["has_paytm_ref"] = unmatched["paytm_ref"].notna()
print(unmatched.groupby(["mop", "has_paytm_ref"]).size()
      .rename("bills").reset_index().to_string(index=False))
with_ref = unmatched[unmatched["has_paytm_ref"]]
without_ref = unmatched[~unmatched["has_paytm_ref"]]
print(f"\nwith a reference   : {len(with_ref)}")
print(f"without a reference: {len(without_ref)}")

# --- pull the raw response for these bills from the warehouse ------------
dates = sorted(unmatched["business_date"].unique())
pos_frames = []
for d in dates:
    path = WAREHOUSE / "pos_bill_wise" / f"{d}.parquet"
    if path.exists():
        pos_frames.append(pd.read_parquet(
            path, columns=["business_date", "store", "bill_no", "mop", "amount",
                           "TPEDCRESPONSE", "tpedc_parse_failed", "paytm_ref"]))
pos = pd.concat(pos_frames, ignore_index=True) if pos_frames else pd.DataFrame()

raw = unmatched.merge(
    pos[["business_date", "bill_no", "mop", "amount", "TPEDCRESPONSE", "tpedc_parse_failed"]],
    on=["business_date", "bill_no", "mop", "amount"], how="left", suffixes=("", "_pos"))

# --- (c) raw TPEDCRESPONSE where no reference was extracted --------------
rule("(c) RAW TPEDCRESPONSE WHERE NO REFERENCE WAS EXTRACTED")
no_ref_raw = raw[~raw["has_paytm_ref"]]
if no_ref_raw.empty:
    print("None — every unmatched bill on these MOPs DID yield a Paytm reference.")
    print("So this is not a parse failure and not a missing reference in the file.")
else:
    print(f"{len(no_ref_raw)} bill(s); showing up to 10 raw responses verbatim:\n")
    for _, r in no_ref_raw.head(10).iterrows():
        print("-" * 78)
        print(f"{r['business_date']} {r['store']} {r['bill_no']} {r['mop']} "
              f"Rs {r['amount']:,.2f}  parse_failed={r.get('tpedc_parse_failed')}")
        blob = r.get("TPEDCRESPONSE")
        print(f"TPEDCRESPONSE: {blob!r}"[:3000])

# --- (d) was the reference searched, and was the report loaded? ----------
rule("(d) WAS THE REFERENCE SEARCHED, IN WHICH REPORT, AND WAS THAT REPORT LOADED?")
paytm_by_date = {}
paytm_frames = []
for path in sorted((WAREHOUSE / "paytm_settlement").glob("*.parquet")):
    df = pd.read_parquet(path, columns=["business_date", "store", "amount",
                                        "transaction_id", "status", "transaction_type"])
    paytm_by_date[path.stem] = len(df)
    paytm_frames.append(df)
paytm = pd.concat(paytm_frames, ignore_index=True)
paytm["business_date"] = paytm["business_date"].astype(str)
paytm["txn_ref"] = paytm["transaction_id"].astype(str).str.strip()

rows = []
for d in dates:
    n = paytm_by_date.get(d)
    subset = with_ref[with_ref["business_date"] == d]
    rows.append({
        "business_date": d,
        "unmatched_with_ref": len(subset),
        "paytm_file_loaded_for_date": "yes" if n else "NO",
        "paytm_rows_that_date": n or 0,
    })
print(pd.DataFrame(rows).to_string(index=False))
print("\nEvery reference above was searched against the Paytm settlement report "
      "(bill paytm_ref -> Paytm transaction_id), same business date, exact amount, "
      "status SUCCESS and transaction_type ACQUIRING.")

# Does the reference exist ANYWHERE in the Paytm data, ignoring date/amount?
ref_index = set(paytm["txn_ref"])
with_ref = with_ref.copy()
with_ref["ref_exists_anywhere_in_paytm"] = with_ref["paytm_ref"].astype(str).map(
    lambda r: clean_ref(r) in ref_index if r else False)
print(f"\nreference present somewhere in the Paytm data : "
      f"{int(with_ref['ref_exists_anywhere_in_paytm'].sum())} of {len(with_ref)}")

# --- (e) is there a plausible candidate on store + amount + date? --------
rule("(e) IS THERE A PLAUSIBLE CANDIDATE ON STORE + AMOUNT + DATE, IGNORING REFERENCE?")
paytm["amount_r"] = paytm["amount"].astype(float).round(2)
successful = paytm[paytm["status"].str.upper().eq("SUCCESS")
                   & paytm["transaction_type"].str.upper().eq("ACQUIRING")]
cand_index = (successful.groupby(["store", "business_date", "amount_r"])
              .size().to_dict())

def candidates_for(row):
    return cand_index.get((row["store"], row["business_date"], round(row["amount"], 2)), 0)

unmatched["candidates_same_store_date_amount"] = unmatched.apply(candidates_for, axis=1)
has_cand = unmatched[unmatched["candidates_same_store_date_amount"] > 0]
print(f"bills with at least one plausible candidate : {len(has_cand)} of {len(unmatched)}")
print(f"bills with NO candidate at all             : "
      f"{len(unmatched) - len(has_cand)} of {len(unmatched)}")
if not has_cand.empty:
    print("\nThese SHOULD have matched — a defect if the reference was searched properly:")
    print(has_cand[["business_date", "store", "bill_no", "mop", "amount",
                    "candidates_same_store_date_amount"]].to_string(index=False))

# --- (f) group by likely cause -------------------------------------------
rule("(f) LIKELY CAUSE")


def classify(row):
    if bool(row.get("tpedc_parse_failed")):
        return "parse_failed — response present but unreadable"
    if not row["has_paytm_ref"]:
        return "no_reference_at_pos — nothing extracted from the response"
    if not paytm_by_date.get(row["business_date"]):
        return "gateway_file_missing_for_that_date"
    if row["candidates_same_store_date_amount"] > 0:
        return "DEFECT — candidate exists on store+date+amount but the bill did not match"
    ref = clean_ref(row["paytm_ref"]) if row["paytm_ref"] else None
    if ref and ref in ref_index:
        return "reference_found_in_file_but_not_on_this_date_or_amount"
    return "reference_present_txn_genuinely_absent_from_report"


merged = unmatched.merge(
    raw[["business_date", "bill_no", "mop", "amount", "tpedc_parse_failed"]],
    on=["business_date", "bill_no", "mop", "amount"], how="left")
merged["likely_cause"] = merged.apply(classify, axis=1)

summary = merged.groupby("likely_cause").agg(
    bills=("bill_no", "size"), value=("amount", "sum")).reset_index()
summary = summary.sort_values("bills", ascending=False)
summary["value"] = summary["value"].map(money_table)
print(summary.to_string(index=False))

print("\nEvery affected bill:")
print(merged[["business_date", "store", "bill_no", "mop", "amount", "status",
              "paytm_ref", "likely_cause"]].to_string(index=False))

out = ROOT / "logs" / "paytm_unmatched_diagnostic.csv"
merged.to_csv(out, index=False)
print(f"\nfull detail written to {out}")
session.close()
