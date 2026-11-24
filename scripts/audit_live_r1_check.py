"""Audit Phase 2: independent re-performance of R1 (bill <-> gateway transaction) against a stored run. READ-ONLY.

    deploy\\run_in_app_env.ps1 scripts\\audit_live_r1_check.py --run 2 --from 2026-04-01 --to 2026-11-14

Written from the specification (CLAUDE.md 6.2 / 6.3), NOT from the matching engine's code, and compared with what the engine STORED:
  A. every stored reference match is real: the transaction exists in the raw gateway data (SUCCESS, ACQUIRING), its amount equals the bill amount to
     the paisa, and the reference used is the bill's own reference for that gateway
  B. no transaction is consumed by two bill lines
  C. nothing eligible was missed: no unmatched bill line holds a reference to a SUCCESS/ACQUIRING transaction of exactly its amount that no other
     bill line consumed
  D. every EDC OTHER (tier 2) match has the same store, the same amount and the same date as its transaction
  E. every SUCCESS/ACQUIRING transaction in the range that no bill consumed is in the exception register (and the register holds nothing consumed)
Exact integer paise throughout. Exit code 0 only if every check holds. Dates are arguments (CLAUDE.md rule 8).
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(os.environ.get("MOP_ROOT") or Path(__file__).resolve().parents[1])
for _c in (Path(__file__).resolve().parents[1] / "app", ROOT / "app"):
    if _c.is_dir():
        sys.path.insert(0, str(_c))
from mop_reco.db import get_engine, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run", type=int, required=True)
ap.add_argument("--from", dest="lo", required=True)
ap.add_argument("--to", dest="hi", required=True)
args = ap.parse_args()
LO, HI = date.fromisoformat(args.lo), date.fromisoformat(args.hi)
WH = ROOT / "warehouse"
engine = init_db(get_engine())
results = []


def report(name, ok, detail=""):
    results.append(ok)
    print(f"{'OK  ' if ok else 'FAIL'} {name}{(' - ' + detail) if detail else ''}")


def paise(series) -> pd.Series:
    return pd.Series(np.rint(series.astype(float).to_numpy() * 100).astype("int64"), index=series.index)


print(f"Audit re-performance of R1, run {args.run}, bill dates {LO} to {HI}\n")
r1 = pd.read_sql(text("select bill_no, line_seq, business_date, store, mop, amount, paytm_ref, axis_ref, bajaj_ref, matched, match_tier, matched_gateway, "
                      "matched_txn_ref from r1_bill_lines where run_id = :r and business_date >= :a and business_date <= :b"), engine,
                 params={"r": args.run, "a": LO.isoformat(), "b": HI.isoformat()})
r1["p"] = paise(r1["amount"])
print(f"stored R1 lines: {len(r1):,}; matched {int(r1.matched.sum()):,}; tiers: {r1.loc[r1.matched, 'match_tier'].value_counts(dropna=False).to_dict()}")

# the raw gateway pool, built from the partitions with the rules of 6.3 (SUCCESS and ACQUIRING only; Bajaj status case-insensitive)
frames = []
for source, cols, ren in (
        ("paytm_settlement", ["transaction_id", "amount", "status", "transaction_type", "business_date", "store"], {"transaction_id": "ref", "transaction_type": "type"}),
        ("axis_edc", ["RRN", "GROSS_AMT", "business_date", "store"], {"RRN": "ref", "GROSS_AMT": "amount"}),
        ("bajaj", ["Transaction Id", "Transaction Amount", "Transaction Status", "business_date", "store"],
         {"Transaction Id": "ref", "Transaction Amount": "amount", "Transaction Status": "status"})):
    parts = []
    for f in sorted((WH / source).glob("*.parquet")):
        parts.append(pd.read_parquet(f, columns=cols))
    t = pd.concat(parts, ignore_index=True).rename(columns=ren)
    t["gateway"] = {"paytm_settlement": "paytm", "axis_edc": "axis", "bajaj": "bajaj"}[source]
    if "status" not in t.columns:
        t["status"] = "SUCCESS"
    if "type" not in t.columns:
        t["type"] = "ACQUIRING"
    frames.append(t[["gateway", "ref", "amount", "status", "type", "business_date", "store"]])
pool = pd.concat(frames, ignore_index=True)
pool["ref"] = pool["ref"].astype(str).str.strip()
pool["bd"] = pd.to_datetime(pool["business_date"]).dt.strftime("%Y-%m-%d")
pool_all = pool.copy()
pool = pool[pool["status"].astype(str).str.upper().eq("SUCCESS") & pool["type"].astype(str).str.upper().eq("ACQUIRING")].copy()
pool["p"] = paise(pool["amount"])
print(f"raw gateway rows {len(pool_all):,}; SUCCESS and ACQUIRING {len(pool):,} (paytm {int((pool.gateway == 'paytm').sum()):,}, axis {int((pool.gateway == 'axis').sum()):,}, "
      f"bajaj {int((pool.gateway == 'bajaj').sum()):,})")
key = lambda df, g, r: list(zip(df[g], df[r]))  # noqa: E731
first = pool.drop_duplicates(["gateway", "ref"]).set_index(["gateway", "ref"])
dups = int(pool.duplicated(["gateway", "ref"]).sum())
print(f"     (information) SUCCESS/ACQUIRING references that appear more than once in the raw files: {dups:,}")

matched = r1[r1["matched"]].copy()
t1 = matched[~matched["match_tier"].astype(str).str.contains("2|edc", case=False, regex=True)]
t2 = matched[matched["match_tier"].astype(str).str.contains("2|edc", case=False, regex=True)]

# A. every tier-1 match is real
idx = pd.MultiIndex.from_arrays([t1["matched_gateway"], t1["matched_txn_ref"].astype(str)])
exists = idx.isin(first.index)
report("A1 every tier-1 match points at a SUCCESS/ACQUIRING transaction that exists in the raw gateway data", bool(exists.all()),
       f"{len(t1):,} matches, {int((~exists).sum()):,} point at a transaction that is not there" + (f"; first {t1.loc[~exists, ['bill_no', 'matched_gateway', 'matched_txn_ref']].head(3).values.tolist()}" if (~exists).any() else ""))
pool_triples = set(zip(pool["gateway"], pool["ref"], pool["p"]))      # a reference can sit on several raw rows with different amounts
amount_ok = np.array([(g, r, p) in pool_triples for g, r, p in zip(t1["matched_gateway"], t1["matched_txn_ref"].astype(str), t1["p"])])
report("A2 every tier-1 match has exactly the bill's amount on at least one raw row of that reference (paisa for paisa)", bool(amount_ok.all()),
       f"{int((~amount_ok).sum()):,} differ" + (f"; first {t1.loc[~amount_ok, ['bill_no', 'amount', 'matched_txn_ref']].head(3).values.tolist()}" if (~amount_ok).any() else ""))
own = np.select([t1["matched_gateway"] == "paytm", t1["matched_gateway"] == "axis", t1["matched_gateway"] == "bajaj"],
                [t1["paytm_ref"].astype(str), t1["axis_ref"].astype(str), t1["bajaj_ref"].astype(str)], default="")
ref_ok = own == t1["matched_txn_ref"].astype(str).to_numpy()
report("A3 the reference used is the bill's own reference for that gateway", bool(ref_ok.all()), f"{int((~ref_ok).sum()):,} use a different reference")

# B. consumed once
cons = matched.groupby(["matched_gateway", "matched_txn_ref"]).size()
multi = cons[cons > 1]
report("B no transaction is consumed by two bill lines", len(multi) == 0, f"{len(multi):,} transactions used more than once" + (f"; first {multi.head(3).index.tolist()}" if len(multi) else ""))

# C. nothing eligible missed
consumed = set(zip(matched["matched_gateway"], matched["matched_txn_ref"].astype(str)))
un = r1[~r1["matched"]]
missed = []
for gw, col in (("paytm", "paytm_ref"), ("axis", "axis_ref"), ("bajaj", "bajaj_ref")):
    u = un[un[col].notna() & (un[col].astype(str).str.strip() != "")][["bill_no", "business_date", "p", col]].copy()
    u["ref"] = u[col].astype(str).str.strip()
    j = u.merge(pool[pool.gateway == gw][["ref", "p"]].drop_duplicates("ref"), on="ref", how="inner", suffixes=("", "_txn"))
    j = j[(j["p"] == j["p_txn"]) & ~j["ref"].map(lambda r: (gw, r) in consumed)]
    j["gateway"] = gw
    missed.append(j[["bill_no", "business_date", "gateway", "ref", "p"]])
missed = pd.concat(missed, ignore_index=True) if missed else pd.DataFrame()
report("C no unmatched bill line holds a reference to an eligible, unconsumed transaction of exactly its amount", len(missed) == 0,
       f"{len(missed):,} possible missed matches" + (f"; first {missed.head(3).values.tolist()}" if len(missed) else ""))

# D. tier-2 sanity
if len(t2):
    quad = set(zip(pool["gateway"], pool["ref"], pool["p"], pool["store"], pool["bd"]))
    bad = np.array([(g, r, p, st, d) not in quad for g, r, p, st, d in
                    zip(t2["matched_gateway"], t2["matched_txn_ref"].astype(str), t2["p"], t2["store"], t2["business_date"])])
    report("D every EDC OTHER match has the same store, amount and date as its transaction", not bad.any(), f"{len(t2):,} matches, {int(bad.sum()):,} violate" +
           (f"; first {t2.loc[bad, ['bill_no', 'store', 'amount', 'matched_txn_ref']].head(3).values.tolist()}" if bad.any() else ""))
else:
    report("D EDC OTHER matches", True, "none stored in this run")

# E. unmapped transactions are all in the register
reg = pd.read_sql(text("select exception_key, status from exceptions where exception_key like 'paytm|%' or exception_key like 'axis|%' or exception_key like 'bajaj|%'"), engine)
reg_keys = set(reg["exception_key"].astype(str))
in_window = pool[(pool["bd"] >= LO.isoformat()) & (pool["bd"] <= HI.isoformat())]
noid_rows = int(in_window["ref"].isna().sum())                       # rows with no transaction id: tracked under NOID- labels (CLAUDE.md 6.35)
in_range = in_window[in_window["ref"].notna()].drop_duplicates(["gateway", "ref"])
unconsumed = in_range[[(g, r) not in consumed for g, r in zip(in_range["gateway"], in_range["ref"])]]
unconsumed_keys = {f"{g}|{r}" for g, r in zip(unconsumed["gateway"], unconsumed["ref"])}
noid_registered = int(reg["exception_key"].astype(str).str.contains("NOID-").sum())
report("E0 every raw row with no transaction id has its own NOID- exception", noid_rows == noid_registered,
       f"{noid_rows:,} id-less raw rows in range, {noid_registered:,} NOID- exceptions in the register")
not_registered = unconsumed_keys - reg_keys
report("E every SUCCESS/ACQUIRING transaction in range that no bill consumed is in the exception register", len(not_registered) == 0,
       f"{len(unconsumed_keys):,} unconsumed, {len(not_registered):,} not in the register" + (f"; first {sorted(not_registered)[:3]}" if not_registered else ""))
open_keys = set(reg.loc[reg["status"].isin(["open", "under_investigation"]), "exception_key"])
consumed_but_open = {f"{g}|{r}" for g, r in consumed} & open_keys
rows_per_ref = pool.groupby(["gateway", "ref"]).size()
unexplained = {k for k in consumed_but_open if rows_per_ref.get(tuple(k.split("|", 1)), 0) < 2}
print(f"     (information) {len(consumed_but_open):,} open exceptions share a key with a consumed transaction; {len(consumed_but_open) - len(unexplained):,} of them "
      f"because the raw file holds the SAME reference on two or more rows (the known duplicate-reference lines, CLAUDE.md 6.23); "
      f"by gateway {pd.Series([k.split('|')[0] for k in consumed_but_open]).value_counts().to_dict()}")
report("E2 every open exception that shares a key with a consumed transaction is explained by a repeated reference in the raw file", len(unexplained) == 0,
       f"{len(unexplained):,} unexplained" + (f"; first {sorted(unexplained)[:3]}" if unexplained else ""))

print("\nVERDICT:", "EVERY CHECK HOLDS." if all(results) else f"{results.count(False)} CHECK(S) FAILED - see above.")
sys.exit(0 if all(results) else 1)
