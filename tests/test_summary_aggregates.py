"""The Summary's precomputed aggregates must give what the live (slow) calculations gave.

Two strips used to be rebuilt from raw rows on every page load - 12.7 s (bill lines) and 144 s /
6 GB (the whole gateway pool) for five months. They now read persisted per-run aggregates and
apply the live things (manual matches, refunds) on top. Each test builds the same world twice:
once with aggregates (fast path) and once without (the original calculation), and demands the
same answer - including after a manual match and a refund, with no re-run.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import queries as q
from mop_reco import runs
from mop_reco.db import (ManualMatch, R1BillLine, Run, TransactionRefund, TxnDayAgg, get_engine,
                         get_session, init_db)

D1, D2 = date(2026, 7, 20), date(2026, 7, 21)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "a.sqlite")
    init_db(engine)
    s = get_session(engine)
    for rid in (1, 2):
        s.add(Run(id=rid, started_at=datetime(2026, 9, 1), business_dates="[]", status="completed"))
    s.commit()
    return s


def lines():
    rows = []
    for i in range(60):
        day = D1 if i % 2 else D2
        store = ["ABD", "AGT", "ALC"][i % 3]
        matched = i % 7 != 0
        rows.append(dict(
            business_date=day, store=store, bill_no=f"B{i:04d}", line_seq=1.0, mop="PAYTM EDC",
            mop_class="digital", amount=100.0 + i + (0.35 if i % 4 == 0 else 0), paytm_ref=f"R{i}",
            axis_ref=None, bajaj_ref=None, matched=matched,
            match_tier="Tier 1" if matched else None,
            matched_gateway="paytm" if matched else None,
            matched_txn_ref=f"T{i:04d}" if matched else None,
            matched_payment_mode="UPI" if matched else None,
            status="Tier 1" if matched else "Not found in gateway", date_diff_days=0, match_note=None,
            bill_time="12:00:00", created_by="C"))
    return pd.DataFrame(rows)


def load_run(session, run_id, with_results):
    df = lines()
    for day, g in df.groupby("business_date"):
        if with_results:
            runs._persist_day(session, run_id, day, g, datetime(2026, 9, 1))
        else:                                   # bill lines only - the original line-level query
            session.execute(R1BillLine.__table__.insert(), runs._bill_line_records(run_id, g))
        session.commit()


def add_manual_bill(session, bill_no):
    session.add(ManualMatch(
        bill_key=f"k-{bill_no}", business_date=str(D1), store="ABD", bill_no=bill_no, mop="PAYTM EDC",
        amount=100.0, gateway="paytm", transaction_ref=f"X-{bill_no}", status="matched_manual",
        validation_case="a", reason_code="test", lifecycle="active", operator="t", created_at=datetime(2026, 9, 2),
        validation_detail=json.dumps({"txn": {"gateway": "paytm", "txn_ref": f"X-{bill_no}",
                                              "business_date": str(D1), "store": "ABD",
                                              "amount": 77.5}})))
    session.commit()


def same_bill_summary(a, b):
    for k in ("open", "closed", "total"):
        assert a[k]["count"] == b[k]["count"], k
        assert a[k]["value"] == pytest.approx(b[k]["value"]), k


def test_bill_match_summary_from_results_equals_the_line_query(session):
    load_run(session, 1, with_results=True)
    load_run(session, 2, with_results=False)
    unmatched = [r.bill_no for r in session.query(R1BillLine).filter_by(run_id=1, matched=False)]

    same_bill_summary(q.bill_match_summary(session, 1), q.bill_match_summary(session, 2))
    same_bill_summary(q.bill_match_summary(session, 1, D1, D1, "ABD"),
                      q.bill_match_summary(session, 2, D1, D1, "ABD"))

    # a person matches a bill the engine left open: both paths close it, with no re-run
    add_manual_bill(session, unmatched[0])
    for args in ((), (D1, D2, None), (D1, D1, "ABD")):
        same_bill_summary(q.bill_match_summary(session, 1, *args), q.bill_match_summary(session, 2, *args))
    assert q.bill_match_summary(session, 1)["closed"]["count"] > 0


def test_fast_path_is_used_only_when_the_run_has_results(session):
    load_run(session, 2, with_results=False)
    assert q._bill_match_summary_from_results(session, 2, None, None, None, set()) is None
    load_run(session, 1, with_results=True)
    assert q._bill_match_summary_from_results(session, 1, None, None, None, set()) is not None


def write_pool(root):
    """Paytm files for the two days; T0001.. are the refs the bill lines matched."""
    out = root / "paytm_settlement"
    out.mkdir(parents=True)
    for day in (D1, D2):
        rows = []
        for i in range(60):
            if (D1 if i % 2 else D2) != day:
                continue
            rows.append(dict(transaction_id=f"T{i:04d}", amount=100.0 + i, status="SUCCESS",
                             transaction_type="ACQUIRING", payment_mode="UPI", original_mid="M",
                             store=["ABD", "AGT", "ALC"][i % 3], transaction_time=None))
        for j in range(5):                      # transactions no bill points at
            rows.append(dict(transaction_id=f"U{day.day}{j}", amount=10.0 * (j + 1), status="SUCCESS",
                             transaction_type="ACQUIRING", payment_mode="UPI", original_mid="M",
                             store="ABD", transaction_time=None))
        rows.append(dict(transaction_id=f"RF{day.day}", amount=5.0, status="SUCCESS",
                         transaction_type="REFUND", payment_mode="UPI", original_mid="M",
                         store="AGT", transaction_time=None))
        pd.DataFrame(rows).to_parquet(out / f"{day}.parquet", index=False)


def agg_from_pool(root, session, run_id):
    """Exactly what the engine emits: totals over every row, mapped = rows a bill consumed."""
    matched = {r.matched_txn_ref for r in session.query(R1BillLine).filter_by(run_id=run_id, matched=True)}
    rows = []
    for day in (D1, D2):
        df = pd.read_parquet(root / "paytm_settlement" / f"{day}.parquet")
        df["mapped"] = df["transaction_id"].isin(matched)
        for store, g in df.groupby("store"):
            rows.append(TxnDayAgg(run_id=run_id, business_date=str(day), store=store, gateway="paytm",
                                  total_n=len(g), total_v=float(g["amount"].sum()),
                                  mapped_n=int(g["mapped"].sum()),
                                  mapped_v=float(g.loc[g["mapped"], "amount"].sum())))
    session.add_all(rows)
    session.commit()


def same_resolution(a, b):
    for f in ("total_n", "mapped_n", "refunded_n", "refund_pending_n", "refund_released_n", "unmapped_n"):
        assert getattr(a, f) == getattr(b, f), f
    for f in ("total_v", "mapped_v", "refunded_v", "unmapped_v"):
        assert getattr(a, f) == pytest.approx(getattr(b, f)), f
    assert a.ok and b.ok


def test_transaction_resolution_from_the_aggregate_equals_the_pool_read(session, tmp_path):
    load_run(session, 1, with_results=True)
    root = tmp_path / "wh"
    write_pool(root)
    agg_from_pool(root, session, 1)

    def both(store=None):
        slow = q.transaction_resolution_summary(session, D1, D2, store, warehouse=root)
        fast = q.transaction_resolution_summary(session, D1, D2, store, run_id=1)
        same_resolution(fast, slow)
        return fast

    base = both()
    assert base.mapped_n > 0 and base.unmapped_n > 0
    both("ABD")

    # a manual match closes one unmapped transaction; a refund resolves two others - no re-run
    session.add(ManualMatch(
        bill_key="k-x", business_date=str(D1), store="ABD", bill_no="BX", mop="PAYTM EDC", amount=10.0,
        gateway="paytm", transaction_ref="U200", status="matched_manual", validation_case="a",
        reason_code="test", lifecycle="active", operator="t", created_at=datetime(2026, 9, 2),
        validation_detail=json.dumps({"txn": {"gateway": "paytm", "txn_ref": "U200",
                                              "business_date": str(D1), "store": "ABD", "amount": 10.0}})))
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="U211", business_date=str(D2), store="ABD", amount=20.0,
        zoho_no="Z1", refund_status="pending", uploaded_by="t", uploaded_at=datetime(2026, 9, 2)))
    session.add(TransactionRefund(
        gateway="paytm", transaction_ref="U212", business_date=str(D2), store="ABD", amount=30.0,
        zoho_no="Z2", refund_status="released", uploaded_by="t", uploaded_at=datetime(2026, 9, 2)))
    session.commit()
    after = both()
    assert after.mapped_n == base.mapped_n + 1
    assert after.refunded_n == 2 and after.refund_pending_n == 1 and after.refund_released_n == 1
    assert after.unmapped_n == base.unmapped_n - 3
    both("ABD")
    both("AGT")


def test_a_run_without_an_aggregate_falls_back_to_the_pool_read(session, monkeypatch):
    frame = pd.DataFrame([{"gateway": "paytm", "txn_ref": "P1", "business_date": "2026-07-20",
                           "store": "ABD", "amount": 100.0, "resolution": "mapped",
                           "refund_status": None}])
    monkeypatch.setattr("mop_reco.gateway_txns.gateway_transactions_with_resolution",
                        lambda *a, **kw: frame)
    out = q.transaction_resolution_summary(session, D1, D1, None, run_id=1)    # no TxnDayAgg rows
    assert out.total_n == 1 and out.mapped_n == 1
