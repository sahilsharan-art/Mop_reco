"""Bulk accept: two separate rules (exact single candidate / split that sums exactly), re-validated at the moment of
applying, applied as manual matches with source "bulk accept", fully reversible as a batch."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import bulk_accept as ba
from mop_reco import exception_pairing as ep
from mop_reco import exception_tables as et
from mop_reco import gateway_txns as gt
from mop_reco import manual_match as mm
from mop_reco import refunds as rf
from mop_reco.db import (Exception_, ManualMatch, R1BillLine, Run, StoreTask, get_engine, get_session, init_db)
from mop_reco.exceptions import bill_exception_key, txn_exception_key

DAY = "2026-07-20"


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), business_dates="[]", status="completed"))
    s.commit()
    return s


def add_bill(s, bill_no, amount, store="ABD", mop="PAYTM EDC"):
    s.add(R1BillLine(run_id=1, business_date=DAY, store=store, bill_no=bill_no, line_seq=1, mop=mop,
                     mop_class="digital", amount=amount, matched=False, status="Not found in gateway",
                     bill_time="12:30:00", created_by="C"))
    s.add(Exception_(exception_key=bill_exception_key(DAY, store, bill_no, mop), reconciliation="R1",
                     type="bill_not_matched", business_date=DAY, store=store, amount=amount,
                     detail=json.dumps({"bill_no": bill_no, "mop": mop}), status="open",
                     first_seen_run_id=1, last_seen_run_id=1))
    s.add(StoreTask(task_key=f"open_bill:{DAY}|{store}|{bill_no}|{mop}", generation=1, task_type="open_bill",
                    store=store, business_date=DAY, bill_no=bill_no, mop=mop, amount=amount, detail="{}",
                    raised_on="2026-10-14", due_by="2026-10-17", status="open", first_seen_run_id=1,
                    last_seen_run_id=1))
    s.commit()


def add_txn(s, txn, amount, store="ABD"):
    s.add(Exception_(exception_key=txn_exception_key("paytm", txn), reconciliation="R1",
                     type="unmapped_gateway_transaction", business_date=DAY, store=store, amount=amount,
                     detail=json.dumps({"gateway": "paytm", "transaction_id": txn, "payment_mode": "UPI"}),
                     status="open", first_seen_run_id=1, last_seen_run_id=1, transaction_time="12:31:00"))
    s.commit()


def suggestions(s):
    pool, bills = ep.unmapped_pool(s), ep.open_population(s, 1)
    return bills, pool, ep.assign(bills, pool)


def status(s, key):
    return s.query(Exception_).filter_by(exception_key=key).one().status


def test_exact_rule_accepts_only_a_single_exact_same_store_candidate(session):
    add_bill(session, "B1", 500.0); add_txn(session, "T1", 500.0)                                   # exact
    add_bill(session, "B2", 11381.0); add_txn(session, "T2a", 6000.0); add_txn(session, "T2b", 5381.0)   # split
    add_bill(session, "B3", 300.0); add_bill(session, "B4", 300.0); add_txn(session, "T3", 300.0)   # ambiguous
    add_bill(session, "B5", 777.0)                                                                  # none
    bills, pool, a = suggestions(session)
    s = ba.build(bills, pool, a)
    assert [r.bill_no for r in s.exact] == ["B1"] and s.exact_value == 500.0
    assert [r.bill_no for r in s.split] == ["B2"] and s.split_value == 11381.0
    assert s.left_n == 3 and s.left_value == 300.0 + 300.0 + 777.0
    assert s.exact_n + s.split_n + s.left_n == s.bills == 5


def test_exact_accept_cascades_and_a_batch_reverses_with_history(session):
    add_bill(session, "B1", 500.0); add_txn(session, "T1", 500.0)
    bills, pool, a = suggestions(session)
    s = ba.build(bills, pool, a)
    rep = ba.apply(session, 1, s.exact, "Aditi", "exact")
    assert (rep.applied, rep.bills_closed, rep.transactions_consumed, rep.tasks_closed) == (1, 1, 1, 1)
    assert rep.open_value_before == 500.0 and rep.open_value_after == 0.0 and not rep.refused
    m = session.query(ManualMatch).one()
    assert (m.source, m.reason_code, m.operator, m.lifecycle) == ("bulk accept", "bulk_accept", "Aditi", "active")
    assert status(session, bill_exception_key(DAY, "ABD", "B1", "PAYTM EDC")) == "closed"
    assert status(session, txn_exception_key("paytm", "T1")) == "closed"
    assert session.query(StoreTask).one().status == "closed"
    assert ("paytm", "T1") in mm.consumed_transactions(session)
    frame = et.manual_bill_frame(session, 1)
    assert list(frame["source"]) == ["bulk accept"] and frame.iloc[0]["matched_by"] == "Aditi"
    assert list(et.manual_txn_frame(session)["source"]) == ["bulk accept"]
    assert mm.reverse_batch(session, m.batch_id, "Aditi", "test reversal") == 1
    assert status(session, bill_exception_key(DAY, "ABD", "B1", "PAYTM EDC")) == "open"
    assert status(session, txn_exception_key("paytm", "T1")) == "open"
    assert session.query(ManualMatch).one().lifecycle == "reversed"                      # the record is kept
    assert session.query(StoreTask).filter(StoreTask.status == "open").count() == 1      # a new task (generation 2)


def test_split_consumes_both_transactions_closes_the_bill_once_and_reverses(session):
    add_bill(session, "BHR", 11381.0); add_txn(session, "A", 6000.0); add_txn(session, "B", 5381.0)
    bills, pool, a = suggestions(session)
    s = ba.build(bills, pool, a)
    assert s.exact_n == 0 and s.split_n == 1
    # the exact button never touches a split row
    assert ba.apply(session, 1, s.split, "Aditi", "exact").applied == 0
    rep = ba.apply(session, 1, s.split, "Aditi", "split")
    assert (rep.bills_closed, rep.transactions_consumed) == (1, 2) and rep.open_value_after == 0.0
    assert session.query(ManualMatch).count() == 1                                       # the bill closes ONCE
    assert {("paytm", "A"), ("paytm", "B")} <= mm.consumed_transactions(session)
    for t in ("A", "B"):
        assert status(session, txn_exception_key("paytm", t)) == "closed"
    assert sorted(et.manual_txn_frame(session)["transaction_id"]) == ["A", "B"]
    f = et.manual_bill_frame(session, 1).iloc[0]
    assert (f["Txn 1"], f["Txn 2"], f["Amount 2"]) == ("A", "B", 5381.0)
    mapped = gt.with_bill_mapping(session, pd.DataFrame({"gateway": ["paytm", "paytm"], "txn_ref": ["A", "B"]}))
    assert list(mapped["mapped"]) == [True, True]
    assert mm.reverse_batch(session, rep.batch_id, "Aditi", "undo") == 1
    for t in ("A", "B"):
        assert status(session, txn_exception_key("paytm", t)) == "open"
    assert mm.consumed_transactions(session) == set()


def test_a_transaction_claimed_since_the_suggestion_is_refused_and_reported(session):
    add_bill(session, "B1", 500.0); add_txn(session, "T1", 500.0)
    add_bill(session, "B2", 800.0); add_txn(session, "T2", 800.0)
    bills, pool, a = suggestions(session)
    s = ba.build(bills, pool, a)
    assert s.exact_n == 2
    # someone refunds T1 after the suggestions were built
    rf.record_refund(session, {"gateway": "paytm", "txn_ref": "T1", "business_date": DAY, "store": "ABD",
                               "amount": 500.0}, "Cust", "Z-1", "21/07/2026", "Someone")
    rep = ba.apply(session, 1, s.exact, "Aditi", "exact")
    assert rep.applied == 1 and len(rep.refused) == 1
    assert rep.refused[0]["bill_no"] == "B1" and "no longer unmapped" in rep.refused[0]["why"]
    assert status(session, bill_exception_key(DAY, "ABD", "B1", "PAYTM EDC")) == "open"


def test_one_transaction_offered_to_two_bills_refuses_both(session):
    add_bill(session, "B1", 500.0); add_bill(session, "B2", 500.0); add_txn(session, "T1", 500.0)
    leg = ba.Leg("paytm", "T1", 500.0, DAY, "")
    rows = [ba.BulkRow(bill_exception_key(DAY, "ABD", b, "PAYTM EDC"), b, DAY, "ABD", "PAYTM EDC", 500.0, [leg], "exact")
            for b in ("B1", "B2")]
    rep = ba.apply(session, 1, rows, "Aditi", "exact")
    assert rep.applied == 0 and len(rep.refused) == 2 and session.query(ManualMatch).count() == 0
