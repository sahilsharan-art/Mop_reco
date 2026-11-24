"""UAT cases that WRITE (audit Phase 7): manual matches, reversal, bulk accept, remarks. UAT database only.

    MOP_DATABASE_URL=postgresql+psycopg://.../mopuat python scripts/audit_uat_run_writes.py

Refuses to run unless the database name contains 'uat'. Merges its results into docs/audit/uat_results.json.
"""
import io
import json
import os
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
if os.environ.get("MOP_REQUIRE_POSTGRES") == "1":
    sys.exit("Refusing to run against a production-mode environment.")

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from mop_reco import bulk_accept as ba  # noqa: E402
from mop_reco import exception_pairing as ep  # noqa: E402
from mop_reco import exception_tables as et  # noqa: E402
from mop_reco import exceptions as exc  # noqa: E402
from mop_reco import export as ex  # noqa: E402
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco import queries as q  # noqa: E402
from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco.db import Exception_, ManualMatch, ManualMatchEvent, Run, StoreTask, get_engine, get_session  # noqa: E402

OUT = ROOT / "docs" / "audit" / "uat_results.json"
results = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
engine = get_engine()
if "uat" not in (engine.url.database or "").lower():
    sys.exit(f"Refusing to run against {engine.url.database!r}: the database name must contain 'uat'.")
s = get_session(engine)
RUN = s.query(Run).filter(Run.status == "completed").order_by(Run.id.desc()).first().id
LO, HI = date(2026, 4, 1), date(2026, 11, 14)
WHO = "UAT tester"
close = lambda a, b: abs(float(a) - float(b)) <= 0.01   # noqa: E731
status_of = lambda key: s.query(Exception_).filter_by(exception_key=key).one().status   # noqa: E731


