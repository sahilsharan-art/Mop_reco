"""Part 3 of the rehearsal on its own: the refusals, on real data (run against a COPY - see round_trip_rehearsal.py)."""

from __future__ import annotations



import os
import sys
from datetime import date
from pathlib import Path

import openpyxl
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

DB = os.environ.get("MOP_RECO_DB", "")
if not DB or Path(DB).name == "reco.sqlite":
    sys.exit("Set MOP_RECO_DB to a COPY of the database (never db/reco.sqlite).")

from mop_reco import exception_pairing as ep  # noqa: E402
from mop_reco import exception_tables as et  # noqa: E402
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco import queries as q  # noqa: E402
from mop_reco import refunds as rf  # noqa: E402
from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco import table_upload as tu  # noqa: E402
from mop_reco.db import (Exception_, RefundEvent, Run, StoreTask, TransactionRefund, get_engine,  # noqa: E402
                         get_session, init_db)
from mop_reco.export import ExportMeta, build_export  # noqa: E402

RUN, A, B = 11, date(2026, 4, 1), date(2026, 8, 31)
WHO = "Round-trip rehearsal"
OUT = ROOT / "exports" / "round_trip"
OUT.mkdir(parents=True, exist_ok=True)
CONFIG = yaml.safe_load((ROOT / "app" / "config.yaml").read_text(encoding="utf-8"))
MINUTES, DAYS = ep.window_settings(CONFIG)
BILL_TYPES = q.NOT_RECEIVED_TYPES
TXN_TYPES = q.RECEIVED_NOT_BILLED_TYPES + ("original_txn_not_found", "store_ambiguous")

engine = init_db(get_engine())
session = get_session(engine)
print(f"database: {DB}")


def say(text=""):
    print(text, flush=True)


def figures(label, store=None, bill_no=None):
    ov = q.match_overview(session, RUN, A, B)
    mat = src.mop_particulars_matrix(session, RUN, A, B)
    sheet, _ = src.load_fast(session, RUN, A, B, store) if store else (None, None)
    task = None
    if bill_no:
        t = (session.query(StoreTask).filter(StoreTask.task_type == "open_bill", StoreTask.bill_no == bill_no)
             .order_by(StoreTask.generation.desc()).first())
        task = None if t is None else (t.generation, t.status, t.closed_by, t.close_reason)
    f = {
        "label": label,
        "exception bills (open)": (ov.exception_bills_n, round(ov.exception_bills_v, 2)),
        "exception transactions (open)": (ov.exception_txns_n, round(ov.exception_txns_v, 2)),
        "bills open/auto/manual/total": tuple(ov.bills[k]["count"] for k in ("open", "auto", "manual", "total")),
        "txns auto/manual/refunded/unmapped/total": (ov.txns.auto_n, ov.txns.manual_n, ov.txns.refunded_n,
                                                      ov.txns.unmapped_n, ov.txns.total_n),
        "controls": ("bills OK" if ov.bills_ok else "bills MISMATCH",
                     "txns OK" if ov.txns_ok else "txns MISMATCH",
                     "bills=exceptions OK" if ov.bills_agree_with_exceptions else "bills=exceptions MISMATCH",
                     "unmapped=exceptions OK" if ov.unmapped_agrees_with_exceptions else "unmapped=exceptions MISMATCH"),
        "Summary total-sale receivable (Rs)": round(mat.total_sale.receivable, 2),
        "Summary digital receivable, that store (Rs)": None if sheet is None else round(float(sheet["digital_receivable"].sum()), 2),
        "store task": task,
    }
    say(f"--- {label}")
    for k, v in f.items():
        if k != "label":
            say(f"    {k}: {v}")
    return f


pool = ep.unmapped_pool(session)
pop = ep.open_population(session, RUN)
assignment = ep.assign(pop, pool, MINUTES, DAYS, ep.mop_family(CONFIG))
live_tasks = {b for (b,) in session.query(StoreTask.bill_no).filter(StoreTask.task_type == "open_bill",
                                                                     StoreTask.status.in_(("open", "replied", "under_review"))).all()}
pick = next(r for r in pop.sort_values(["amount", "bill_no"], ascending=[False, True]).itertuples()
            if assignment.kind.get(r.bill_key) == "exact" and 800 <= r.amount <= 3000 and r.bill_no in live_tasks)
cand = assignment.candidates[pick.bill_key][0]
cols = et.bill_columns(pairing=True, screen=False)
downloaded = OUT / "Exception_bills_downloaded.xlsx"
say()
say("=" * 100)
say("PART 3 - THE REFUSALS, ON REAL DATA")
say("=" * 100)
dup = next(r for r in pool.sort_values("txn_id").itertuples() if rf._owner_of(session, r.gateway, str(r.txn_id)) is not None)
owner = rf._owner_of(session, dup.gateway, str(dup.txn_id))
say(f"transaction {dup.txn_id} ({dup.gateway}, store {dup.store}, Rs {dup.amount:,.2f}) is listed as an exception, but its "
    f"reference is already held by bill {owner.bill_no}")
other = next(r for r in pop.itertuples() if r.store == dup.store and r.bill_no != owner.bill_no)
say(f"(a) an upload matching open bill {other.bill_no} to it:")
base = tu.read_table_file(downloaded.read_bytes(), downloaded.name, cols).assign(**{"Txn 1": "", "Txn 2": ""})
one = base.copy()
one.loc[one["bill_no"] == other.bill_no, "Txn 1"] = str(dup.txn_id)
bf = tu.preview_bills(session, RUN, one)
for r in bf.rows:
    if r.verdict != "ignore":
        say(f"    row {r.row_no}: {r.kind} {r.ref} -> {r.counterpart}: {r.verdict.upper()} {r.why}")
say("(b) all or nothing - one good row and one bad row in the same file:")
mixed = base.copy()
mixed.loc[mixed["bill_no"] == pick.bill_no, "Txn 1"] = str(cand.txn_id)
mixed.loc[mixed["bill_no"] == other.bill_no, "Txn 1"] = str(dup.txn_id)
mprev = tu.preview_bills(session, RUN, mixed)
say(f"    preview: {len(mprev.accepted)} accepted, {len(mprev.rejected)} rejected -> can apply: {mprev.can_apply}")
n_before = session.query(mm.ManualMatch).filter_by(lifecycle="active").count()
try:
    tu.apply_preview(session, mprev, WHO)
except ValueError as exc:
    say(f"    apply refused: {exc}")
say(f"    active manual matches before {n_before}, after {session.query(mm.ManualMatch).filter_by(lifecycle='active').count()} - nothing written")
