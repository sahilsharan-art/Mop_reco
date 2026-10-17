"""Start a reconciliation run over a date range, from the command line (the Upload page does the same).

    python scripts/run_range.py --from 2026-04-01 --to 2026-09-30 \
        --use-stored pickup_location --reason "file on disk lost its LOCATION column" --by "Aditi"

`--use-stored NAME` (repeatable) authorises THIS run to use the stored version of a master whose file on disk is
unusable. `--reason` is required with it and is written to the run's notes and shown on the Masters page together with
the file's modified date. Without it the master pre-flight blocks the run, as it always does.
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import runs  # noqa: E402
from mop_reco.config import load_config  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="lo", type=date.fromisoformat, required=True)
ap.add_argument("--to", dest="hi", type=date.fromisoformat, required=True)
ap.add_argument("--by", default="script")
ap.add_argument("--use-stored", action="append", default=[], metavar="MASTER")
ap.add_argument("--reason", default=None)
args = ap.parse_args()
if args.use_stored and not args.reason:
    sys.exit("--use-stored needs --reason (it is written to the run's notes)")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
engine = init_db(get_engine())
session = get_session(engine)
out = runs.run_reconciliation(load_config(), session, args.lo, args.hi, triggered_by=args.by,
                              use_stored_masters={m: args.reason for m in args.use_stored})
print("RUN FINISHED", out["run_id"], round(out["elapsed_seconds"]), "s", out["r1"])
