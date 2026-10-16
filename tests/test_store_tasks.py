"""Store Task: generation from data, auto-closure, replies, head-office actions (2026-10-14).

Each test builds a tiny world in a scratch database - a few store-days of cash figures and a few
bill lines - and checks the rule in CLAUDE.md-terms: a task is raised from a fact, closed by the
fact clearing (never by a reply), never reopened after a person closed it, and every change leaves
a history row.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_tasks as stt
from mop_reco.db import (ManualMatch, R1BillLine, Run, StoreDayAgg, StoreTask, StoreTaskEvent,
                         get_engine, get_session, init_db)

TODAY = date(2026, 10, 14)          # a Wednesday
CFG = {"float_tracker": {"float_threshold": 10000}}


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), business_dates="[]", status="completed"))
    s.commit()
    return s


def sday(session, store, day, sale=0.0, opening=0.0, deposit=0.0, received=0.0, run_id=1):
    """One store-day row. float = opening - deposit (column N)."""
    session.add(StoreDayAgg(
        run_id=run_id, business_date=day, store=store, total_sale=sale, digital_sale=0, cash_sale=sale,
        opening_balance=opening, cash_deposit=deposit, cash_received=received,
        cash_receivable=deposit - received, cash_expense_income=0,
        closing_balance=opening + sale - deposit, float_amount=opening - deposit, defect_b=False))
    session.commit()


def bill(session, bill_no, matched=False, store="ABD", day="2026-08-03", amount=100.0, mop="PAYTM EDC", run_id=1):
    session.add(R1BillLine(run_id=run_id, business_date=day, store=store, bill_no=bill_no, line_seq=1, mop=mop,
                           mop_class="digital", amount=amount, matched=matched,
                           status="Tier 1" if matched else "Not found in gateway",
                           bill_time="12:30:00", created_by="CASH2"))
    session.commit()


def tasks(session, ttype=None):
    q = session.query(StoreTask)
    if ttype:
        q = q.filter(StoreTask.task_type == ttype)
    return q.order_by(StoreTask.id).all()


# --- calendar and ids -------------------------------------------------------------------

def test_working_days_skip_weekends():
    assert stt.add_working_days(date(2026, 10, 14), 3) == date(2026, 10, 19)      # Wed -> Mon
    assert stt.add_working_days(date(2026, 10, 16), 1) == date(2026, 10, 19)      # Fri -> Mon


def test_banking_day_skips_sunday_and_second_fourth_saturday():
    assert stt.next_banking_day(date(2026, 8, 7)) == date(2026, 8, 10)     # Fri; Sat 8 Aug is the 2nd Sat
    assert stt.next_banking_day(date(2026, 8, 1)) == date(2026, 8, 3)      # Sat 1 Aug (1st Sat) -> Sun skipped -> Mon
    assert stt.next_banking_day(date(2026, 8, 4)) == date(2026, 8, 5)


@pytest.mark.parametrize("raw, expected", [("ST-000123", 123), ("st-5", 5), (7, 7), ("7.0", 7), ("x", None), (None, None)])
def test_parse_id(raw, expected):
    assert stt.parse_id(raw) == expected


# --- open bills ------------------------------------------------------------------------

def test_open_bill_task_per_unmatched_bill_with_its_facts(session):
    bill(session, "B1")
    bill(session, "B2", matched=True)
    rep = stt.sync_tasks(session, 1, TODAY, CFG)
    [t] = tasks(session, stt.OPEN_BILL)
    assert (t.bill_no, t.store, float(t.amount), t.status) == ("B1", "ABD", 100.0, stt.OPEN)
    assert t.task_key == "open_bill:2026-08-03|ABD|B1|PAYTM EDC"
    assert '"bill_time": "12:30:00"' in t.detail and '"created_by": "CASH2"' in t.detail
    assert rep.created[stt.OPEN_BILL] == 1


def test_two_unmatched_lines_of_one_bill_and_mop_are_one_task(session):
    bill(session, "B1", amount=40.0)
    bill(session, "B1", amount=60.0)
    stt.sync_tasks(session, 1, TODAY, CFG)
    [t] = tasks(session, stt.OPEN_BILL)
    assert float(t.amount) == 100.0


def test_active_manual_match_means_no_task(session):
    bill(session, "B1")
    session.add(ManualMatch(bill_key="k", business_date="2026-08-03", store="ABD", bill_no="B1", mop="PAYTM EDC",
                            amount=100, gateway="paytm", transaction_ref="T1", status="matched_manual",
                            validation_case="a", reason_code="manual", operator="x", created_at=datetime(2026, 9, 2),
                            lifecycle="active"))
    session.commit()
    stt.sync_tasks(session, 1, TODAY, CFG)
    assert tasks(session) == []


def test_sync_is_idempotent(session):
    bill(session, "B1")
    stt.sync_tasks(session, 1, TODAY, CFG)
    rep = stt.sync_tasks(session, 1, TODAY, CFG)
    assert len(tasks(session)) == 1 and rep.created == {} and rep.auto_closed == {}
    assert rep.still_open[stt.OPEN_BILL] == (1, 100.0)


def test_bill_matched_later_closes_the_task_by_the_data(session):
    bill(session, "B1")
    stt.sync_tasks(session, 1, TODAY, CFG)
    session.query(R1BillLine).update({"matched": True})
    session.commit()
    rep = stt.sync_tasks(session, 1, TODAY + timedelta(days=1), CFG)
    [t] = tasks(session)
    assert (t.status, t.closed_by, t.close_reason) == (stt.CLOSED, stt.RESOLVED_BY_DATA, "bill matched to a transaction")
    assert t.closed_on == "2026-10-15" and rep.auto_closed[stt.OPEN_BILL] == 1


def test_manual_match_closes_the_task_without_a_rerun(session):
    bill(session, "B1")
    stt.sync_tasks(session, 1, TODAY, CFG)
    session.add(ManualMatch(bill_key="k", business_date="2026-08-03", store="ABD", bill_no="B1", mop="PAYTM EDC",
                            amount=100, gateway="paytm", transaction_ref="T1", status="matched_manual",
                            validation_case="a", reason_code="manual", operator="x", created_at=datetime(2026, 9, 2), lifecycle="active"))
    session.commit()
    stt.sync_tasks(session, 1, TODAY, CFG)
    [t] = tasks(session)
    assert t.status == stt.CLOSED and t.close_reason == "bill matched manually" and t.closed_by == stt.RESOLVED_BY_DATA


def test_a_store_reply_does_not_close_a_task(session):
    bill(session, "B1")
    stt.sync_tasks(session, 1, TODAY, CFG)
    prev = stt.preview_replies(session, pd.DataFrame([{"task_id": "ST-000001", "store": "ABD",
                                                       "reply": "customer paid by UPI", "replied_by": "Ravi"}]))
    assert stt.apply_replies(session, prev, "HO") == 1
    stt.sync_tasks(session, 1, TODAY, CFG)
    [t] = tasks(session)
    assert t.status == stt.REPLIED and t.store_reply == "customer paid by UPI" and t.closed_on is None


def test_manually_closed_task_is_never_reopened_a_new_one_is_raised(session):
    bill(session, "B1")
    stt.sync_tasks(session, 1, TODAY, CFG)
    stt.close_task(session, 1, "store confirmed duplicate", "Aditi", TODAY)
    assert tasks(session)[0].status == stt.CLOSED and tasks(session)[0].closed_by == "Aditi"
    rep = stt.sync_tasks(session, 1, TODAY, CFG)            # bill still open
    old, new = tasks(session)
    assert old.status == stt.CLOSED and old.closed_by == "Aditi"            # untouched
    assert (new.generation, new.status, new.task_key) == (2, stt.OPEN, old.task_key)
    assert rep.created[stt.OPEN_BILL] == 1


def test_narrower_run_cannot_close_tasks_outside_its_dates(session):
    bill(session, "B1", day="2026-08-03")
    sday(session, "ABD", "2026-08-03")
    stt.sync_tasks(session, 1, TODAY, CFG)
    # run 2 covers only September and knows nothing of August
    session.add(Run(id=2, started_at=datetime(2026, 10, 1), business_dates="[]", status="completed"))
    sday(session, "ABD", "2026-09-01", run_id=2)
    stt.sync_tasks(session, 2, TODAY, CFG)
    assert tasks(session, stt.OPEN_BILL)[0].status == stt.OPEN


def test_amount_revision_is_recorded_not_silent(session):
    bill(session, "B1", amount=100.0)
    stt.sync_tasks(session, 1, TODAY, CFG)
    session.query(R1BillLine).update({"amount": 150.0})
    session.commit()
    rep = stt.sync_tasks(session, 1, TODAY, CFG)
    [t] = tasks(session)
    assert float(t.amount) == 150.0 and rep.revised == 1
    assert any(e.event == "amount_revised" for e in session.query(StoreTaskEvent).all())


# --- high float -------------------------------------------------------------------------

def test_high_float_active_only_while_the_streak_is_unbroken(session):
    # ABD: high on Mon 3rd, Tue 4th, then low on Wed 5th -> the 3rd and 4th have cleared.
    # AGT: high on Mon 3rd, Tue 4th, still high on Wed 5th -> all three are live.
    for day, f in (("2026-08-03", 30000), ("2026-08-04", 30000), ("2026-08-05", 5000)):
        sday(session, "ABD", day, opening=f)
    for day in ("2026-08-03", "2026-08-04", "2026-08-05"):
        sday(session, "AGT", day, opening=30000)
    gen = stt.generate(session, 1, CFG)[stt.HIGH_FLOAT]
    assert sorted((c.store, c.business_date) for c in gen.active) == [
        ("AGT", "2026-08-03"), ("AGT", "2026-08-04"), ("AGT", "2026-08-05")]
    assert gen.cleared_n == 2
    assert {c.business_date: c.detail["days_held_high"] for c in gen.active if c.store == "AGT"} == {
        "2026-08-03": 1, "2026-08-04": 2, "2026-08-05": 3}


def test_high_float_skips_a_day_with_a_weekend_auto_remark(session):
    sday(session, "ABD", "2026-08-08", opening=30000)       # 2nd Saturday of August
    sday(session, "ABD", "2026-08-09", opening=30000)       # Sunday
    sday(session, "ABD", "2026-08-10", opening=30000)       # Monday
    gen = stt.generate(session, 1, CFG)[stt.HIGH_FLOAT]
    assert [c.business_date for c in gen.active] == ["2026-08-10"]


def test_high_float_closes_when_float_drops(session):
    sday(session, "ABD", "2026-08-03", opening=30000)
    stt.sync_tasks(session, 1, TODAY, CFG)
    assert tasks(session)[0].status == stt.OPEN
    sday(session, "ABD", "2026-08-04", opening=2000)         # float back under the threshold
    stt.sync_tasks(session, 1, TODAY, CFG)
    [t] = tasks(session)
    assert t.status == stt.CLOSED and t.close_reason == "float is back at or below the threshold"


# --- CMS not done -----------------------------------------------------------------------

def test_cms_no_deposit_by_the_due_banking_day_raises_a_task(session):
    sday(session, "ABD", "2026-08-03", sale=50000)          # Mon sale, due Tue
    sday(session, "ABD", "2026-08-04")                       # no deposit Tuesday
    sday(session, "ABD", "2026-08-05")
    gen = stt.generate(session, 1, CFG)[stt.CMS_NOT_DONE]
    [c] = gen.active
    assert (c.store, c.business_date, c.amount) == ("ABD", "2026-08-03", 50000.0)
    assert c.detail["reasons"][0]["kind"] == "no_deposit" and c.detail["days_outstanding"] >= 1


def test_cms_a_deposit_inside_the_window_means_no_task(session):
    sday(session, "ABD", "2026-08-03", sale=50000)
    sday(session, "ABD", "2026-08-04", deposit=50000, received=50000)
    sday(session, "ABD", "2026-08-05")
    assert stt.generate(session, 1, CFG)[stt.CMS_NOT_DONE].active == []


def test_cms_not_done_clears_when_a_credited_deposit_appears_later(session):
    sday(session, "ABD", "2026-08-03", sale=50000)
    sday(session, "ABD", "2026-08-04")
    sday(session, "ABD", "2026-08-05")
    stt.sync_tasks(session, 1, TODAY, CFG)
    assert tasks(session, stt.CMS_NOT_DONE)[0].status == stt.OPEN
    sday(session, "ABD", "2026-08-06", deposit=50000, received=50000)       # catch-up, credited
    stt.sync_tasks(session, 1, TODAY, CFG)
    [t] = tasks(session, stt.CMS_NOT_DONE)
    assert t.status == stt.CLOSED and t.closed_by == stt.RESOLVED_BY_DATA


def test_cms_deposit_not_credited_past_its_due_date(session):
    sday(session, "ABD", "2026-08-03", sale=0, deposit=40000, received=0)
    sday(session, "ABD", "2026-08-04")
    sday(session, "ABD", "2026-08-05")
    [c] = stt.generate(session, 1, CFG)[stt.CMS_NOT_DONE].active
    assert (c.business_date, c.amount) == ("2026-08-03", 40000.0)
    assert c.detail["reasons"][0]["kind"] == "not_credited"


def test_cms_deposit_still_in_transit_is_not_a_task(session):
    sday(session, "ABD", "2026-08-04")
    sday(session, "ABD", "2026-08-05", deposit=40000, received=0)            # last loaded day, due 6th
    assert stt.generate(session, 1, CFG)[stt.CMS_NOT_DONE].active == []


def test_cms_and_high_float_on_the_same_store_day_do_not_collide(session):
    sday(session, "ABD", "2026-08-03", sale=50000, opening=40000)
    sday(session, "ABD", "2026-08-04", opening=40000)
    sday(session, "ABD", "2026-08-05", opening=40000)
    stt.sync_tasks(session, 1, TODAY, CFG)
    keys = {t.task_key for t in tasks(session)}
    assert {"cms_not_done:ABD|2026-08-03", "high_float:ABD|2026-08-03"} <= keys


# --- SLA, overdue, tiles ----------------------------------------------------------------

def test_due_by_is_raised_plus_sla_working_days_and_overdue_is_flagged(session):
    bill(session, "B1")
    stt.sync_tasks(session, 1, TODAY, {"store_tasks": {"sla_working_days": {"open_bill": 3}}})
    [t] = tasks(session)
    assert (t.raised_on, t.due_by) == ("2026-10-14", "2026-10-19")
    on_time = stt.tasks_page(session, TODAY, "Newest raised", 10, 0)
    late = stt.tasks_page(session, date(2026, 10, 20), "Newest raised", 10, 0)
    assert not on_time["_overdue"].iloc[0] and late["_overdue"].iloc[0]
    assert stt.tiles(session, date(2026, 10, 20))["overdue"] == (1, 100.0)
    assert stt.tiles(session, TODAY)["overdue"] == (0, 0.0)


def test_tiles_replied_and_closed(session):
    for n in ("B1", "B2", "B3"):
        bill(session, n)
    stt.sync_tasks(session, 1, TODAY, CFG)
    prev = stt.preview_replies(session, pd.DataFrame([{"task_id": 1, "store": "ABD", "reply": "r", "replied_by": "x"}]))
    stt.apply_replies(session, prev, "HO")
    stt.close_task(session, 2, "done", "Aditi", TODAY)
    t = stt.tiles(session, TODAY)
    assert t["open"][0] == 1 and t["pending"][0] == 1 and t["wip"][0] == 0 and t["closed"][0] == 1


def test_open_pending_wip_closed_partition_the_tasks(session):
    for n in ("B1", "B2", "B3", "B4"):
        bill(session, n)
    stt.sync_tasks(session, 1, TODAY, CFG)
    prev = stt.preview_replies(session, pd.DataFrame([{"task_id": 1, "store": "ABD", "reply": "r", "replied_by": "x"}]))
    stt.apply_replies(session, prev, "HO")
    stt.mark_under_review(session, 2, "Aditi")
    stt.close_task(session, 3, "done", "Aditi", TODAY)
    t = stt.tiles(session, TODAY)
    assert (t["open"][0], t["pending"][0], t["wip"][0], t["closed"][0]) == (1, 1, 1, 1)
    assert sum(t[k][1] for k in ("open", "pending", "wip", "closed")) == 400.0


def test_every_read_can_be_scoped_to_one_store(session):
    bill(session, "B1", store="ABD")
    bill(session, "B2", store="AGT")
    stt.sync_tasks(session, 1, TODAY, CFG)
    page = stt.tasks_page(session, TODAY, "Store, then date", 10, 0, store="AGT")
    assert page["Store"].tolist() == ["AGT"]
    assert stt.tiles(session, TODAY, store="AGT")["open"][0] == 1


# --- replies ----------------------------------------------------------------------------

def _two_tasks(session):
    bill(session, "B1", store="ABD")
    bill(session, "B2", store="AGT")
    stt.sync_tasks(session, 1, TODAY, CFG)


def test_reply_upload_rejects_wrong_store_unknown_task_closed_task_and_duplicates(session):
    _two_tasks(session)
    stt.close_task(session, 2, "done", "Aditi", TODAY)
    frame = pd.DataFrame([
        {"task_id": "ST-000001", "store": "ABD", "reply": "ok", "replied_by": "Ravi"},      # accepted
        {"task_id": "ST-000001", "store": "ABD", "reply": "again", "replied_by": "Ravi"},   # duplicate in file
        {"task_id": "ST-000002", "store": "AGT", "reply": "ok", "replied_by": "Ravi"},      # closed
        {"task_id": "ST-000009", "store": "ABD", "reply": "ok", "replied_by": "Ravi"},      # no such task
        {"task_id": "ST-000001", "store": "XYZ", "reply": "ok", "replied_by": "Ravi"},      # wrong store
        {"task_id": "ST-000001", "store": "ABD", "reply": "", "replied_by": ""},            # empty
    ])
    prev = stt.preview_replies(session, frame)
    assert [r.accepted for r in prev.rows] == [True, False, False, False, False, False]
    why = {r.row: " ".join(r.reasons) for r in prev.rows}
    assert "more than once" in why[3] and "closed" in why[4] and "no task" in why[5]
    assert "does not match" in why[6] and "reply is empty" in why[7]


def test_preview_writes_nothing_and_a_missing_column_is_a_clear_error(session):
    _two_tasks(session)
    stt.preview_replies(session, pd.DataFrame([{"task_id": 1, "store": "ABD", "reply": "r", "replied_by": "x"}]))
    assert tasks(session)[0].store_reply is None
    with pytest.raises(ValueError, match="missing column"):
        stt.preview_replies(session, pd.DataFrame([{"task_id": 1, "store": "ABD"}]))


def test_a_second_reply_keeps_the_first_in_the_history(session):
    _two_tasks(session)
    for text in ("first answer", "second answer"):
        prev = stt.preview_replies(session, pd.DataFrame([{"task_id": 1, "store": "ABD", "reply": text, "replied_by": "R"}]))
        stt.apply_replies(session, prev, "HO")
    assert tasks(session)[0].store_reply == "second answer"
    replies = stt.history(session, 1)
    assert replies[replies["Event"] == "reply"]["Text"].tolist() == ["first answer", "second answer"]


# --- head office ------------------------------------------------------------------------

def test_remark_under_review_and_close_leave_history(session):
    _two_tasks(session)
    stt.add_remark(session, 1, "chasing the store", "Aditi")
    stt.mark_under_review(session, 1, "Aditi")
    stt.close_task(session, 1, "paid by cash, confirmed", "Aditi", TODAY)
    h = stt.history(session, 1)
    assert h["Event"].tolist() == ["raised", "remark", "under_review", "closed"]
    t = tasks(session)[0]
    assert (t.ho_remark, t.status, t.closed_by) == ("chasing the store", stt.CLOSED, "Aditi")


def test_close_needs_a_reason_and_a_closed_task_cannot_change(session):
    _two_tasks(session)
    with pytest.raises(ValueError):
        stt.close_task(session, 1, "  ", "Aditi", TODAY)
    stt.close_task(session, 1, "ok", "Aditi", TODAY)
    with pytest.raises(ValueError):
        stt.mark_under_review(session, 1, "Aditi")
    with pytest.raises(ValueError):
        stt.close_task(session, 1, "again", "Aditi", TODAY)


# --- exports ----------------------------------------------------------------------------

def test_store_sheet_is_the_stores_open_tasks_with_blank_reply_columns(session):
    _two_tasks(session)
    sheet = stt.store_sheet(session, "ABD", TODAY)
    assert sheet["store"].tolist() == ["ABD"] and sheet["task_id"].tolist() == ["ST-000001"]
    assert sheet["reply"].tolist() == [""] and "question" in sheet.columns
    # the sheet's own columns are exactly what the reply template needs, plus context
    assert set(stt.TEMPLATE_COLUMNS) <= set(sheet.columns)


def test_export_covers_every_row_in_the_filter_not_the_page(session):
    for i in range(7):
        bill(session, f"B{i}")
    stt.sync_tasks(session, 1, TODAY, CFG)
    allrows = stt.tasks_all(session, TODAY, chunk=3)
    assert len(allrows) == 7 and list(allrows.columns) == stt.GRID_COLUMNS
    assert len(stt.tasks_page(session, TODAY, "Newest raised", 3, 0)) == 3


def test_report_counts_generated_cleared_and_still_open(session):
    sday(session, "ABD", "2026-08-03", opening=30000)
    sday(session, "ABD", "2026-08-04", opening=2000)
    sday(session, "AGT", "2026-08-03", opening=30000)
    sday(session, "AGT", "2026-08-04", opening=30000)
    rep = stt.sync_tasks(session, 1, TODAY, CFG)
    assert rep.generated[stt.HIGH_FLOAT][0] == 3          # three high store-days seen
    assert rep.already_clear[stt.HIGH_FLOAT][0] == 1      # ABD's, cleared next day - not persisted
    assert rep.created[stt.HIGH_FLOAT] == 2 and rep.still_open[stt.HIGH_FLOAT][0] == 2
