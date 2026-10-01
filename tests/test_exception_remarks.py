"""Editable remarks on Exception bills/transactions — bulk-upload
validation and the shared write path (2026-09-30). A remark is annotation
only: these tests assert it never touches a figure, a status or a match,
only ever adds a new Remark row, and that bulk upload is genuinely
all-or-nothing."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco.db import Exception_, Remark, Run, get_engine, get_session, init_db
from mop_reco.exceptions import bill_exception_key, txn_exception_key
from mop_reco import exception_remarks as er


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "er.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def seeded(session):
    run = Run(started_at=datetime(2026, 9, 1, 10, 0), status="completed",
              triggered_by="test", business_dates=json.dumps(["2026-07-01", "2026-07-02"]))
    session.add(run)
    session.commit()

    open_bill_key = bill_exception_key("2026-07-01", "ABD", "SA1/001", "Cash")
    closed_bill_key = bill_exception_key("2026-07-02", "XYZ", "SA1/002", "Cash")
    open_txn_key = txn_exception_key("paytm", "20260701000000000000000000000000001")

    session.add_all([
        Exception_(exception_key=open_bill_key, reconciliation="R1",
                  type="bill_not_matched", business_date="2026-07-01",
                  store="ABD", amount=500.0, detail="{}", status="open",
                  first_seen_run_id=run.id, last_seen_run_id=run.id),
        Exception_(exception_key=closed_bill_key, reconciliation="R1",
                  type="bill_not_matched", business_date="2026-07-02",
                  store="XYZ", amount=700.0, detail="{}", status="closed",
                  first_seen_run_id=run.id, last_seen_run_id=run.id),
        Exception_(exception_key=open_txn_key, reconciliation="R1",
                  type="unmapped_gateway_transaction", business_date="2026-07-01",
                  store="ABD", amount=1234.0, detail="{}", status="open",
                  first_seen_run_id=run.id, last_seen_run_id=run.id),
    ])
    session.commit()
    return {"open_bill_key": open_bill_key, "closed_bill_key": closed_bill_key,
           "open_txn_key": open_txn_key}


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------

def test_a_clean_bill_row_validates_ok(session, seeded):
    df = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                       "bill_no": "SA1/001", "mop": "Cash",
                       "remark": "checked with store manager"}])
    results = er.validate_bill_remarks(session, df)
    assert len(results) == 1
    assert results[0].ok
    assert results[0].exception_key == seeded["open_bill_key"]


def test_a_bill_row_for_an_unknown_key_is_rejected(session, seeded):
    df = pd.DataFrame([{"business_date": "2026-07-01", "store": "NOPE",
                       "bill_no": "SA1/999", "mop": "Cash", "remark": "hello"}])
    results = er.validate_bill_remarks(session, df)
    assert not results[0].ok
    assert "no exception" in results[0].reason


def test_a_bill_row_for_a_closed_exception_is_rejected(session, seeded):
    df = pd.DataFrame([{"business_date": "2026-07-02", "store": "XYZ",
                       "bill_no": "SA1/002", "mop": "Cash", "remark": "hello"}])
    results = er.validate_bill_remarks(session, df)
    assert not results[0].ok
    assert "not an open exception" in results[0].reason
    assert "closed" in results[0].reason


def test_a_bill_row_with_an_empty_remark_is_rejected(session, seeded):
    df = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                       "bill_no": "SA1/001", "mop": "Cash", "remark": "  "}])
    results = er.validate_bill_remarks(session, df)
    assert not results[0].ok
    assert "empty" in results[0].reason


# ---------------------------------------------------------------------------
# Transactions — the apostrophe/full-digit-string rule
# ---------------------------------------------------------------------------

def test_a_txn_id_with_a_leading_apostrophe_still_matches(session, seeded):
    df = pd.DataFrame([{"gateway": "paytm",
                       "transaction_id": "'20260701000000000000000000000000001",
                       "remark": "confirmed with gateway portal"}])
    results = er.validate_txn_remarks(session, df)
    assert results[0].ok
    assert results[0].exception_key == seeded["open_txn_key"]


def test_a_txn_row_for_an_unknown_id_is_rejected(session, seeded):
    df = pd.DataFrame([{"gateway": "paytm", "transaction_id": "99999999999999999999999999999999999",
                       "remark": "hello"}])
    results = er.validate_txn_remarks(session, df)
    assert not results[0].ok
    assert "no exception" in results[0].reason


# ---------------------------------------------------------------------------
# Apply — all-or-nothing, append-only, never a figure/status/match change
# ---------------------------------------------------------------------------

def test_apply_writes_one_new_remark_row_per_accepted_row(session, seeded):
    df = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                       "bill_no": "SA1/001", "mop": "Cash",
                       "remark": "first remark"}])
    results = er.validate_bill_remarks(session, df)
    assert all(r.ok for r in results)
    n = er.apply_bulk_remarks(session, results, operator="aditi")
    assert n == 1
    row = session.query(Exception_).filter_by(exception_key=seeded["open_bill_key"]).one()
    remarks = session.query(Remark).filter_by(exception_id=row.id).all()
    assert len(remarks) == 1
    assert remarks[0].remark_text == "first remark"
    assert remarks[0].created_by == "aditi"
    # A remark never changes the exception's own figure, status or match.
    assert row.status == "open"
    assert float(row.amount) == 500.0


def test_a_second_remark_never_overwrites_the_first(session, seeded):
    """Rule 4 — history is kept, never silently replaced."""
    df = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                       "bill_no": "SA1/001", "mop": "Cash", "remark": "first"}])
    er.apply_bulk_remarks(session, er.validate_bill_remarks(session, df), operator="aditi")
    df2 = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                        "bill_no": "SA1/001", "mop": "Cash", "remark": "second, corrected"}])
    er.apply_bulk_remarks(session, er.validate_bill_remarks(session, df2), operator="raj")

    row = session.query(Exception_).filter_by(exception_key=seeded["open_bill_key"]).one()
    remarks = (session.query(Remark).filter_by(exception_id=row.id)
              .order_by(Remark.created_at.asc()).all())
    assert [r.remark_text for r in remarks] == ["first", "second, corrected"]
    assert [r.created_by for r in remarks] == ["aditi", "raj"]


def test_preview_frame_shows_accepted_and_rejected_with_reasons(session, seeded):
    df = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "bill_no": "SA1/001",
         "mop": "Cash", "remark": "ok"},
        {"business_date": "2026-07-02", "store": "XYZ", "bill_no": "SA1/002",
         "mop": "Cash", "remark": "ok"},
    ])
    results = er.validate_bill_remarks(session, df)
    preview = er.preview_frame(results)
    assert list(preview["result"]) == ["Accepted", "Rejected"]
    assert preview.iloc[1]["reason"] != ""


def test_templates_carry_exactly_the_specified_columns():
    import io as _io
    bill_df = pd.read_csv(_io.BytesIO(er.bill_template_csv()))
    txn_df = pd.read_csv(_io.BytesIO(er.txn_template_csv()))
    assert list(bill_df.columns) == er.BILL_TEMPLATE_COLUMNS
    assert list(txn_df.columns) == er.TXN_TEMPLATE_COLUMNS
