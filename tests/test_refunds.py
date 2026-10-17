"""Customer refunds (Request B, part 3) — a THIRD way an unmapped
transaction closes, never a bill match. Every validation rule is tested from
the operator's own instruction: a refund is rejected, not merely warned,
when the transaction doesn't exist, is already matched to a bill, the store
doesn't resolve, the store disagrees with the transaction's own, or the
zoho number is reused — and applied all-or-nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import queries as q
from mop_reco import refunds as rf
from mop_reco.db import (
    MasterRow,
    MasterVersion,
    R1BillLine,
    Run,
    TransactionRefund,
    get_engine,
    get_session,
    init_db,
)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "refunds.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def run(session):
    r = Run(started_at=datetime(2026, 7, 20, 9, 0), finished_at=datetime(2026, 7, 20, 9, 5),
            business_dates=json.dumps(["2026-07-20"]), status="completed",
            triggered_by="test")
    session.add(r)
    session.commit()
    return r


def seed_store_master(session, codes):
    version = MasterVersion(
        master_name="store", source_file_path="test", loaded_at=datetime(2026, 1, 1),
        row_count=len(codes), is_current=True, content_hash="test",
    )
    session.add(version)
    session.flush()
    for code in codes:
        session.add(MasterRow(
            master_version_id=version.id,
            row_json=json.dumps({"store": code, "store_name": f"Test Store ({code})"}),
        ))
    session.commit()


def txn_pool(rows):
    """The manual_match pool shape — gateway/txn_ref/business_date/store/
    amount/status/txn_type/payment_mode."""
    return pd.DataFrame(rows, columns=["gateway", "txn_ref", "business_date",
                                       "store", "amount", "status", "txn_type",
                                       "payment_mode"])


CLEAN_POOL = txn_pool([
    ("paytm", "TXN-1", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING", "UPI"),
    ("paytm", "TXN-WRONG-STORE", "2026-07-20", "XYZ", 500.0, "SUCCESS", "ACQUIRING", "UPI"),
])


def upload_row(date="2026-07-20", store_code="ABD", transaction_no="TXN-1",
               zoho_no="ZOHO-1", customer_name="Ramesh", status="pending"):
    return pd.DataFrame([{
        "date": date, "store_code": store_code, "transaction_no": transaction_no,
        "zoho_no": zoho_no, "customer_name": customer_name, "status": status,
    }], columns=rf.UPLOAD_COLUMNS)


# ---------------------------------------------------------------------------
# db.TransactionRefund — the natural key, and the zoho_no uniqueness
# ---------------------------------------------------------------------------

def test_transaction_refund_gateway_and_ref_must_be_unique(session):
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="T1", business_date="2026-07-20",
        store="ABD", amount=100.0, zoho_no="Z1", refund_status="pending",
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 20, 10, 0)))
    session.commit()
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="T1", business_date="2026-07-21",
        store="ABD", amount=100.0, zoho_no="Z2", refund_status="pending",
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 21, 10, 0)))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_transaction_refund_zoho_no_must_be_unique(session):
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="T1", business_date="2026-07-20",
        store="ABD", amount=100.0, zoho_no="Z1", refund_status="pending",
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 20, 10, 0)))
    session.commit()
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="T2", business_date="2026-07-20",
        store="ABD", amount=100.0, zoho_no="Z1", refund_status="pending",
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 20, 10, 0)))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# refunds.preview_upload — validate every row before anything is applied
# ---------------------------------------------------------------------------

def test_preview_upload_accepts_a_clean_row(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)

    preview = rf.preview_upload(session, upload_row())
    assert len(preview.accepted) == 1
    assert len(preview.rejected) == 0
    assert preview.accepted[0].gateway == "paytm"
    assert preview.accepted[0].amount == 500.0


def test_preview_upload_rejects_a_transaction_not_in_any_gateway(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)

    preview = rf.preview_upload(session, upload_row(transaction_no="DOES-NOT-EXIST"))
    assert len(preview.accepted) == 0
    assert len(preview.rejected) == 1
    assert "not in the Paytm, Axis or Bajaj" in preview.rejected[0].why[0]


def test_preview_upload_rejects_a_transaction_already_matched_to_a_bill(session, run, monkeypatch):
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)
    session.add(R1BillLine(
        run_id=run.id, business_date="2026-07-20", store="ABD", bill_no="BILL-1",
        line_seq=1, mop="PAYTM EDC", mop_class="digital", amount=500.0,
        matched=True, matched_gateway="paytm", matched_txn_ref="TXN-1",
        status="Tier 1"))
    session.commit()

    preview = rf.preview_upload(session, upload_row())
    assert len(preview.rejected) == 1
    assert "BILL-1" in preview.rejected[0].why[0]


def test_preview_upload_rejects_an_unknown_store_code(session, monkeypatch):
    # No store master seeded at all — every store_code is unknown.
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)
    preview = rf.preview_upload(session, upload_row(store_code="ZZZ"))
    assert len(preview.rejected) == 1
    assert "store_code" in preview.rejected[0].why[0]


def test_preview_upload_flags_a_store_mismatch_showing_both(session, monkeypatch):
    """The upload names one store, the transaction itself belongs to
    another — flagged and BOTH shown, never silently accepted either way."""
    seed_store_master(session, ["ABD", "XYZ"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)

    preview = rf.preview_upload(session, upload_row(
        store_code="ABD", transaction_no="TXN-WRONG-STORE"))
    assert len(preview.rejected) == 1
    why = preview.rejected[0].why[0]
    assert "ABD" in why and "XYZ" in why


def test_preview_upload_rejects_a_zoho_no_already_used(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="SOME-OTHER-TXN", business_date="2026-07-19",
        store="ABD", amount=500.0, zoho_no="ZOHO-1", refund_status="pending",
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 19, 10, 0)))
    session.commit()

    preview = rf.preview_upload(session, upload_row(zoho_no="ZOHO-1"))
    assert len(preview.rejected) == 1
    assert "already used" in preview.rejected[0].why[0]


def test_preview_upload_rejects_a_zoho_no_duplicated_within_the_same_file(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    pool = txn_pool([
        ("paytm", "TXN-1", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING", "UPI"),
        ("paytm", "TXN-2", "2026-07-20", "ABD", 300.0, "SUCCESS", "ACQUIRING", "UPI"),
    ])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: pool)
    frame = pd.DataFrame([
        {"date": "2026-07-20", "store_code": "ABD", "transaction_no": "TXN-1",
         "zoho_no": "DUPE", "customer_name": "A", "status": "pending"},
        {"date": "2026-07-20", "store_code": "ABD", "transaction_no": "TXN-2",
         "zoho_no": "DUPE", "customer_name": "B", "status": "pending"},
    ], columns=rf.UPLOAD_COLUMNS)

    preview = rf.preview_upload(session, frame)
    assert len(preview.accepted) == 0     # BOTH rows share the Zoho number - neither is more right
    assert len(preview.rejected) == 2
    assert "row 3" in preview.rejected[0].why[0] and "row 2" in preview.rejected[1].why[0]


def test_preview_upload_rejects_an_unrecognised_status(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)
    preview = rf.preview_upload(session, upload_row(status="maybe"))
    assert len(preview.rejected) == 1
    assert "status" in preview.rejected[0].why[0]


def test_preview_upload_raises_on_missing_columns(session):
    bad = pd.DataFrame([{"date": "2026-07-20"}])
    with pytest.raises(ValueError):
        rf.preview_upload(session, bad)


# ---------------------------------------------------------------------------
# refunds.apply_upload — all accepted rows written as one operation; a
# rejected row is never applied.
# ---------------------------------------------------------------------------

def test_apply_upload_writes_only_accepted_rows(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)

    preview = rf.preview_upload(session, upload_row())
    applied, rejected = rf.apply_upload(session, preview, "tester")
    assert len(applied) == 1
    assert len(rejected) == 0

    row = session.query(TransactionRefund).one()
    assert row.gateway == "paytm"
    assert row.transaction_ref == "TXN-1"
    assert row.zoho_no == "ZOHO-1"
    assert row.refund_status == "pending"
    assert row.uploaded_by == "tester"


def test_apply_upload_requires_an_operator(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)
    preview = rf.preview_upload(session, upload_row())
    with pytest.raises(ValueError):
        rf.apply_upload(session, preview, "")


def test_refund_records_lists_newest_first(session, monkeypatch):
    seed_store_master(session, ["ABD"])
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="OLD", business_date="2026-07-01",
        store="ABD", amount=100.0, zoho_no="Z-OLD", refund_status="pending",
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 1, 10, 0)))
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="NEW", business_date="2026-07-20",
        store="ABD", amount=200.0, zoho_no="Z-NEW", refund_status="released",
        uploaded_by="tester", uploaded_at=datetime(2026, 7, 20, 10, 0)))
    session.commit()

    rows = rf.refund_records(session)
    assert list(rows["transaction_id"]) == ["NEW", "OLD"]


# ---------------------------------------------------------------------------
# queries.transaction_resolution_summary and manual_match_bill_report
# ---------------------------------------------------------------------------

def test_transaction_resolution_summary_sums_to_total(session, monkeypatch):
    frame = pd.DataFrame([
        {"gateway": "paytm", "txn_ref": "P1", "business_date": "2026-07-20",
         "store": "ABD", "amount": 100.0, "resolution": "mapped",
         "refund_status": None},
        {"gateway": "paytm", "txn_ref": "P2", "business_date": "2026-07-20",
         "store": "ABD", "amount": 50.0, "resolution": "refunded",
         "refund_status": "pending"},
        {"gateway": "paytm", "txn_ref": "P3", "business_date": "2026-07-20",
         "store": "ABD", "amount": 25.0, "resolution": "refunded",
         "refund_status": "released"},
        {"gateway": "paytm", "txn_ref": "P4", "business_date": "2026-07-20",
         "store": "ABD", "amount": 10.0, "resolution": "unmapped",
         "refund_status": None},
    ])
    monkeypatch.setattr("mop_reco.gateway_txns.gateway_transactions_with_resolution",
                       lambda *a, **kw: frame)

    from datetime import date
    out = q.transaction_resolution_summary(session, date(2026, 7, 20), date(2026, 7, 20))
    assert out.total_n == 4
    assert out.mapped_n == 1
    assert out.refunded_n == 2
    assert out.refund_pending_n == 1
    assert out.refund_released_n == 1
    assert out.unmapped_n == 1
    assert out.ok
    assert abs(out.total_v - 185.0) < 0.005


def test_transaction_resolution_summary_flags_a_mismatch(session, monkeypatch):
    # A resolution value outside the three known buckets would break the
    # three-way sum — asserted, never hidden.
    frame = pd.DataFrame([
        {"gateway": "paytm", "txn_ref": "P1", "business_date": "2026-07-20",
         "store": "ABD", "amount": 100.0, "resolution": "something_else",
         "refund_status": None},
    ])
    monkeypatch.setattr("mop_reco.gateway_txns.gateway_transactions_with_resolution",
                       lambda *a, **kw: frame)

    from datetime import date
    out = q.transaction_resolution_summary(session, date(2026, 7, 20), date(2026, 7, 20))
    assert not out.ok
    assert out.difference == 1


def test_transaction_resolution_summary_with_no_range_is_empty(session):
    out = q.transaction_resolution_summary(session, None, None)
    assert out.total_n == 0
    assert out.ok


def test_manual_match_bill_report_reads_lag_from_validation_detail(session):
    from mop_reco.db import ManualMatch
    session.add(ManualMatch(
        bill_key="2026-07-20|ABD|B1|PAYTM EDC", business_date="2026-07-20",
        store="ABD", bill_no="B1", mop="PAYTM EDC", amount=500.0,
        gateway="paytm", transaction_ref="TXN-1", status="matched_manual",
        validation_case="a", reason_code="manual_match_by_bill",
        operator="tester", created_at=datetime(2026, 7, 20, 11, 0),
        lifecycle="active",
        validation_detail=json.dumps({"date_lag_days": 2}),
    ))
    session.commit()

    report = q.manual_match_bill_report(session)
    assert len(report) == 1
    row = report.iloc[0]
    assert row["bill_no"] == "B1"
    assert row["lag"] == 2
    assert row["txn_date"] == "2026-07-22"
    assert row["status"] == "active"


def test_manual_match_bill_report_empty_selection(session):
    report = q.manual_match_bill_report(session)
    assert report.empty
    assert list(report.columns) == [
        "date", "store", "bill_no", "mop", "amount", "gateway",
        "transaction_id", "txn_date", "lag", "matched_by", "matched_at",
        "reason", "status",
    ]
