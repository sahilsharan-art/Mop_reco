"""Step 1 of moving the masters from Excel into the database (one time).

    python scripts/export_masters_snapshot.py [--prefer excel|stored] [--out snapshots/masters_snapshot.json]

Reads the five lookup-master workbooks and the opening-cash workbook ONCE (and the versions already stored in the
local database), decides per master which to use, writes ONE json file with a fingerprint of every master, and prints
what it decided - including any master whose workbook differs from the stored version or is unusable (it then says
which stored version is used). Nothing is written to any database. The file is then imported with
`import_masters_snapshot.py` (here for testing, and on the application server for production).
"""
import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import masters_db  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--prefer", choices=["excel", "stored"], default="excel")
ap.add_argument("--out", default=str(ROOT / "snapshots" / "masters_snapshot.json"))
args = ap.parse_args()
logging.basicConfig(level=logging.WARNING)

session = get_session(init_db(get_engine()))
snap = masters_db.build_snapshot(load_config(), session, prefer=args.prefer)
out = Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(snap, indent=1, default=str), encoding="utf-8")
print(f"snapshot written: {out}  ({out.stat().st_size / 1024:,.0f} KB)")
for line in snap["report"]:
    print("  -", line)
print("fingerprint:", snap["snapshot_fingerprint"])
