"""Rehearse BULK ACCEPT end to end against a COPY of the database (never db/reco.sqlite).

    set MOP_RECO_DB=D:\\MOP_Reco\\exports\\rehearsal\\reco_copy.sqlite
    python scripts/bulk_accept_rehearsal.py

Builds the exact and split sets from the whole open population, applies the exact rule then the split rule through
the SAME function the Exceptions page calls, and prints for each: bills closed, transactions consumed, store tasks
closed, the digital receivable before and after (Summary column G, read live), the Summary tile figures, and both
two-sided controls. Then it reverses both batches and proves everything returned.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

DB = os.environ.get("MOP_RECO_DB", "")
if not DB or Path(DB).name == "reco.sqlite":
    sys.exit("Set MOP_RECO_DB to a COPY of the database (never db/reco.sqlite).")

from mop_reco import bulk_accept as ba  # noqa: E402
from mop_reco import exception_pairing as ep  # noqa: E402
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco import queries as q  # noqa: E402
from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco.db import ManualMatch, StoreTask, get_engine, get_session, init_db  # noqa: E402

logging.disable(logging.CRITICAL)
WHO = "Bulk accept rehearsal"
CONFIG = yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8"))

session = get_session(init_db(get_engine(Path(DB))))
run_id = mm.latest_completed_run_id(session)
A, B = date(2026, 4, 1), date(2026, 9, 30)
minutes, days = ep.window_settings(CONFIG)


def snapshot(label):
    ov = q.match_overview(session, run_id, A, B, None)
    sheet, _ = src.load_fast(session, run_id, A, B)
    g = round(float(sheet["digital_receivable"].sum()), 2)
    tasks = session.query(StoreTask).filter(StoreTask.task_type == "open_bill",
                                            StoreTask.status.in_(("open", "replied", "under_review"))).count()
    snap = {
        "digital receivable (Summary G)": g, "open bill lines": ov.bills["open"]["count"],
        "manual bills": ov.bills["manual"]["count"], "unmapped txns": ov.txns.unmapped_n,
        "manual txns": ov.txns.manual_n, "exception bills": ov.exception_bills_n,
        "exception txns": ov.exception_txns_n, "live open-bill tasks": tasks,
        "CONTROL bills open+system+manual=total": ov.bills_ok,
        "CONTROL txns system+manual+refunded+unmapped=total": ov.txns_ok,
        "CONTROL open bills = exception bills + folded": ov.bills_agree_with_exceptions,
        "CONTROL unmapped = exception txns + folded": ov.unmapped_agrees_with_exceptions,
    }
    print(f"\n--- {label}")
    for k, v in snap.items():
        print(f"  {k}: {v:,.2f}" if isinstance(v, float) else f"  {k}: {v}")
    return snap


def sets():
    pool, bills = ep.unmapped_pool(session), ep.open_population(session, run_id)
    return ba.build(bills, pool, ep.assign(bills, pool, minutes, days, ep.mop_family(CONFIG)))


print(f"run #{run_id}, range {A} to {B}, database {DB}")
t = time.perf_counter()
bulk = sets()
print(f"built in {time.perf_counter() - t:.1f}s: exact {bulk.exact_n:,} bills Rs {bulk.exact_value:,.2f} | "
      f"split {bulk.split_n:,} bills Rs {bulk.split_value:,.2f} | left {bulk.left_n:,} bills Rs {bulk.left_value:,.2f}")
for k, v in bulk.left.items():
    print(f"   left: {v['bills']:,} bills Rs {v['value']:,.2f} - {k}")
before = snapshot("BEFORE")

reports = []
for kind, rows in (("exact", bulk.exact), ("split", bulk.split)):
    rep = ba.apply(session, run_id, rows, WHO, kind)
    reports.append(rep)
    print(f"\n=== {kind}: submitted {rep.submitted:,} applied {rep.applied:,} refused {len(rep.refused)} "
          f"bills closed {rep.bills_closed:,} (Rs {rep.value:,.2f}) transactions consumed {rep.transactions_consumed:,} "
          f"tasks closed {rep.tasks_closed:,} batch #{rep.batch_id} in {rep.seconds:.0f}s")
    for r in rep.refused[:5]:
        print("   refused:", r)
after = snapshot("AFTER both rules")

left = sets()
print(f"\nStill left for a person after both buttons: {left.left_n:,} bills Rs {left.left_value:,.2f}; "
      f"new exact {left.exact_n}, new split {left.split_n}")
print("sources recorded:", {s: n for s, n in session.query(ManualMatch.source, __import__('sqlalchemy').func.count())
                            .filter(ManualMatch.batch_id.in_([r.batch_id for r in reports]))
                            .group_by(ManualMatch.source).all()})

for rep in reversed(reports):
    t = time.perf_counter()
    n = mm.reverse_batch(session, rep.batch_id, WHO, "rehearsal reversal")
    print(f"\nreversed batch #{rep.batch_id}: {n:,} match(es) in {time.perf_counter() - t:.0f}s")
back = snapshot("AFTER reversing both")
same = {k: (before[k], back[k]) for k in before if before[k] != back[k]}
print("\nDIFFERENCES between BEFORE and after-reversal:", same or "none - everything returned")
print("history kept:", session.query(ManualMatch).filter(ManualMatch.lifecycle == "reversed",
                                                         ManualMatch.source == ba.BULK_SOURCE).count(),
      "reversed rows still in the register")
