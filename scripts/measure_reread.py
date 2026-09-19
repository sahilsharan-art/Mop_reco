"""Before/after: does re-ingesting an already-ingested POS file touch the .xlsb?

Run from the project root:  python scripts/measure_reread.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import psutil

from mop_reco.config import load_config
from mop_reco.db import get_engine, get_session, init_db
from mop_reco.ingest import ingest_pos_bill_wise, warehouse_manifest

config = load_config()
engine = get_engine(ROOT / "db" / "measure_reread.sqlite")
init_db(engine)
session = get_session(engine)
proc = psutil.Process()


def timed(label, **kwargs):
    rss_before = proc.memory_info().rss / 1_048_576
    t0 = time.perf_counter()
    df, report = ingest_pos_bill_wise(config, session, **kwargs)
    secs = time.perf_counter() - t0
    rss_after = proc.memory_info().rss / 1_048_576
    print(f"{label}")
    print(f"    time            : {secs:>8.2f}s")
    print(f"    rows            : {len(df):,}")
    print(f"    report status   : {report.status}")
    print(f"    cash total      : {report.breakdown['mop_class']['cash']['total_amount']:,.0f}")
    print(f"    digital lines   : {report.breakdown['mop_class']['digital']['count'] + report.breakdown['mop_class']['digital_noref']['count']:,}")
    print(f"    RSS delta       : {rss_after - rss_before:+.0f} MB")
    return secs, df, report


pos_file = config.sources["pos_bill_wise"] / "July" / "MOP SALE DATA.xlsb"
print(f"file: {pos_file.name}  ({pos_file.stat().st_size / 1_048_576:.0f} MB)")
print(f"warehouse current? {'yes' if warehouse_manifest('pos_bill_wise', pos_file) else 'no'}\n")

print("=" * 68)
before, df_raw, rep_raw = timed("BEFORE — forced raw read (use_warehouse=False)",
                                file_path=pos_file, use_warehouse=False)
print()
after, df_wh, rep_wh = timed("AFTER  — warehouse read (default)",
                             file_path=pos_file, use_warehouse=True)
print("=" * 68)

print(f"\nspeed-up: {before / after:.0f}x  ({before:.0f}s -> {after:.1f}s, "
      f"{before - after:.0f}s saved)")

# The numbers must be identical, or the fast path is worthless.
same_rows = len(df_raw) == len(df_wh)
same_cash = (rep_raw.breakdown["mop_class"]["cash"]["total_amount"]
             == rep_wh.breakdown["mop_class"]["cash"]["total_amount"])
same_dup = (rep_raw.breakdown["duplicate_candidates"]
            == rep_wh.breakdown["duplicate_candidates"])
print(f"identical rows       : {same_rows}")
print(f"identical cash total : {same_cash}")
print(f"identical duplicates : {same_dup}")

# And a changed file must force a raw re-read rather than serving stale Parquet.
print("\n--- staleness check ---")
manifest = warehouse_manifest("pos_bill_wise", pos_file)
print(f"manifest matches current file      : {manifest is not None}")
tampered = dict(manifest)
tampered["mtime"] = manifest["mtime"] + 1
import json
mpath = ROOT / "warehouse" / "pos_bill_wise" / "_manifest.json"
original = mpath.read_text(encoding="utf-8")
mpath.write_text(json.dumps(tampered, indent=2, default=str), encoding="utf-8")
try:
    stale = warehouse_manifest("pos_bill_wise", pos_file)
    print(f"manifest with a changed mtime      : {'REUSED (BUG)' if stale else 'rejected -> raw re-read'}")
finally:
    mpath.write_text(original, encoding="utf-8")
    print(f"manifest restored                  : {warehouse_manifest('pos_bill_wise', pos_file) is not None}")

session.close()
