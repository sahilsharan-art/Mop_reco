"""Keep what the numbered operator folders produced, before they stop being read (run ONCE, before SOURCES_FROM=uploads).

The folders themselves are never touched (rule 7). This copies the warehouse partitions the folder-based runs wrote for the four
uploaded sources into warehouse/_folder_baseline/<source>/ and writes BASELINE.json: for every source, the raw files that were read
(path, size, modified time) and, per business date, the row count and the money total. The comparison after the uploads (the Upload
screen, scripts/compare_uploads_to_folders.py) reads this and nothing else of the folders.

Refuses to overwrite an existing baseline. Read-only apart from the new _folder_baseline folder.
    python scripts/snapshot_folder_baseline.py
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

SOURCES = {  # source -> (the money column, the key column) used for the per-date totals
    "paytm_settlement": ("amount", "transaction_id"),
    "axis_edc": ("GROSS_AMT", "RRN"),
    "bajaj": ("Transaction Amount", "Transaction Id"),
    "axis_cms": ("credit_amount", "SLIPNO"),
}


def main() -> int:
    wh = ROOT / "warehouse"
    out = wh / "_folder_baseline"
    if (out / "BASELINE.json").exists():
        print(f"A baseline already exists in {out}; not overwritten.")
        return 1
    from mop_reco.config import load_config
    import os
    os.environ.pop("MOP_SOURCES_FROM", None)          # the folder paths, whatever the environment says
    cfg = load_config()
    report = {"created_at": datetime.now().isoformat(timespec="seconds"), "sources": {}}
    for source, (money, key) in SOURCES.items():
        src = wh / source
        dst = out / source
        dst.mkdir(parents=True, exist_ok=True)
        per_date, rows, total = {}, 0, 0.0
        for p in sorted(src.glob("*.parquet")):
            shutil.copy2(p, dst / p.name)
            df = pd.read_parquet(p)
            amt = pd.to_numeric(df[money], errors="coerce").fillna(0.0).sum() if money in df.columns else None
            per_date[p.stem] = {"rows": int(len(df)), "amount": round(float(amt), 2) if amt is not None else None}
            rows += len(df)
            total += float(amt or 0.0)
        files = []
        status = cfg.source_status(source)
        for f in status.files:
            st = f.stat()
            files.append({"path": str(f), "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
        report["sources"][source] = {"money_column": money, "key_column": key, "dates": len(per_date), "rows": rows,
                                     "amount": round(total, 2), "raw_files": files, "per_date": per_date}
        print(f"{source:18s} {len(per_date):4d} dates {rows:>10,d} rows  amount {total:,.2f}  ({len(files)} raw files recorded)")
    (out / "BASELINE.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"baseline written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
