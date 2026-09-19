"""The two risk figures must never be added together, and the reasons behind
'amount not received' must never overlap."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import queries as q
from mop_reco.db import Base, Exception_, R1BillLine, Run, get_session
from sqlalchemy import create_engine


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return get_session(engine)


def bill(run_id, bill_no, amount, matched, mop="EDC OTHER", tier=None,
         paytm_ref=None, store="ABD", day="2026-07-01"):
    return R1BillLine(
        run_id=run_id, business_date=day, store=store, bill_no=bill_no,
        line_seq=1, mop=mop, mop_class="digital", amount=amount,
        matched=matched, match_tier=tier, paytm_ref=paytm_ref,
        status="Matched" if matched else "unmatched",
    )


@pytest.fixture
def seeded(session):
    run = Run(started_at=datetime(2026, 9, 1, 10, 0), status="completed",
              business_dates="2026-07-01")
    session.add(run)
    session.flush()

    session.add_all([
        # not received: a bill raised, no payment found
        Exception_(exception_key="k1", reconciliation="R1", type="bill_not_matched",
                   business_date="2026-07-01", store="ABD", amount=1000.0, status="open",
                   first_seen_run_id=run.id, last_seen_run_id=run.id),
        Exception_(exception_key="k2", reconciliation="R1", type="ambiguous_edc_other",
                   business_date="2026-07-01", store="ABD", amount=250.0, status="open",
                   first_seen_run_id=run.id, last_seen_run_id=run.id),
        # received, not billed: money that arrived with no bill
        Exception_(exception_key="k3", reconciliation="R1",
                   type="unmapped_gateway_transaction", business_date="2026-07-01",
                   store="ABD", amount=90_000.0, status="open",
                   first_seen_run_id=run.id, last_seen_run_id=run.id),
        # neither: a reversal whose original is outside the range
        Exception_(exception_key="k4", reconciliation="R1", type="original_txn_not_found",
                   business_date="2026-07-01", store="ABD", amount=700.0, status="open",
                   first_seen_run_id=run.id, last_seen_run_id=run.id),
    ])
    session.commit()
    return run


def test_the_two_figures_are_counted_separately(session, seeded):
    split = q.risk_split(session)
    assert (split.not_received_count, split.not_received_value) == (2, 1250.0)
    assert (split.received_not_billed_count, split.received_not_billed_value) == (1, 90_000.0)


def test_what_is_neither_is_reported_not_folded_in(session, seeded):
    """A reversal with a missing original is not money lost and not money
    received — it must not be quietly added to either figure."""
    split = q.risk_split(session)
    assert (split.other_count, split.other_value) == (1, 700.0)
    assert 700.0 not in (split.not_received_value, split.received_not_billed_value)


def test_every_exception_lands_in_exactly_one_group(session, seeded):
    split = q.risk_split(session)
    assert split.total_count == 4


# --- the reason breakdown -------------------------------------------------

def test_reasons_sum_to_the_unmatched_population(session):
    run = Run(started_at=datetime(2026, 9, 1, 10, 0), status="completed",
              business_dates="2026-07-01")
    session.add(run)
    session.flush()
    session.add_all([
        bill(run.id, "B1", 100.0, False),                      # no ref, no twin
        bill(run.id, "B2", 200.0, False, tier="Ambiguous"),    # ambiguous
        bill(run.id, "B3", 300.0, False, mop="PAYTM EDC", paytm_ref="20260701X"),
        bill(run.id, "B4", 400.0, False),                      # twin of B5
        bill(run.id, "B5", 400.0, True),                       # the matched twin
    ])
    session.commit()

    df = q.unmatched_by_reason(session, run.id)
    assert df["bills"].sum() == 4
    assert df["value"].sum() == 1000.0
    by = dict(zip(df["reason"], df["bills"]))
    assert by == {
        "reference_present_txn_not_found": 1,
        "ambiguous_multiple_candidates": 1,
        "no_reference_at_pos": 1,
        "suspected_duplicate_bill": 1,
    }


def test_a_referenced_bill_is_never_classed_as_a_duplicate(session):
    """A bill carrying a live gateway reference must outrank the duplicate
    test. Otherwise a real matching defect hides inside 'probably not a loss'
    — which is exactly what happened to the 5 Paytm bills whose transaction
    exists on the following day."""
    run = Run(started_at=datetime(2026, 9, 1, 10, 0), status="completed",
              business_dates="2026-07-01")
    session.add(run)
    session.flush()
    session.add_all([
        bill(run.id, "B1", 404.0, False, mop="PAYTM EDC", paytm_ref="20260710A"),
        bill(run.id, "B2", 404.0, True, mop="PAYTM EDC"),   # a matched twin exists
    ])
    session.commit()

    df = q.unmatched_by_reason(session, run.id)
    assert list(df["reason"]) == ["reference_present_txn_not_found"]
    assert "suspected_duplicate_bill" not in set(df["reason"])


def test_a_blank_reference_string_is_not_a_reference(session):
    """Blank cells arrive as '', 'nan' or None depending on the read path."""
    run = Run(started_at=datetime(2026, 9, 1, 10, 0), status="completed",
              business_dates="2026-07-01")
    session.add(run)
    session.flush()
    session.add_all([
        bill(run.id, "B1", 100.0, False, paytm_ref=""),
        bill(run.id, "B2", 200.0, False, paytm_ref="nan"),
        bill(run.id, "B3", 300.0, False, paytm_ref=None),
    ])
    session.commit()

    df = q.unmatched_by_reason(session, run.id)
    assert list(df["reason"]) == ["no_reference_at_pos"]
    assert int(df["bills"].iloc[0]) == 3


def test_no_run_no_rows_is_an_empty_frame_not_a_crash(session):
    run = Run(started_at=datetime(2026, 9, 1, 10, 0), status="completed",
              business_dates="2026-07-01")
    session.add(run)
    session.commit()
    df = q.unmatched_by_reason(session, run.id)
    assert df.empty
    assert list(df.columns) == ["reason", "label", "bills", "value", "exposure"]


def test_every_reason_carries_a_label_and_an_exposure_note():
    for reason in q.REASON_ORDER:
        assert reason in q.REASON_LABEL
        assert reason in q.REASON_EXPOSURE
