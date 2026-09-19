"""Counts under the proposed finer status vocabulary — REPORT ONLY.

Nothing is changed: this classifies the existing unmatched population under the
new names so the split can be seen before any code adopts them.

  no_reference_at_pos                     genuinely absent from the bill
  reference_present_gateway_file_missing  our data gap
  reference_present_txn_not_found         searched properly, genuinely absent
  amount_mismatch                         found, but the amount differs
  parse_failed                            response existed, unreadable
  (plus) reference_dated_another_day       found, but the same-date rule blocks it
  (plus) ambiguous_multiple_candidates     EDC OTHER with >1 candidate

Run from the project root:  python scripts/status_vocabulary_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.db import R1BillLine, Run, get_session, init_db
from mop_reco.exceptions import date_from_paytm_txn_id
from mop_reco.formatting import money_table
from mop_reco.io_ import clean_ref

WAREHOUSE = ROOT / "warehouse"
NO_REF_EXPECTED_MOPS = ("EDC OTHER", "EDC OTHER AXIS")

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)

session = get_session(init_db())
run = session.query(Run).order_by(Run.started_at.desc()).first()

unmatched = pd.DataFrame([{
    "business_date": r.business_date,
    "store": r.store,
    "bill_no": r.bill_no,
    "mop": r.mop,
    "amount": float(r.amount or 0),
    "current_status": r.status,
    "match_tier": r.match_tier,
    "paytm_ref": r.paytm_ref,
    "axis_ref": r.axis_ref,
    "bajaj_ref": r.bajaj_ref,
} for r in session.query(R1BillLine).filter(
    R1BillLine.run_id == run.id, R1BillLine.matched.is_(False)).all()])

print(f"run #{run.id} — {len(unmatched)} unmatched bill lines\n")

# --- what we need to classify against ------------------------------------
def load_pool(folder, cols):
    frames = []
    for path in sorted((WAREHOUSE / folder).glob("*.parquet")):
        if path.stem.startswith("_"):
            continue
        frames.append(pd.read_parquet(path, columns=cols))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)


paytm = load_pool("paytm_settlement", ["business_date", "amount", "transaction_id"])
paytm["transaction_id"] = paytm["transaction_id"].astype(str).str.strip()
paytm_index = paytm.set_index("transaction_id")[["business_date", "amount"]]
paytm_dates = set(paytm["business_date"].astype(str))

axis = load_pool("axis_edc", ["business_date", "GROSS_AMT", "RRN"])
axis["RRN"] = axis["RRN"].astype(str).str.strip()
axis_index = axis.set_index("RRN")[["business_date", "GROSS_AMT"]].rename(
    columns={"GROSS_AMT": "amount"})
axis_dates = set(axis["business_date"].astype(str))

bajaj = load_pool("bajaj", ["business_date", "Transaction Amount", "Transaction Id"])
bajaj["Transaction Id"] = bajaj["Transaction Id"].astype(str).str.strip()
bajaj_index = bajaj.set_index("Transaction Id")[["business_date", "Transaction Amount"]] \
    .rename(columns={"Transaction Amount": "amount"})
bajaj_dates = set(bajaj["business_date"].astype(str))

GATEWAYS = [
    ("paytm", "paytm_ref", paytm_index, paytm_dates),
    ("axis", "axis_ref", axis_index, axis_dates),
    ("bajaj", "bajaj_ref", bajaj_index, bajaj_dates),
]

# Parse failures, from the POS warehouse.
parse_failed_keys = set()
for path in sorted((WAREHOUSE / "pos_bill_wise").glob("*.parquet")):
    if path.stem.startswith("_"):
        continue
    df = pd.read_parquet(path, columns=["business_date", "bill_no", "mop",
                                        "tpedc_parse_failed"])
    bad = df[df["tpedc_parse_failed"].fillna(False).astype(bool)]
    for _, r in bad.iterrows():
        parse_failed_keys.add((str(r["business_date"]), r["bill_no"], r["mop"]))


def classify(row):
    key = (row["business_date"], row["bill_no"], row["mop"])
    if key in parse_failed_keys:
        return "parse_failed", ""

    if str(row["match_tier"]) == "Ambiguous":
        return "ambiguous_multiple_candidates", "EDC OTHER, more than one candidate"

    # pd.notna, not truthiness: a null reference column arrives as NaN, and
    # bool(NaN) is True — which would report a bill with no reference at all as
    # "reference present, transaction not found".
    def has_ref(col):
        value = row.get(col)
        return pd.notna(value) and str(value).strip() not in ("", "None", "nan")

    refs = [(name, clean_ref(row[col]), index, dates)
            for name, col, index, dates in GATEWAYS if has_ref(col)]

    if not refs:
        expected = row["mop"] in NO_REF_EXPECTED_MOPS
        return ("no_reference_at_pos",
                "expected for this MOP" if expected
                else "NOT expected for this MOP — bill should carry a reference")

    notes = []
    for name, ref, index, dates in refs:
        if row["business_date"] not in dates:
            return ("reference_present_gateway_file_missing",
                    f"{name} report has no rows for {row['business_date']}")
        if ref in index.index:
            hit = index.loc[ref]
            if isinstance(hit, pd.DataFrame):
                hit = hit.iloc[0]
            txn_date, txn_amount = str(hit["business_date"]), float(hit["amount"])
            if txn_date != row["business_date"]:
                ref_date = date_from_paytm_txn_id(ref)
                off = ((ref_date - pd.Timestamp(row["business_date"]).date()).days
                       if ref_date else None)
                return ("reference_dated_another_day",
                        f"{name} txn dated {txn_date} vs bill {row['business_date']}"
                        + (f" ({off:+d}d)" if off is not None else ""))
            if abs(txn_amount - row["amount"]) > 0.005:
                return ("amount_mismatch",
                        f"{name} txn {txn_amount:,.2f} vs bill {row['amount']:,.2f}")
            return ("found_same_date_same_amount_DEFECT", f"{name} — should have matched")
        notes.append(f"{name} searched, absent")

    return "reference_present_txn_not_found", "; ".join(notes)


classified = unmatched.apply(lambda r: pd.Series(classify(r), index=["new_status", "why"]),
                             axis=1)
unmatched = pd.concat([unmatched, classified], axis=1)

print("=" * 100)
print("COUNTS UNDER THE NEW VOCABULARY")
print("=" * 100)
summary = unmatched.groupby("new_status").agg(
    bills=("bill_no", "size"), value=("amount", "sum")).reset_index()
summary = summary.sort_values("bills", ascending=False)
summary["value"] = summary["value"].map(money_table)
print(summary.to_string(index=False))

print("\n" + "=" * 100)
print("NEW STATUS vs THE SINGLE CURRENT STATUS IT REPLACES")
print("=" * 100)
print(pd.crosstab(unmatched["current_status"], unmatched["new_status"]).to_string())

print("\n" + "=" * 100)
print("BY MOP — is 'no_reference_at_pos' confined to the MOPs where it is expected?")
print("=" * 100)
print(pd.crosstab(unmatched["mop"], unmatched["new_status"]).to_string())

for status in ("reference_dated_another_day", "found_same_date_same_amount_DEFECT",
               "reference_present_gateway_file_missing", "amount_mismatch",
               "parse_failed"):
    subset = unmatched[unmatched["new_status"] == status]
    if subset.empty:
        continue
    print(f"\n--- {status} ({len(subset)}) ---")
    print(subset[["business_date", "store", "bill_no", "mop", "amount", "why"]]
          .to_string(index=False))

out = ROOT / "logs" / "status_vocabulary_report.csv"
unmatched.to_csv(out, index=False)
print(f"\nwritten to {out}")
session.close()
