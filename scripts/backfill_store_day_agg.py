"""One-time backfill: populate store_day_agg / date_total_agg for a run that
completed before those tables existed (2026-09-20). A run from here on
writes them itself, as its own stage in `runs.run_reconciliation()` — this
script exists only to bring an EXISTING run's data forward without having
to re-run the full ~16-minute reconciliation.

Run from the project root:  python scripts/backfill_store_day_agg.py [run_id]
Defaults to the latest run when no run_id is given.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco.db import R1BillLine, Run, get_session, init_db  # noqa: E402

session = get_session(init_db())

run_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
run = (session.query(Run).get(run_id) if run_id
      else session.query(Run).order_by(Run.started_at.desc()).first())
if run is None:
    print("No run found.")
    sys.exit(1)

dates = [d[0] for d in session.query(R1BillLine.business_date)
        .filter(R1BillLine.run_id == run.id).distinct().all()]
if not dates:
    print(f"run #{run.id} has no bill lines — nothing to backfill.")
    sys.exit(0)

date_from, date_to = date.fromisoformat(min(dates)), date.fromisoformat(max(dates))
print(f"Backfilling run #{run.id}: {date_from} to {date_to}")
report = src.persist_aggregates(session, run.id, date_from, date_to)
print(f"store_day_agg: {report['store_days']} rows, "
     f"date_total_agg: {report['dates']} rows, {report['elapsed']}s")

# `runs.run_reconciliation()` stores this same report — including the
# "static" controls/notes/CMS-type payload `load_static()` reads — on
# `Run.notes` under the "store_day_agg" key, as its own stage. A backfill
# against an EXISTING run has to do the identical write, or Summary.py's
# `load_static()` still falls back to the slow `build()` path for
# everything except the sheet itself, for this run.
try:
    existing_notes = json.loads(run.notes) if run.notes else {}
except (ValueError, TypeError):
    existing_notes = {}
existing_notes["store_day_agg"] = report
run.notes = json.dumps(existing_notes, default=str)
session.commit()
print("Run.notes updated with the static controls/notes/CMS-type payload.")
session.close()