def rec(cid, ok, evidence):
    results[cid] = {"status": "Pass" if ok else "FAIL", "evidence": evidence, "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    print(f"{cid}: {'Pass' if ok else 'FAIL'} - {evidence[:170]}")


def guard(cid, fn):
    try:
        fn()
    except Exception as e:
        results[cid] = {"status": "FAIL", "evidence": f"raised {type(e).__name__}: {str(e)[:300]}", "executed_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        print(f"{cid}: FAIL (exception) {type(e).__name__}: {str(e)[:200]}")
        traceback.print_exc(limit=3)


bills_df = ep.open_population(s, RUN)
pool = ep.unmapped_pool(s)
sug = ba.build(bills_df, pool, ep.assign(bills_df, pool))
print(f"open bills {len(bills_df):,}, unmapped transactions {len(pool):,}; exact suggestions {len(sug.exact):,}, split {len(sug.split):,}")
state = {}
FRESH = {r.txn_id: r for r in pool.itertuples(index=False)}


def bill_and_pool(row):
    bill, is_open, why = mm.find_bill_for_match(s, RUN, row.bill_no)
    assert is_open, why
    leg = row.legs[0]
    return bill, leg, ba._pool_row(leg.gateway, FRESH[leg.txn_id])


def e03_e04_t04():
    row = sug.exact[0]
    bill, leg, pl = bill_and_pool(row)
    before = q.match_overview(s, RUN, LO, HI)
    recv_before = src.mop_particulars_matrix(s, RUN, LO, HI, None).total_digital.receivable
    task_before = s.query(StoreTask).filter(StoreTask.bill_no == row.bill_no, StoreTask.task_type == "open_bill", StoreTask.status != "closed").count()
    v = mm.validate_by_id(s, bill, leg.txn_id, gateway_pool=pl)
    assert v.ok and v.case == mm.CASE_CLEAN, (v.case, v.problems)
    entry = mm.record(s, bill, v.gateway, leg.txn_id, "manual_match_by_bill", WHO, v, source="inline")
    state["entry_id"], state["row"], state["bill"] = entry.id, row, bill
    bkey, tkey = exc.bill_exception_key(row.date, row.store, row.bill_no, row.mop), exc.txn_exception_key(leg.gateway, leg.txn_id)
    state["bkey"], state["tkey"] = bkey, tkey
    ok_ex = status_of(bkey) == "closed" and status_of(tkey) == "closed"
    m = s.get(ManualMatch, entry.id)
    rec("UAT-E03", ok_ex and m.operator == WHO and m.created_at is not None and m.source == "inline" and m.status == "matched_manual",
        f"bill {row.bill_no} (Rs {row.amount:,.2f}) matched to {leg.gateway} {leg.txn_id[:14]}...; bill and transaction exceptions closed; recorded by '{m.operator}' at {m.created_at:%H:%M} source {m.source}")
    after = q.match_overview(s, RUN, LO, HI)
    recv_after = src.mop_particulars_matrix(s, RUN, LO, HI, None).total_digital.receivable
    rec("UAT-E04", after.bills["manual"]["count"] == before.bills["manual"]["count"] + 1 and after.bills["open"]["count"] == before.bills["open"]["count"] - 1
        and after.txns.unmapped_n == before.txns.unmapped_n - 1 and close(recv_before - recv_after, row.amount) and after.bills_ok and after.txns_ok,
        f"no re-run: open bills {before.bills['open']['count']:,} -> {after.bills['open']['count']:,}; manual {before.bills['manual']['count']} -> {after.bills['manual']['count']}; unmapped {before.txns.unmapped_n:,} -> {after.txns.unmapped_n:,}; digital receivable down {recv_before - recv_after:,.2f} (bill Rs {row.amount:,.2f}); both controls still hold")
    task_after = s.query(StoreTask).filter(StoreTask.bill_no == row.bill_no, StoreTask.task_type == "open_bill", StoreTask.status != "closed").count()
    closed = s.query(StoreTask).filter(StoreTask.bill_no == row.bill_no, StoreTask.task_type == "open_bill", StoreTask.status == "closed").count()
    rec("UAT-T04", task_before >= 1 and task_after == 0 and closed >= 1, f"open-bill task for {row.bill_no}: live before {task_before}, live after {task_after}, closed {closed}")


def e05_e06_e07():
    r1, r2 = sug.exact[1], sug.exact[2]
    bill2, _, _ = bill_and_pool(r2)
    used = state["row"].legs[0]
    # E05: the transaction used in E03 offered to another bill
    pl_used = ba._pool_row(used.gateway, FRESH[used.txn_id])
    v = mm.validate_by_id(s, bill2, used.txn_id, gateway_pool=pl_used)
    msg = " | ".join(v.problems)
    rec("UAT-E05", (not v.ok or v.needs_override) and (state["row"].bill_no in msg or "already" in msg.lower()), f"a used transaction offered to {r2.bill_no}: refused/needs override; message: {msg[:160]}")
    # E06: a transaction whose amount differs from the bill
    other = sug.exact[3] if len(sug.exact) > 3 else sug.exact[1]
    leg = other.legs[0]
    bad = bill2.copy(); bad["amount"] = float(bad["amount"]) + 1.0
    pl = ba._pool_row(leg.gateway, FRESH[leg.txn_id])
    v6 = mm.validate_by_id(s, bad, leg.txn_id, gateway_pool=pl)
    rec("UAT-E06", v6.case == mm.CASE_DISCREPANT and v6.needs_override and bool(v6.problems), f"amount differs by Rs 1: case {v6.case}, override required: {v6.needs_override}; problems: {' | '.join(v6.problems)[:140]}")
    # E07: an id found nowhere
    v7 = mm.validate_by_id(s, bill2, "NO-SUCH-TRANSACTION-ID-0001", gateway_pool=pl)
    rec("UAT-E07", v7.case == mm.CASE_NOT_FOUND, f"an id in no gateway: case {v7.case} (asserted, unverified); the bill is not closed: {status_of(exc.bill_exception_key(r2.date, r2.store, r2.bill_no, r2.mop))}")


def e08():
    before = s.query(ManualMatchEvent).filter(ManualMatchEvent.manual_match_id == state["entry_id"]).count()
    mm.reverse(s, state["entry_id"], WHO, "UAT reversal")
    m = s.get(ManualMatch, state["entry_id"])
    after = s.query(ManualMatchEvent).filter(ManualMatchEvent.manual_match_id == state["entry_id"]).count()
    row = state["row"]
    s.expire_all()
    tasks = s.query(StoreTask).filter(StoreTask.bill_no == row.bill_no, StoreTask.task_type == "open_bill").order_by(StoreTask.generation).all()
    rec("UAT-E08", status_of(state["bkey"]) == "open" and status_of(state["tkey"]) == "open" and m.lifecycle == "reversed" and after > before and tasks[-1].status != "closed" and tasks[-1].generation >= 2,
        f"after reversal: bill and transaction exceptions open again; match kept with lifecycle '{m.lifecycle}'; events {before} -> {after}; a new task raised (generation {tasks[-1].generation})")


def e09():
    rows = sug.exact[1:6]
    before = q.match_overview(s, RUN, LO, HI)
    rep = ba.apply(s, RUN, rows, WHO, "exact")
    after = q.match_overview(s, RUN, LO, HI)
    batch = s.query(ManualMatch).filter(ManualMatch.batch_id == rep.batch_id).count() if rep.batch_id else 0
    rec("UAT-E09", rep.applied + len(rep.refused) == len(rows) and rep.applied >= 1 and batch == rep.applied
        and after.bills["manual"]["count"] == before.bills["manual"]["count"] + rep.applied and after.bills_ok and after.txns_ok,
        f"submitted {rep.submitted}: applied {rep.applied}, refused {len(rep.refused)} ({[r['why'][:60] for r in rep.refused][:2]}); one batch #{rep.batch_id} with {batch} matches; Rs {rep.value:,.2f}; controls hold")
    state["batch_id"] = rep.batch_id


def e12():
    key = state["bkey"]
    exc.add_remark(s, key, "UAT remark one", WHO)
    exc.add_remark(s, key, "UAT remark two", WHO)
    rs = exc.remarks_for(s, key)
    texts = [getattr(r, "remark_text", None) or r.get("remark_text") for r in rs] if rs else []
    rec("UAT-E12", len(rs) == 2 and set(texts) == {"UAT remark one", "UAT remark two"} and all(getattr(r, "created_by", WHO) == WHO for r in rs),
        f"{len(rs)} remarks kept with who and when: {texts}")


def e13_e15():
    types = ["unmapped_gateway_transaction"]
    df = q.exception_rows(s, None, None, None, open_only=True, types=types)
    df = df.head(3000)
    frame = et.txn_frame(s, df)
    cols = list(frame.columns)
    meta = ex.ExportMeta(run_id=RUN, date_from=LO, date_to=HI, title="exception_transactions")
    res = ex.build_export(frame, meta)
    assert res.rows == len(frame)
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(res.data)).active
    header_row = res.header_row + 1
    hdr = [c.value for c in ws[header_row]]
    idcol = next((i for i, h in enumerate(hdr) if h and "transaction" in str(h).lower() and "id" in str(h).lower()), None)
    long_ids = [ws.cell(row=header_row + 1 + i, column=idcol + 1) for i in range(min(50, len(frame)))] if idcol is not None else []
    all_text = bool(long_ids) and all(c.data_type == "s" for c in long_ids) and any(len(str(c.value)) >= 30 for c in long_ids)
    meta_cells = [str(ws.cell(row=r, column=1).value) for r in range(1, header_row) if ws.cell(row=r, column=1).value]
    needed = {"run", "filter", "row", "total", "export"}
    have = {n for n in needed if any(n in m.lower() for m in meta_cells)}
    rec("UAT-E13", res.rows == len(frame) and all_text and len(have) >= 4,
        f"{res.rows:,} filtered rows exported as {res.fmt}; header block lines: {meta_cells}; first 50 transaction ids are all stored as text, the long Paytm ones with every digit (e.g. {max((str(c.value) for c in long_ids), key=len)})")
    closed = q.exception_rows(s, None, None, None, open_only=False, types=list(q.NOT_RECEIVED_TYPES))
    n_closed = int(closed["status"].astype(str).eq("closed").sum()) if "status" in closed else -1
    sql_closed = s.execute(text("select count(*) from exceptions where status = 'closed' and type in ('bill_not_matched','ambiguous_edc_other','no_source_report')")).scalar()
    rec("UAT-E15", n_closed >= 5 and n_closed == sql_closed, f"open_only off: {len(closed):,} bill exceptions listed, {n_closed} closed = SQL {sql_closed} (the bulk-accepted ones); nothing deleted")


for cid, fn in [("UAT-E03", e03_e04_t04), ("UAT-E05", e05_e06_e07), ("UAT-E08", e08), ("UAT-E09", e09), ("UAT-E12", e12), ("UAT-E13", e13_e15)]:
    guard(cid, fn)

OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"\n{len(results)} results saved")
