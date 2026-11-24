"""Independent check of the gateways' own settlement arithmetic in the loaded data (audit Phase 9).

R2 (transaction -> settlement -> bank) is not implemented in the application and its bank leg has no data. What CAN be checked is the foundation it
would stand on: in each gateway's own file, does gross - charges - GST equal the net credited, does every payout carry one UTR, and how long
do credits take? Amounts are compared in whole paise (integers). Reads the warehouse read-only.

    python scripts/audit_gateway_arithmetic.py
"""
import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def paise(s):
    return np.rint(pd.to_numeric(s, errors="coerce") * 100).astype("Int64")


def load(src, cols):
    return pd.concat([pd.read_parquet(f, columns=cols) for f in sorted(glob.glob(str(ROOT / "warehouse" / src / "2*.parquet")))], ignore_index=True)


print("=== Paytm")
p = load("paytm_settlement", ["transaction_id", "status", "transaction_type", "amount", "commission", "gst", "settled_amount", "payout_id", "utr_no",
                              "business_date", "settled_date", "payment_mode"])
print(f"rows {len(p):,}; by type {p['transaction_type'].value_counts().to_dict()}")
sale = p[p["transaction_type"].astype(str).str.upper().eq("ACQUIRING") & p["status"].astype(str).str.upper().eq("SUCCESS")].copy()
sale["g"], sale["c"], sale["t"], sale["n"] = paise(sale["amount"]), paise(sale["commission"]), paise(sale["gst"]), paise(sale["settled_amount"])
has = sale["n"].notna()
exp = sale["g"] - sale["c"].fillna(0) - sale["t"].fillna(0)
diff = (sale["n"] - exp)[has]
bad = diff[diff != 0]
print(f"successful sales {len(sale):,}; with a settled amount {int(has.sum()):,}; without {int((~has).sum()):,}")
print(f"settled = amount - commission - GST exactly: {int((diff == 0).sum()):,} rows; differ: {len(bad):,} (sum of differences Rs {bad.sum() / 100:,.2f}; largest {bad.abs().max() / 100 if len(bad) else 0:,.2f})")
if len(bad):
    print("   differences by size (paise):", bad.abs().value_counts().head(6).to_dict())
tot_g, tot_n = int(sale["g"].sum()), int(sale.loc[has, "n"].sum())
print(f"gross Rs {tot_g / 100:,.2f}; charges Rs {int(sale['c'].fillna(0).sum()) / 100:,.2f}; GST Rs {int(sale['t'].fillna(0).sum()) / 100:,.2f}; settled Rs {tot_n / 100:,.2f}")
# payouts: one UTR each, and no payout left without one
pp = sale[sale["payout_id"].notna()]
multi = pp.groupby("payout_id")["utr_no"].nunique()
print(f"payouts {pp['payout_id'].nunique():,}; payouts with more than one UTR: {int((multi > 1).sum())}; settled rows with no payout id: {int((has & sale['payout_id'].isna()).sum())}; with no UTR: {int((has & sale['utr_no'].isna()).sum())}")
lag = (pd.to_datetime(sale["settled_date"], errors="coerce") - pd.to_datetime(sale["business_date"], errors="coerce")).dt.days
print("settlement lag in days (successful sales):", lag.value_counts(dropna=False).sort_index().head(8).to_dict())
print("negative lag (settled before the sale):", int((lag < 0).sum()))

print("\n=== Axis EDC")
a = load("axis_edc", ["RRN", "GROSS_AMT", "MDR", "GST", "NET_AMT", "UTR", "business_date", "process_date"])
a["g"], a["m"], a["t"], a["n"] = paise(a["GROSS_AMT"]), paise(a["MDR"]), paise(a["GST"]), paise(a["NET_AMT"])
d = (a["n"] - (a["g"] - a["m"].fillna(0) - a["t"].fillna(0)))
print(f"rows {len(a):,}; net = gross - MDR - GST exactly: {int((d == 0).sum()):,}; differ: {int((d != 0).sum()):,} (sum Rs {d[d != 0].sum() / 100:,.2f}; largest {d.abs().max() / 100:,.2f})")
print(f"gross Rs {int(a['g'].sum()) / 100:,.2f}; MDR Rs {int(a['m'].fillna(0).sum()) / 100:,.2f}; GST Rs {int(a['t'].fillna(0).sum()) / 100:,.2f}; net Rs {int(a['n'].sum()) / 100:,.2f}; rows with no UTR {int(a['UTR'].isna().sum()):,}")
alag = (pd.to_datetime(a["process_date"], errors="coerce") - pd.to_datetime(a["business_date"], errors="coerce")).dt.days
print("process lag in days:", alag.value_counts(dropna=False).sort_index().head(8).to_dict())

print("\n=== Bajaj")
b = load("bajaj", ["Transaction Id", "Transaction Amount", "Transaction Status", "Net Amount", "Total MDR"])
b["g"], b["n"], b["m"] = paise(b["Transaction Amount"]), paise(b["Net Amount"]), paise(b["Total MDR"])
ok = b["Transaction Status"].astype(str).str.upper().eq("SUCCESS")
d = (b["n"] - (b["g"] - b["m"].fillna(0)))[ok & b["n"].notna()]
print(f"rows {len(b):,}; successful {int(ok.sum()):,}; with a net amount {int((ok & b['n'].notna()).sum()):,}; net = amount - MDR exactly: {int((d == 0).sum()):,}; differ: {int((d != 0).sum()):,} (sum Rs {d[d != 0].sum() / 100:,.2f}); no net amount: {int((ok & b['n'].isna()).sum()):,}")
