"""Snapshot Paytm and Axis settlement totals by month, read-only.

    python scripts/settlement_snapshot.py <label> [warehouse_dir] [run_id]

For each month and gateway: settlement row count and gross amount (read from the per-date warehouse Parquet), and the
gateway-side R1 consumption of the run (rows a bill matched / did not), from `txn_day_agg`. There is no R2
(settlement -> payout -> bank) reconciliation in this system yet (CLAUDE.md section 7), so "matched / unmatched" here means
what exists: sale transactions the engine matched to a bill, and those it did not.
Writes exports/settlement_reingest/<label>.json (and prints a table). Never writes to the database or the warehouse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco.db import get_engine, get_session  # noqa: E402

label = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
warehouse = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "-" else ROOT / "warehouse"
run_id = int(sys.argv[3]) if len(sys.argv) > 3 else None
MONTHS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
SPEC = {"paytm": ("paytm_settlement", "amount", "settled_amount", ["amount", "settled_amount", "commission", "gst", "status", "transaction_type"]),
        "axis": ("axis_edc", "GROSS_AMT", "NET_AMT", ["GROSS_AMT", "NET_AMT", "MDR", "GST"])}

out = {"label": label, "warehouse": str(warehouse), "rows": {}}
for gw, (folder, gross, net, cols) in SPEC.items():
    for month in MONTHS:
        n, g, nt = 0, 0.0, 0.0
        for p in sorted((warehouse / folder).glob(f"{month}-*.parquet")):
            d = pd.read_parquet(p, columns=[c for c in cols if c in pd.read_parquet(p).columns])
            n += len(d)
            g += float(pd.to_numeric(d[gross], errors="coerce").sum())
            nt += float(pd.to_numeric(d[net], errors="coerce").sum())
        out["rows"][f"{gw}|{month}"] = {"rows": n, "gross": round(g, 2), "net_or_settled": round(nt, 2)}

if run_id:
    s = get_session(get_engine())
    q = text("select gateway, substr(business_date,1,7) m, sum(pool_n), round(sum(pool_v),2), sum(consumed_n), "
             "round(sum(consumed_v),2) from txn_day_agg where run_id=:r and business_date>='2026-04-01' "
             "and business_date<='2026-09-30' group by 1,2")
    for gw, m, pn, pv, cn, cv in s.execute(q, {"r": run_id}):
        key = f"{gw}|{m}"
        if key in out["rows"]:
            out["rows"][key].update({"sale_rows": int(pn or 0), "sale_value": float(pv or 0),
                                     "matched_rows": int(cn or 0), "matched_value": float(cv or 0),
                                     "unmatched_rows": int((pn or 0) - (cn or 0)),
                                     "unmatched_value": round(float((pv or 0) - (cv or 0)), 2)})
dest = ROOT / "exports" / "settlement_reingest"
dest.mkdir(parents=True, exist_ok=True)
(dest / f"{label}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
frame = pd.DataFrame.from_dict(out["rows"], orient="index")
print(frame.to_string())
