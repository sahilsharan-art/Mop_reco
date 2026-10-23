"""Compare the CURRENT warehouse (what run #N reconciled) with a PREVIEW warehouse built from the replaced settlement files.

    python scripts/settlement_reingest_compare.py <preview_warehouse_dir> [run_id]

Read-only. Writes exports/settlement_reingest/compare_*.csv and compare_summary.json:
  1. row-level diff per gateway and month: transactions ADDED, REMOVED, RESTATED (which columns changed), unchanged;
  2. the effect on matches of the run: every bill the run matched (Tier 1 or EDC OTHER) whose transaction is now missing, has a
     different amount, or is no longer SUCCESS / ACQUIRING (would become UNMATCHED), and every open bill whose gateway reference
     now exists with the exact amount (would become MATCHED). It tests each pair on its own, not the consumption order, so it is an
     estimate of what a re-run will do, listed in full so nothing is silent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.db import get_engine  # noqa: E402

new_wh = Path(sys.argv[1])
run_id = int(sys.argv[2]) if len(sys.argv) > 2 else 15
old_wh = ROOT / "warehouse"
out = ROOT / "exports" / "settlement_reingest"
out.mkdir(parents=True, exist_ok=True)

SPEC = {
    "paytm": dict(folder="paytm_settlement", key="transaction_id",
                  cols=["transaction_id", "business_date", "amount", "settled_amount", "status", "transaction_type", "utr_no",
                        "payout_id", "original_mid", "commission", "gst"],
                  watch=["business_date", "amount", "settled_amount", "status", "transaction_type", "utr_no", "payout_id",
                         "original_mid", "commission", "gst"]),
    "axis": dict(folder="axis_edc", key="RRN",
                 cols=["RRN", "business_date", "GROSS_AMT", "NET_AMT", "MDR", "GST", "UTR", "MID"],
                 watch=["business_date", "GROSS_AMT", "NET_AMT", "MDR", "GST", "UTR", "MID"]),
}


def load(wh: Path, folder: str, cols: list) -> pd.DataFrame:
    parts = []
    for p in sorted((wh / folder).glob("*.parquet")):
        have = pd.read_parquet(p).columns
        d = pd.read_parquet(p, columns=[c for c in cols if c in have])
        parts.append(d)
    d = pd.concat(parts, ignore_index=True)
    return d


summary = {"run_id": run_id, "diff": {}, "match_impact": {}}
pools = {}
for gw, sp in SPEC.items():
    old, new = load(old_wh, sp["folder"], sp["cols"]), load(new_wh, sp["folder"], sp["cols"])
    key = sp["key"]
    for d in (old, new):
        d["_k"] = d[key].astype(str)
        d["month"] = d["business_date"].astype(str).str[:7]
        # a blank key cannot be compared - keep every such row distinct
    old_dup, new_dup = int(old["_k"].duplicated().sum()), int(new["_k"].duplicated().sum())
    o, n = old.drop_duplicates("_k").set_index("_k"), new.drop_duplicates("_k").set_index("_k")
    added_idx, removed_idx = n.index.difference(o.index), o.index.difference(n.index)
    both = o.index.intersection(n.index)
    ob, nb = o.loc[both], n.loc[both]
    changed_cols = {}
    restated = pd.Series(False, index=both)
    for c in sp["watch"]:
        if c not in ob.columns or c not in nb.columns:
            continue
        a, b = ob[c], nb[c]
        if c in ("amount", "settled_amount", "commission", "gst", "GROSS_AMT", "NET_AMT", "MDR", "GST"):
            diff = ~((pd.to_numeric(a, errors="coerce").fillna(0) - pd.to_numeric(b, errors="coerce").fillna(0)).abs() < 0.005)
        else:
            diff = a.astype(str) != b.astype(str)
        changed_cols[c] = int(diff.sum())
        restated |= diff
    rows = []
    for month in sorted(set(old["month"]) | set(new["month"])):
        rows.append({"gateway": gw, "month": month, "rows_before": int((old.drop_duplicates('_k')["month"] == month).sum()),
                     "rows_after": int((new.drop_duplicates('_k')["month"] == month).sum()),
                     "added": int((n.loc[added_idx, "month"] == month).sum()),
                     "removed": int((o.loc[removed_idx, "month"] == month).sum()),
                     "restated": int(((nb["month"] == month) & restated).sum()),
                     "added_value": round(float(pd.to_numeric(n.loc[added_idx][n.loc[added_idx, "month"] == month][SPEC[gw]["cols"][2]], errors="coerce").sum()), 2),
                     "removed_value": round(float(pd.to_numeric(o.loc[removed_idx][o.loc[removed_idx, "month"] == month][SPEC[gw]["cols"][2]], errors="coerce").sum()), 2)})
    pd.DataFrame(rows).to_csv(out / f"compare_rows_{gw}.csv", index=False)
    summary["diff"][gw] = {"rows_before": len(old), "rows_after": len(new), "duplicate_keys_before": old_dup,
                           "duplicate_keys_after": new_dup, "added": len(added_idx), "removed": len(removed_idx),
                           "restated": int(restated.sum()), "restated_by_column": changed_cols}
    for label, idx, src in (("added", added_idx, n), ("removed", removed_idx, o)):
        src.loc[idx].drop(columns=["month"]).head(200000).to_csv(out / f"compare_{label}_{gw}.csv")
    ob.loc[restated[restated].index].join(nb.loc[restated[restated].index], lsuffix="_before", rsuffix="_after").to_csv(
        out / f"compare_restated_{gw}.csv")
    pools[gw] = n
    print(gw, summary["diff"][gw])

# ---- the effect on matches -----------------------------------------------------------------------------------------
eng = get_engine()
impact = {}
for gw, gname in (("paytm", "paytm"), ("axis", "axis")):
    pool = pools[gw]
    amt_col = "amount" if gw == "paytm" else "GROSS_AMT"
    stat = pool["status"].astype(str).str.upper() if "status" in pool.columns else None
    typ = pool["transaction_type"].astype(str).str.upper() if "transaction_type" in pool.columns else None
    ok_sale = pd.Series(True, index=pool.index)
    if stat is not None:
        ok_sale &= stat == "SUCCESS"
    if typ is not None:
        ok_sale &= typ == "ACQUIRING"
    pool_amt = pd.to_numeric(pool[amt_col], errors="coerce")
    broke, fixed_rows = [], []
    q = text("select bill_no, business_date, store, amount, matched_txn_ref, match_tier from r1_bill_lines "
             "where run_id=:r and matched=1 and matched_gateway=:g")
    for chunk in pd.read_sql(q, eng, params={"r": run_id, "g": gname}, chunksize=500000):
        chunk["ref"] = chunk["matched_txn_ref"].astype(str)
        present = chunk["ref"].isin(pool.index)
        why = pd.Series("", index=chunk.index)
        why[~present] = "transaction no longer in the settlement data"
        sub = chunk[present]
        pa, ok = pool_amt.reindex(sub["ref"]).to_numpy(), ok_sale.reindex(sub["ref"]).to_numpy()
        bad_amt = (pd.to_numeric(sub["amount"]).to_numpy() - pa).__abs__() >= 0.005
        why.loc[sub.index[bad_amt]] = "transaction amount changed: now " + pd.Series(pa[bad_amt]).astype(str).values
        why.loc[sub.index[~ok & ~bad_amt]] = "transaction is no longer a SUCCESS / ACQUIRING sale"
        bad = chunk[why != ""].copy()
        bad["reason"] = why[why != ""]
        broke.append(bad)
    broke = pd.concat(broke) if broke else pd.DataFrame()
    broke.to_csv(out / f"compare_matched_would_break_{gw}.csv", index=False)
    # open bills whose reference now resolves with the exact amount
    refcol = "paytm_ref" if gw == "paytm" else "axis_ref"
    q2 = text(f"select bill_no, business_date, store, amount, {refcol} as ref from r1_bill_lines "
              f"where run_id=:r and matched=0 and {refcol} is not null and {refcol} != ''")
    opened = pd.read_sql(q2, eng, params={"r": run_id})
    opened["ref"] = opened["ref"].astype(str)
    hit = opened[opened["ref"].isin(pool.index)].copy()
    if len(hit):
        hit["txn_amount"] = pool_amt.reindex(hit["ref"]).to_numpy()
        hit["sale_ok"] = ok_sale.reindex(hit["ref"]).to_numpy()
        hit = hit[(hit["txn_amount"] - pd.to_numeric(hit["amount"])).abs() < 0.005]
        hit = hit[hit["sale_ok"]]
    hit.to_csv(out / f"compare_open_would_match_{gw}.csv", index=False)
    impact[gw] = {"matched_bills_that_would_break": int(len(broke)),
                  "matched_value_that_would_break": round(float(pd.to_numeric(broke["amount"]).sum()), 2) if len(broke) else 0.0,
                  "broken_by_reason": broke["reason"].str.split(":").str[0].value_counts().to_dict() if len(broke) else {},
                  "open_bills_with_a_reference": int(len(opened)),
                  "open_bills_that_would_match": int(len(hit)),
                  "open_value_that_would_match": round(float(pd.to_numeric(hit["amount"]).sum()), 2) if len(hit) else 0.0}
    print(gw, impact[gw])
summary["match_impact"] = impact
(out / "compare_summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
print("compare done")
