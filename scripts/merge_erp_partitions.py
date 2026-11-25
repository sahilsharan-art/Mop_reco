"""One-off: apply the ERP reader's "one line per bill x reference-less MOP" rule (CLAUDE.md 6.42) to the POS partitions already stored in the
warehouse by earlier ERP pulls. Days older than the platform's re-read window are never pulled again, so without this they would keep the
ERP's one-row-per-tender shape. Idempotent: a partition that is already merged is left untouched (no write). Reads and writes only
warehouse/pos_bill_wise/<date>.parquet; the file-derived copies in warehouse/_file_baseline are never touched.

  run_in_app_env.ps1 scripts\\merge_erp_partitions.py            report only (nothing written)
  run_in_app_env.ps1 scripts\\merge_erp_partitions.py --apply    merge and write
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import ingest  # noqa: E402
from mop_reco.erp_source import merge_reference_less_lines  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--apply", action="store_true")
args = ap.parse_args()

part_dir = ingest.WAREHOUSE_ROOT / "pos_bill_wise"
files = sorted(part_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].parquet"))
changed = removed = 0
for f in files:
    df = pd.read_parquet(f)
    merged, n = merge_reference_less_lines(df)
    if n:
        changed += 1
        removed += n
        print(f"{f.name}: {n} line(s) merged")
        if args.apply:
            tmp = f.with_suffix(".tmp")
            merged.to_parquet(tmp, index=False)
            tmp.replace(f)
print(f"{len(files)} partition(s) read; {changed} {'rewritten' if args.apply else 'would change'}; {removed} line(s) merged away")
