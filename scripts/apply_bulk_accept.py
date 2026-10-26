"""Apply the system's own exact and split suggestions (the two Accept-all buttons on Exceptions) from the command line.

    python scripts/apply_bulk_accept.py --by "Aditi Agarwal"

Same functions the page calls (bulk_accept.build / apply): every row is re-validated at the moment of applying, each rule is one
reversible batch, and a manual match closes the bill's exception, the transaction's exception and the open-bill store task together.
"""
import argparse
import logging
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from mop_reco import bulk_accept as ba  # noqa: E402
from mop_reco import exception_pairing as ep  # noqa: E402
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco.db import get_engine, get_session, init_db  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--by", required=True)
args = ap.parse_args()
logging.disable(logging.CRITICAL)
cfg = yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8"))
session = get_session(init_db(get_engine()))
run_id = mm.latest_completed_run_id(session)
minutes, days = ep.window_settings(cfg)


def build():
    pool, bills = ep.unmapped_pool(session), ep.open_population(session, run_id)
    return ba.build(bills, pool, ep.assign(bills, pool, minutes, days, ep.mop_family(cfg)))


bulk = build()
print(f"run #{run_id}: exact {bulk.exact_n} bills, split {bulk.split_n} bills, left {bulk.left_n}")
for kind, rows in (("exact", bulk.exact), ("split", bulk.split)):
    rep = ba.apply(session, run_id, rows, args.by, kind)
    print(f"{kind}: applied {rep.applied} of {rep.submitted}, refused {len(rep.refused)}, bills closed {rep.bills_closed} "
          f"(Rs {rep.value:,.2f}), transactions consumed {rep.transactions_consumed}, tasks closed {rep.tasks_closed}, "
          f"batch #{rep.batch_id}")
    for r in rep.refused[:10]:
        print("   refused:", r)
