"""Step 2 of moving the masters from Excel into the database (one time).

    python scripts/import_masters_snapshot.py --snapshot snapshots/masters_snapshot.json --by "Your Name"

Loads the snapshot into the database the application is pointed at (MOP_DATABASE_URL, else the local SQLite file) as
the first versions of each master, reads every master back to verify it, and only then switches the setting
masters_source to 'database'. From then on no master workbook is ever read. Safe to run twice (a master already
holding exactly these rows is left alone). --check only reads the snapshot and the target and changes nothing.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import masters_db  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--snapshot", required=True)
ap.add_argument("--by", required=True, help="who is running the import (recorded on every version)")
ap.add_argument("--check", action="store_true")
args = ap.parse_args()

snap = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
engine = init_db(get_engine())
session = get_session(engine)
print("target database:", engine.url.render_as_string(hide_password=True))
print("snapshot:", snap["created_at"], "| fingerprint", snap["snapshot_fingerprint"])
for name, e in snap["masters"].items():
    cur = masters_db.current_rows(session, name)
    same = masters_db._fingerprint([masters_db._clean_row(name, r) for r in cur]) == e["fingerprint"]
    print(f"  {name:16s} snapshot {len(e['versions']):>2} version(s), newest {e['row_count']:>5} rows | "
          f"target now {len(cur):>5} rows | {'already identical' if same else 'would change'}")
if args.check:
    print("--check: nothing was changed")
    sys.exit(0)
for line in masters_db.import_snapshot(session, snap, args.by):
    print(" ", line)
print("done. masters_source =", masters_db.get_setting(session, masters_db.SETTING_MASTERS_SOURCE))
