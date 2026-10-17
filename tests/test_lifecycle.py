"""The lifecycle of a closed exception: a bill and its transaction close TOGETHER (by a manual match, inline or
by upload) or a transaction by a customer refund; everything that reads them moves at once; either can be
reversed; nothing is destroyed. Upload is the SAME file the table exports, validated like an inline row and
applied all or nothing."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import exception_tables as et
from mop_reco import manual_match as mm
from mop_reco import queries as q
from mop_reco import refunds as rf
from mop_reco import store_tasks as stt
from mop_reco import table_upload as tu
from mop_reco.db import (Exception_, ManualMatch, R1BillLine, RefundEvent, Run, StoreTask, TransactionRefund,
                         TxnDayAgg, get_engine, get_session, init_db)
from mop_reco.exceptions import bill_exception_key, txn_exception_key

TODAY = date(2026, 10, 14)
DAY = "2026-07-20"


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), business_dates="[]", status="completed"))
    s.commit()
    return s


def make_pair(session, bill_no="SB1/0001", txn="TXN-1", amount=500.0, store="ABD", mop="PAYTM EDC"):
    """An open bill and an unmapped transaction of the same store and amount, with their exceptions."""
    session.add(R1BillLine(run_id=1, business_date=DAY, store=store, bill_no=bill_no, line_seq=1, mop=mop,
                           mop_class="digital", amount=amount, matched=False, status="Not found in gateway",
                           bill_time="12:30:00", created_by="CASH2"))
    session.add(Exception_(exception_key=bill_exception_key(DAY, store, bill_no, mop), reconciliation="R1",
                           type="bill_not_matched", business_date=DAY, store=store, amount=amount,
                           detail=json.dumps({"bill_no": bill_no, "mop": mop}), status="open",
                           first_seen_run_id=1, last_seen_run_id=1))
    session.add(Exception_(exception_key=txn_exception_key("paytm", txn), reconciliation="R1",
                           type="unmapped_gateway_transaction", business_date=DAY, store=store, amount=amount,
                           detail=json.dumps({"gateway": "paytm", "transaction_id": txn, "payment_mode": "UPI"}),
                           status="open", first_seen_run_id=1, last_seen_run_id=1, transaction_time="12:31:00"))
    session.commit()


def bill_of(bill_no="SB1/0001", amount=500.0, store="ABD", mop="PAYTM EDC"):
    return {"business_date": DAY, "store": store, "bill_no": bill_no, "mop": mop, "amount": amount}


def pool_for(txn="TXN-1", amount=500.0, store="ABD"):
    return pd.DataFrame([{"gateway": "paytm", "txn_ref": txn, "business_date": DAY, "store": store, "amount": amount,
                          "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI"}])


def status_of(session, key):
    return session.query(Exception_).filter_by(exception_key=key).one().status


BILL_KEY = bill_exception_key(DAY, "ABD", "SB1/0001", "PAYTM EDC")
TXN_KEY = txn_exception_key("paytm", "TXN-1")


def match(session, who="Aditi", **kw):
    v = mm.validate_by_id(session, bill_of(), "TXN-1", gateway_pool=pool_for())
    assert v.ok
    return mm.record(session, bill_of(), v.gateway, "TXN-1", "manual_match_by_bill", who, v, **kw)


# --- both sides close together; a task closes; the tables show it ------------------------------

def test_a_match_closes_the_bill_the_transaction_and_the_store_task(session):
    make_pair(session)
    stt.sync_tasks(session, 1, TODAY, {})
    assert [t.status for t in session.query(StoreTask).filter_by(task_type="open_bill").all()] == ["open"]
    match(session, source="inline")
    assert status_of(session, BILL_KEY) == "closed" and status_of(session, TXN_KEY) == "closed"
    task = session.query(StoreTask).filter_by(task_type="open_bill").one()
    assert task.status == "closed" and task.closed_by == stt.RESOLVED_BY_DATA
    mb = et.manual_bill_frame(session, 1)
    mt = et.manual_txn_frame(session)
    assert list(mb.columns[:len(et.bill_matched_columns())]) == et.bill_matched_columns()
    assert mb.iloc[0]["matched_txn"] == "TXN-1" and mb.iloc[0]["source"] == "inline"
    assert mt.iloc[0]["matched_bill"] == "SB1/0001" and mt.iloc[0]["resolution"] == "matched to bill"


def test_a_reversal_returns_both_and_raises_the_task_again_and_keeps_history(session):
    make_pair(session)
    stt.sync_tasks(session, 1, TODAY, {})
    m = match(session)
    mm.reverse(session, m.id, "Aditi", "wrong bill")
    assert status_of(session, BILL_KEY) == "open" and status_of(session, TXN_KEY) == "open"
    tasks = session.query(StoreTask).filter_by(task_type="open_bill").order_by(StoreTask.generation).all()
    assert [t.status for t in tasks] == ["closed", "open"] and [t.generation for t in tasks] == [1, 2]
    kept = session.get(ManualMatch, m.id)
    assert kept.lifecycle == "reversed" and kept.reversed_by == "Aditi" and kept.lifecycle_note == "wrong bill"
    assert et.manual_bill_frame(session, 1).empty and et.manual_txn_frame(session).empty


def test_a_batch_without_commit_can_be_rolled_back_whole(session):
    make_pair(session)
    match(session, commit=False)
    session.rollback()
    assert status_of(session, BILL_KEY) == "open" and status_of(session, TXN_KEY) == "open"
    assert session.query(ManualMatch).count() == 0


# --- customer refund --------------------------------------------------------------------------

def txn_row():
    return {"gateway": "paytm", "txn_ref": "TXN-1", "business_date": DAY, "store": "ABD", "amount": 500.0}


def test_a_refund_resolves_the_transaction_never_a_bill(session):
    make_pair(session)
    stt.sync_tasks(session, 1, TODAY, {})
    rf.record_refund(session, txn_row(), "Ramesh Kumar", "ZOHO-1", "21/07/2026", "Aditi")
    assert status_of(session, TXN_KEY) == "closed"
    assert status_of(session, BILL_KEY) == "open"                                   # never closes a bill
    assert session.query(StoreTask).filter_by(task_type="open_bill").one().status == "open"
    mt = et.manual_txn_frame(session)
    assert mt.iloc[0]["resolution"] == "customer refund" and mt.iloc[0]["zoho_no"] == "ZOHO-1"
    assert mt.iloc[0]["refund_date"] == "21/07/2026" and mt.iloc[0]["matched_bill"] == ""


@pytest.mark.parametrize("name,zoho,rdate,expect", [
    ("", "Z1", "21/07/2026", "customer_name"), ("A", "", "21/07/2026", "zoho_no"), ("A", "Z1", "", "refund_date")])
def test_all_three_refund_fields_are_required(session, name, zoho, rdate, expect):
    make_pair(session)
    with pytest.raises(ValueError, match=expect):
        rf.record_refund(session, txn_row(), name, zoho, rdate, "Aditi")
    assert session.query(TransactionRefund).count() == 0 and status_of(session, TXN_KEY) == "open"


def test_a_zoho_number_cannot_be_used_twice_and_a_reversal_frees_it(session):
    make_pair(session)
    make_pair(session, bill_no="SB1/0002", txn="TXN-2")
    rf.record_refund(session, txn_row(), "A", "ZOHO-1", "21/07/2026", "Aditi")
    second = dict(txn_row(), txn_ref="TXN-2")
    with pytest.raises(ValueError, match="already used"):
        rf.record_refund(session, second, "B", "ZOHO-1", "21/07/2026", "Aditi")
    rid = session.query(TransactionRefund).one().id
    rf.reverse_refund(session, rid, "Aditi", "entered in error")
    assert status_of(session, TXN_KEY) == "open" and session.query(TransactionRefund).count() == 0
    assert [e.action for e in session.query(RefundEvent).order_by(RefundEvent.id).all()] == ["recorded", "reversed"]
    rf.record_refund(session, second, "B", "ZOHO-1", "21/07/2026", "Aditi")          # the number is free again


def test_a_transaction_already_matched_to_a_bill_cannot_be_refunded(session):
    make_pair(session)
    match(session)
    with pytest.raises(ValueError, match="already matched to bill SB1/0001"):
        rf.record_refund(session, txn_row(), "A", "ZOHO-1", "21/07/2026", "Aditi")


# --- one column definition for the table, its export and its template ------------------------

def test_template_columns_are_the_exports_columns_exactly():
    assert list(tu.template_frame("bills").columns) == et.bill_columns(pairing=True, screen=False)
    assert list(tu.template_frame("txns").columns) == list(et.TXN_COLUMNS)
    assert not set(et.BILL_UI_ONLY) & set(tu.template_frame("bills").columns)


def test_the_matched_tables_carry_the_exception_tables_columns_then_who_when_how():
    assert et.bill_matched_columns()[:len(et.BILL_EXPORT_COLUMNS)] == et.BILL_EXPORT_COLUMNS
    assert et.bill_matched_columns()[-4:] == ["matched_txn", "matched_by", "matched_at", "source"]
    assert et.txn_matched_columns()[:len(et.TXN_COLUMNS)] == et.TXN_COLUMNS
    assert et.txn_matched_columns()[-4:] == ["resolution", "matched_by", "matched_at", "source"]


def bills_file(rows):
    base = {c: "" for c in et.bill_columns(pairing=True, screen=False)}
    return pd.DataFrame([dict(base, **r) for r in rows])


def txns_file(rows):
    base = {c: "" for c in et.TXN_COLUMNS}
    return pd.DataFrame([dict(base, **r) for r in rows])


def row_for(bill_no="SB1/0001", **extra):
    return dict({"date": "20/07/2026", "store": "ABD", "bill_no": bill_no, "mop": "PAYTM EDC", "amount": "500.00"},
                **extra)


def test_the_export_round_trips_through_the_reader(session):
    import io

    from mop_reco.export import ExportMeta, build_export
    cols = et.bill_columns(pairing=True, screen=False)
    frame = bills_file([row_for(**{"Txn 1": "TXN-1"})])
    res = build_export(frame, ExportMeta(run_id=1, run_timestamp=datetime(2026, 9, 1), title="Exception bills"))
    back = tu.read_table_file(res.data, res.filename, cols)
    assert list(back.columns) == cols and back.iloc[0]["Txn 1"] == "TXN-1" and back.iloc[0]["bill_no"] == "SB1/0001"


# --- upload: validated like inline, all or nothing --------------------------------------------

def test_an_uploaded_match_closes_both_sides_and_the_task_with_source_upload(session):
    make_pair(session)
    stt.sync_tasks(session, 1, TODAY, {})
    prev = tu.preview_bills(session, 1, bills_file([row_for(**{"Txn 1": "TXN-1"})]))
    assert prev.can_apply and prev.accepted[0].kind == "match"
    tu.apply_preview(session, prev, "Aditi", file_name="f.xlsx")
    assert status_of(session, BILL_KEY) == "closed" and status_of(session, TXN_KEY) == "closed"
    assert session.query(StoreTask).filter_by(task_type="open_bill").one().status == "closed"
    assert et.manual_bill_frame(session, 1).iloc[0]["source"] == "upload"
    assert et.manual_txn_frame(session).iloc[0]["source"] == "upload"


def test_a_row_with_nothing_filled_in_is_ignored(session):
    make_pair(session)
    prev = tu.preview_bills(session, 1, bills_file([row_for()]))
    assert prev.rows[0].verdict == "ignore" and not prev.can_apply


def test_one_rejected_row_applies_nothing(session):
    make_pair(session)
    make_pair(session, bill_no="SB1/0002", txn="TXN-2", amount=700.0)
    prev = tu.preview_bills(session, 1, bills_file([
        row_for(**{"Txn 1": "TXN-1"}),
        row_for("SB1/0002", amount="700.00", **{"Txn 1": "TXN-1"})]))        # the same transaction twice
    assert len(prev.accepted) == 0 and len(prev.rejected) == 2 and not prev.can_apply   # BOTH rows: neither is more right
    with pytest.raises(ValueError, match="nothing is applied"):
        tu.apply_preview(session, prev, "Aditi")
    assert session.query(ManualMatch).count() == 0 and status_of(session, BILL_KEY) == "open"


def test_a_transaction_already_mapped_elsewhere_is_refused_naming_the_bill(session, monkeypatch):
    make_pair(session)
    session.add(R1BillLine(run_id=1, business_date=DAY, store="ABD", bill_no="SB9/0009", line_seq=1,
                           mop="PAYTM EDC", mop_class="digital", amount=500.0, matched=True,
                           matched_gateway="paytm", matched_txn_ref="TXN-TAKEN", status="Tier 1"))
    session.commit()
    monkeypatch.setattr(mm, "targeted_pool", lambda ref, warehouse=None: pool_for("TXN-TAKEN"))
    prev = tu.preview_bills(session, 1, bills_file([row_for(**{"Txn 1": "TXN-TAKEN"})]))
    assert prev.rejected and "SB9/0009" in " ".join(prev.rejected[0].why)


def test_an_amount_that_differs_is_rejected_not_warned(session):
    make_pair(session)
    make_pair(session, bill_no="SB1/0002", txn="TXN-2", amount=999.0)
    prev = tu.preview_bills(session, 1, bills_file([row_for(**{"Txn 1": "TXN-2"})]))       # a 500 bill, a 999 txn
    assert prev.rejected and "Amount differs" in " ".join(prev.rejected[0].why)


def test_a_missing_bill_and_a_closed_bill_are_rejected(session):
    make_pair(session)
    match(session)
    make_pair(session, bill_no="SB1/0003", txn="TXN-3", amount=300.0)
    prev = tu.preview_bills(session, 1, bills_file([
        row_for("NOPE/1", **{"Txn 1": "TXN-3"}), row_for(**{"Txn 1": "TXN-1"})]))
    assert [r.verdict for r in prev.rows] == ["reject", "reject"]


def test_an_uploaded_refund_and_its_all_or_nothing_rollback(session):
    make_pair(session)
    make_pair(session, bill_no="SB1/0002", txn="TXN-2", amount=700.0)
    prev = tu.preview_txns(session, 1, txns_file([
        {"gateway": "paytm", "transaction_id": "TXN-1", "customer_name": "Ramesh", "zoho_no": "Z-1",
         "refund_date": "21/07/2026"},
        {"gateway": "paytm", "transaction_id": "TXN-2", "customer_name": "Sita", "zoho_no": "Z-1",
         "refund_date": "21/07/2026"}]))                                         # the same Zoho number twice
    assert len(prev.accepted) == 0 and len(prev.rejected) == 2 and not prev.can_apply   # BOTH rows: neither is more right
    assert session.query(TransactionRefund).count() == 0
    good = tu.preview_txns(session, 1, txns_file([
        {"gateway": "paytm", "transaction_id": "TXN-1", "customer_name": "Ramesh", "zoho_no": "Z-1",
         "refund_date": "21/07/2026"}]))
    tu.apply_preview(session, good, "Aditi")
    assert status_of(session, TXN_KEY) == "closed" and status_of(session, BILL_KEY) == "open"
    assert et.manual_txn_frame(session).iloc[0]["resolution"] == "customer refund"


def test_a_transaction_row_cannot_be_both_a_match_and_a_refund(session):
    make_pair(session)
    prev = tu.preview_txns(session, 1, txns_file([
        {"gateway": "paytm", "transaction_id": "TXN-1", "matched_bill": "SB1/0001", "customer_name": "R",
         "zoho_no": "Z", "refund_date": "21/07/2026"}]))
    assert prev.rejected and "not both" in " ".join(prev.rejected[0].why)


def test_an_uploaded_transaction_to_bill_match(session):
    make_pair(session)
    stt.sync_tasks(session, 1, TODAY, {})
    prev = tu.preview_txns(session, 1, txns_file([
        {"gateway": "paytm", "transaction_id": "TXN-1", "matched_bill": "SB1/0001"}]))
    assert prev.can_apply
    tu.apply_preview(session, prev, "Aditi")
    assert status_of(session, BILL_KEY) == "closed" and status_of(session, TXN_KEY) == "closed"
    assert session.query(StoreTask).filter_by(task_type="open_bill").one().status == "closed"


# --- the controls -------------------------------------------------------------------------------

def test_only_an_accepted_match_closes_a_bill_in_the_counts(session):
    make_pair(session)
    session.add(ManualMatch(bill_key=BILL_KEY, business_date=DAY, store="ABD", bill_no="SB1/0001", mop="PAYTM EDC",
                            amount=500.0, gateway="unknown", transaction_ref="X", status="asserted_unverified",
                            validation_case="c", override_used=False, reason_code="r", operator="A",
                            created_at=datetime.now(), lifecycle="active"))
    session.commit()
    assert q._active_manual_bill_nos(session) == set()           # an assertion with no transaction closes nothing


def test_bill_split_sums_to_total(session):
    make_pair(session)
    session.add(R1BillLine(run_id=1, business_date=DAY, store="ABD", bill_no="SB5/1", line_seq=1, mop="PAYTM EDC",
                           mop_class="digital", amount=100.0, matched=True, matched_gateway="paytm",
                           matched_txn_ref="T9", status="Tier 1"))
    session.commit()
    b = q.bill_split(session, 1, DAY, DAY)
    assert (b["open"]["count"], b["auto"]["count"], b["manual"]["count"], b["total"]["count"]) == (1, 1, 0, 2)
    match(session)
    b = q.bill_split(session, 1, DAY, DAY)
    assert (b["open"]["count"], b["auto"]["count"], b["manual"]["count"], b["total"]["count"]) == (0, 1, 1, 2)
    assert b["open"]["value"] + b["auto"]["value"] + b["manual"]["value"] == b["total"]["value"]


def test_transaction_strip_splits_auto_manual_refunded_unmapped_and_sums(session):
    make_pair(session)
    make_pair(session, bill_no="SB1/0002", txn="TXN-2", amount=700.0)
    make_pair(session, bill_no="SB1/0003", txn="TXN-3", amount=300.0)
    session.add(TxnDayAgg(run_id=1, business_date=DAY, store="ABD", gateway="paytm", total_n=14, total_v=7000.0,
                          mapped_n=10, mapped_v=6000.0, pool_n=13, pool_v=6990.0, consumed_n=10, consumed_v=5490.0))
    session.commit()
    match(session)
    rf.record_refund(session, dict(txn_row(), txn_ref="TXN-2", amount=700.0), "A", "Z-1", "21/07/2026", "Aditi")
    t = q.transaction_resolution_summary(session, DAY, DAY, None, run_id=1)
    assert t.sale_basis and t.total_n == 13 and t.not_sale_n == 1
    assert (t.auto_n, t.manual_n, t.refunded_n, t.unmapped_n) == (10, 1, 1, 1)
    assert t.ok and t.auto_n + t.manual_n + t.refunded_n + t.unmapped_n == t.total_n
    ov = q.match_overview(session, 1, DAY, DAY)
    assert ov.txns_ok and ov.exception_txns_n == 1 and ov.unmapped_agrees_with_exceptions


def test_two_rows_naming_the_same_transaction_are_both_rejected_and_unchanged_rows_are_skipped(session):
    make_pair(session)
    make_pair(session, bill_no="SB1/0002", txn="TXN-2", amount=500.0)
    make_pair(session, bill_no="SB1/0003", txn="TXN-3", amount=500.0)
    rows = [row_for(**{"Txn 1": "TXN-1"}), row_for("SB1/0002", **{"Txn 1": "TXN-1"}), row_for("SB1/0003")]
    prev = tu.preview_bills(session, 1, bills_file(rows))
    assert [r.verdict for r in prev.rows] == ["reject", "reject", "ignore"]
    assert "also on row" in " ".join(prev.rows[0].why) and "also on row" in " ".join(prev.rows[1].why)
    assert prev.actionable_rows == 2 and prev.total_rows == 3
    assert prev.summary_line() == "2 of 3 rows carry a transaction or a change; 1 skipped as unchanged."
