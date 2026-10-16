"""Exceptions are paged, sorted and filtered IN THE DATABASE; exports stay full-population."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import queries as q
from mop_reco.db import Exception_, R1BillLine, Run, get_engine, get_session, init_db

BILL_TYPES = q.NOT_RECEIVED_TYPES
TXN_TYPES = q.RECEIVED_NOT_BILLED_TYPES


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "p.sqlite")
    init_db(engine)
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), business_dates="[]", status="completed"))
    s.commit()
    n = 0
    for i in range(137):                       # more than two pages of 50, with amount ties
        n += 1
        s.add(Exception_(
            exception_key=f"bill-{i}", reconciliation="R1", type="bill_not_matched",
            business_date=f"2026-07-{1 + i % 28:02d}", store=f"S{i % 9}", amount=float((i % 20) * 10),
            detail=json.dumps({"bill_no": f"B{i:04d}", "mop": "PAYTM EDC"}),
            status="open" if i % 5 else "closed", first_seen_run_id=1, last_seen_run_id=1))
    for i in range(23):
        s.add(Exception_(
            exception_key=f"txn-{i}", reconciliation="R1", type="unmapped_gateway_transaction",
            business_date=f"2026-07-{1 + i % 28:02d}", store=f"S{i % 9}", amount=float(500 + i),
            detail=json.dumps({"gateway": "paytm", "transaction_id": f"T{i:05d}"}),
            status="open", first_seen_run_id=1, last_seen_run_id=1))
    for i in range(40):                        # R1BillLine facts for the unmatched bills
        s.add(R1BillLine(run_id=1, business_date=f"2026-07-{1 + i % 28:02d}", store=f"S{i % 9}",
                         bill_no=f"B{i:04d}", line_seq=1, mop="PAYTM EDC", mop_class="digital",
                         amount=1.0, matched=False, status="Not found in gateway",
                         bill_time="12:00:00", created_by="CASH1"))
    s.commit()
    return s


def all_pages(session, **kw):
    out, offset, total = [], 0, None
    while True:
        df, total, value = q.exception_page(session, None, None, None, BILL_TYPES, ("open",),
                                            limit=50, offset=offset, **kw)
        if df.empty:
            break
        out.append(df)
        offset += 50
    return pd.concat(out, ignore_index=True), total, value


def test_pages_tile_the_whole_population_without_repeats_or_gaps(session):
    paged, total, value = all_pages(session)
    whole = q.exception_rows(session, open_only=False, types=BILL_TYPES)
    whole = whole[whole["status"] == "open"]
    assert total == len(whole) == len(paged)
    assert paged["exception_key"].is_unique
    assert set(paged["exception_key"]) == set(whole["exception_key"])
    assert value == pytest.approx(float(whole["amount"].sum()))


def test_each_sort_is_done_in_the_query_and_is_total_ordered(session):
    for label in q.EXCEPTION_SORTS:
        paged, _, _ = all_pages(session, sort=label)
        if label == "Amount (high to low)":
            assert list(paged["amount"]) == sorted(paged["amount"], reverse=True)
        if label == "Amount (low to high)":
            assert list(paged["amount"]) == sorted(paged["amount"])
        if label == "Date (oldest first)":
            assert list(paged["date"]) == sorted(paged["date"])
        assert paged["exception_key"].is_unique, label            # ties never repeat or skip a row


def test_a_page_reads_only_its_own_rows(session):
    df, total, _ = q.exception_page(session, None, None, None, BILL_TYPES, ("open",), limit=10, offset=0)
    assert len(df) == 10 and total > 10


def test_status_and_type_filters_are_in_the_query(session):
    _, n_open, _ = q.exception_page(session, None, None, None, BILL_TYPES, ("open",), limit=1)
    _, n_closed, _ = q.exception_page(session, None, None, None, BILL_TYPES, ("closed",), limit=1)
    _, n_txn, _ = q.exception_page(session, None, None, None, TXN_TYPES, ("open",), limit=1)
    assert (n_open, n_closed, n_txn) == (109, 28, 23)


def test_export_is_the_full_population_not_the_page(session, monkeypatch):
    monkeypatch.setattr(q, "EXPORT_CHUNK", 40)                     # several chunks
    seen = []
    df = q.exception_export(session, None, None, None, BILL_TYPES, ("open",),
                            progress=lambda d, t: seen.append((d, t)))
    assert len(df) == 109 and df["exception_key"].is_unique
    assert seen[-1] == (109, 109) and len(seen) == 3
    paged, _, _ = all_pages(session)
    assert list(df["exception_key"]) == list(paged["exception_key"])    # same order as paging


def test_unmatched_bill_facts_can_be_limited_to_the_page(session):
    only = q.unmatched_bill_facts(session, 1, bill_nos=["B0001", "B0002"])
    assert {k[2] for k in only} == {"B0001", "B0002"}
    assert q.unmatched_bill_facts(session, 1, bill_nos=[]) == {}
    assert len(q.unmatched_bill_facts(session, 1)) == 40
