"""Audit D-011 (found in UAT case E04): a manual match must move the Summary matrix, whatever recorded the payment mode.

After a manual match, the store sheet's received column moved but the matrix's UPI / Total digital Receivable did not. The matrix finds a matched
bill's mode (card or UPI) in the match's stored validation. Bulk accept built its validation pool WITHOUT the payment mode, so every bulk-accepted
Paytm match carried none, the mode was unknown, and the bill stayed counted as open provisional UPI although every other table called it closed.

Now: the pool carries the mode; a match with none recorded is looked up from the transaction's exception; if it is still unknown the bill moves to
the system's documented default (UPI) rather than staying receivable; and an 'asserted, unverified' match never moves anything."""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import test_bulk_accept as tb  # noqa: E402
import test_mop_matrix as tmm  # noqa: E402
from mop_reco import bulk_accept as ba  # noqa: E402
from mop_reco import exception_pairing as ep  # noqa: E402
from mop_reco import store_sheet_source as src  # noqa: E402
from mop_reco.db import Exception_, ManualMatch  # noqa: E402
from mop_reco.exceptions import txn_exception_key  # noqa: E402

session = tmm.session
run = tmm.run
D1 = date(2026, 7, 1)


def _manual(session, bill_no, ref, gateway="paytm", status="matched_manual", mode_in_detail=None, amount=50.0):
    detail = {"txn": {"payment_mode": mode_in_detail}} if mode_in_detail is not None else {"txn": {}}
    session.add(ManualMatch(bill_key=f"2026-07-01|ABD|{bill_no}|PAYTM EDC", business_date="2026-07-01", store="ABD", bill_no=bill_no, mop="PAYTM EDC",
                            amount=amount, gateway=gateway, transaction_ref=ref, status=status, validation_case="a", reason_code="manual_match_by_bill",
                            operator="tester", created_at=datetime(2026, 7, 1, 10, 0), lifecycle="active", validation_detail=json.dumps(detail)))


def _txn_exception(session, ref, mode):
    session.add(Exception_(exception_key=txn_exception_key("paytm", ref), reconciliation="R1", type="unmapped_gateway_transaction", business_date="2026-07-01",
                           store="ABD", amount=50.0, detail=json.dumps({"gateway": "paytm", "transaction_id": ref, "payment_mode": mode}), status="closed",
                           first_seen_run_id=1, last_seen_run_id=1))


def _open_bill(session, run, bill_no, amount=50.0):
    tmm._digital_mode_row(session, run.id, "2026-07-01", "ABD", "unclassified", amount)
    tmm._bill_line(session, run.id, "PAYTM EDC", amount, matched=False, bill_no=bill_no)


def test_a_match_with_the_mode_recorded_moves_the_matrix(session, run):
    _open_bill(session, run, "B1")
    _manual(session, "B1", "T1", mode_in_detail="UPI")
    session.commit()
    sums = src._digital_mode_sales(session, run.id, D1, D1)
    assert (sums["upi_collected"], sums["provisional_upi"], sums["provisional_bills"]) == (50.0, 0.0, 0)


def test_a_match_with_NO_mode_recorded_is_looked_up_from_the_transaction_not_left_open(session, run):
    """The defect: bulk-accepted Paytm matches carried no mode and stayed 'open provisional UPI'."""
    _open_bill(session, run, "B1")
    _manual(session, "B1", "T1", mode_in_detail=None)
    _txn_exception(session, "T1", "DEBIT_CARD")
    session.commit()
    sums = src._digital_mode_sales(session, run.id, D1, D1)
    assert sums["card_collected"] == 50.0 and sums["upi_collected"] == 0.0, "the transaction says debit card, so it is Card"
    assert sums["provisional_upi"] == 0.0 and sums["provisional_bills"] == 0


def test_a_match_with_no_mode_anywhere_is_collected_under_the_documented_default_not_receivable(session, run):
    _open_bill(session, run, "B1")
    _manual(session, "B1", "T-UNKNOWN", mode_in_detail=None)
    session.commit()
    sums = src._digital_mode_sales(session, run.id, D1, D1)
    assert sums["upi_collected"] == 50.0 and sums["provisional_upi"] == 0.0, "matched money must not stay receivable"
    assert sums["upi"] == 50.0 and sums["card"] == 0.0, "card + upi = total digital, no third bucket"


def test_an_asserted_unverified_match_moves_nothing(session, run):
    _open_bill(session, run, "B1")
    _manual(session, "B1", "NO-SUCH-ID", gateway="unknown", status="asserted_unverified", mode_in_detail=None)
    session.commit()
    sums = src._digital_mode_sales(session, run.id, D1, D1)
    assert (sums["upi_collected"], sums["provisional_upi"], sums["provisional_bills"]) == (0.0, 50.0, 1), "an assertion with no transaction is still an open item"


def test_a_reversed_match_moves_nothing(session, run):
    _open_bill(session, run, "B1")
    _manual(session, "B1", "T1", mode_in_detail="UPI")
    session.query(ManualMatch).update({"lifecycle": "reversed"})
    session.commit()
    sums = src._digital_mode_sales(session, run.id, D1, D1)
    assert sums["provisional_upi"] == 50.0 and sums["upi_collected"] == 0.0


def test_many_matches_without_a_mode_are_resolved_in_few_queries(session, run):
    """A bulk accept can record thousands; the lookup must not be one query each."""
    tmm._digital_mode_row(session, run.id, "2026-07-01", "ABD", "unclassified", 1200.0)      # one aggregate row: the open pool
    for i in range(1200):
        tmm._bill_line(session, run.id, "PAYTM EDC", 1.0, matched=False, bill_no=f"BX{i}")
        _manual(session, f"BX{i}", f"TX{i}", mode_in_detail=None, amount=1.0)
        _txn_exception(session, f"TX{i}", "UPI" if i % 2 else "CREDIT_CARD")
    session.commit()
    from sqlalchemy import event
    n = []
    event.listen(session.bind, "before_cursor_execute", lambda *a, **k: n.append(1))
    sums = src._digital_mode_sales(session, run.id, D1, D1)
    assert sums["card_collected"] == 600.0 and sums["upi_collected"] == 600.0 and sums["provisional_upi"] == 0.0
    assert len(n) < 30, f"{len(n)} queries for 1,200 matches"


# --- the source of the defect: bulk accept's validation pool ----------------------------------------------------------------------
def test_the_unmapped_pool_carries_the_payment_mode(tmp_path):
    s = tb.session.__wrapped__(tmp_path) if hasattr(tb.session, "__wrapped__") else None
    if s is None:
        pytest.skip("fixture not callable directly")
    tb.add_bill(s, "B1", 500.0)
    tb.add_txn(s, "T1", 500.0)
    pool = ep.unmapped_pool(s)
    assert "payment_mode" in pool.columns and pool.iloc[0]["payment_mode"] == "UPI"


def test_bulk_accept_validates_with_the_real_payment_mode():
    class Txn:
        txn_id, date, store, amount, payment_mode = "T1", "2026-07-20", "ABD", 500.0, "DEBIT_CARD"
    assert ba._pool_row("paytm", Txn()).iloc[0]["payment_mode"] == "DEBIT_CARD"

    class NoMode:
        txn_id, date, store, amount = "T1", "2026-07-20", "ABD", 500.0
    assert ba._pool_row("paytm", NoMode()).iloc[0]["payment_mode"] is None, "a pool row with no mode still works"
