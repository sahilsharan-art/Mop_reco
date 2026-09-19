"""Exception register tests — the one thing that must never lose an operator's work."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from datetime import datetime

from mop_reco.db import Exception_, Run, get_engine, get_session, init_db
from mop_reco.exceptions import (
    CandidateException,
    add_remark,
    bill_exception_key,
    remarks_for,
    set_status,
    sync_exceptions,
)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "test_reco.sqlite")
    init_db(engine)
    sess = get_session(engine)
    # Every test in this file references run_id 1/2/3 via sync_exceptions()'s
    # first_seen_run_id/last_seen_run_id, a real FOREIGN KEY to runs.id.
    # SQLite never enforced it before `foreign_keys=ON` was set on connect
    # (db.py, 2026-09-24 — the dashboard concurrency fix), so these were
    # always meant to be real Run rows; only PRAGMA enforcement was missing.
    for run_id in (1, 2, 3):
        sess.add(Run(id=run_id, started_at=datetime.now(), business_dates="[]",
                     status="completed"))
    sess.commit()
    yield sess
    sess.close()


def _candidate(bill_no="SB58/024800/0726", amount=100.0):
    return CandidateException(
        exception_key=bill_exception_key("2026-07-20", "ABD", bill_no, "PAYTM EDC"),
        reconciliation="R1",
        type="bill_not_matched",
        business_date="2026-07-20",
        store="ABD",
        amount=amount,
        detail={"bill_no": bill_no},
    )


def test_keys_are_deterministic():
    a = bill_exception_key("2026-07-20", "ABD", "B1", "PAYTM EDC")
    b = bill_exception_key("2026-07-20", "ABD", "B1", "PAYTM EDC")
    assert a == b == "2026-07-20|ABD|B1|PAYTM EDC"


def test_two_unmatched_lines_sharing_a_key_merge_instead_of_crashing(session):
    """Found 2026-09-24 on a real 5-month run: a bill with two unmatched
    lines carrying the SAME mop produces two candidates with the identical
    exception_key (the key is deliberately coarser than one bill line —
    CLAUDE.md 6.9). sync_exceptions() used to build its `existing` lookup
    once from the database and never update it as new rows were added
    within the same batch, so the second candidate also took the "not in
    existing" branch and tried to INSERT — violating the UNIQUE constraint
    on exception_key and crashing the whole run. Must merge instead: one
    row, amount SUMMED (never dropped — rule 4), both line_seqs kept."""
    key = bill_exception_key("2026-07-20", "ABD", "SB58/024800/0726", "PAYTM EDC")
    line1 = CandidateException(
        exception_key=key, reconciliation="R1", type="bill_not_matched",
        business_date="2026-07-20", store="ABD", amount=100.0,
        detail={"bill_no": "SB58/024800/0726", "line_seq": 1})
    line2 = CandidateException(
        exception_key=key, reconciliation="R1", type="bill_not_matched",
        business_date="2026-07-20", store="ABD", amount=50.0,
        detail={"bill_no": "SB58/024800/0726", "line_seq": 2})

    outcome = sync_exceptions(session, 1, [line1, line2])

    rows = session.query(Exception_).filter_by(exception_key=key).all()
    assert len(rows) == 1, "two same-key candidates must merge into one row, not crash or duplicate"
    assert rows[0].amount == 150.0, "amount must be summed, never silently dropped"
    detail = json.loads(rows[0].detail)
    assert detail["merged_line_seqs"] == [1, 2]
    assert outcome["created"] == 1


def test_rerun_finds_the_same_row_not_a_new_one(session):
    sync_exceptions(session, 1, [_candidate()])
    sync_exceptions(session, 2, [_candidate()])

    rows = session.query(Exception_).all()
    assert len(rows) == 1
    assert rows[0].first_seen_run_id == 1
    assert rows[0].last_seen_run_id == 2


def test_remarks_and_status_survive_a_full_rerun(session):
    """The core promise: re-running a restated date must not wipe the
    operator's investigation."""
    cand = _candidate()
    sync_exceptions(session, 1, [cand])

    add_remark(session, cand.exception_key, "Raised IT ticket INC-4417", "aditi")
    set_status(session, cand.exception_key, "under_investigation", "aditi")

    # A full re-run of the same date.
    sync_exceptions(session, 2, [cand])

    row = session.query(Exception_).filter_by(exception_key=cand.exception_key).one()
    assert row.status == "under_investigation"
    remarks = remarks_for(session, cand.exception_key)
    assert len(remarks) == 1
    assert remarks[0].remark_text == "Raised IT ticket INC-4417"
    assert remarks[0].created_by == "aditi"


def test_vanished_exception_is_auto_resolved_never_deleted(session):
    cand = _candidate()
    sync_exceptions(session, 1, [cand])
    add_remark(session, cand.exception_key, "chasing store", "aditi")

    # Next run: the bill is gone from the dump (IT deleted the duplicate).
    outcome = sync_exceptions(session, 2, [])

    assert outcome["auto_resolved"] == 1
    row = session.query(Exception_).filter_by(exception_key=cand.exception_key).one()
    assert row.status == "auto_resolved"
    assert "no longer present in the source data" in row.detail
    # Never deleted, and the remark is still there.
    assert len(remarks_for(session, cand.exception_key)) == 1


def test_amount_change_is_flagged_with_both_figures(session):
    sync_exceptions(session, 1, [_candidate(amount=100.0)])
    sync_exceptions(session, 2, [_candidate(amount=250.0)])

    row = session.query(Exception_).filter_by(exception_key=_candidate().exception_key).one()
    assert "amount_revised" in row.detail
    assert '"was": 100.0' in row.detail
    assert '"now": 250.0' in row.detail


def test_a_reappearing_exception_is_reopened(session):
    cand = _candidate()
    sync_exceptions(session, 1, [cand])
    sync_exceptions(session, 2, [])          # vanishes -> auto_resolved
    sync_exceptions(session, 3, [cand])      # comes back

    row = session.query(Exception_).filter_by(exception_key=cand.exception_key).one()
    assert row.status == "open"
    assert row.closed_on is None


def test_closing_records_who_and_when(session):
    cand = _candidate()
    sync_exceptions(session, 1, [cand])
    set_status(session, cand.exception_key, "closed", "aditi")

    row = session.query(Exception_).filter_by(exception_key=cand.exception_key).one()
    assert row.status == "closed"
    assert row.closed_by == "aditi"
    assert row.closed_on is not None


def test_a_closed_exception_is_not_reopened_by_a_rerun(session):
    """Closing is the operator's decision; a re-run that still sees the item
    must not silently undo it."""
    cand = _candidate()
    sync_exceptions(session, 1, [cand])
    set_status(session, cand.exception_key, "closed", "aditi")
    sync_exceptions(session, 2, [cand])

    row = session.query(Exception_).filter_by(exception_key=cand.exception_key).one()
    assert row.status == "closed"


def test_multiple_remarks_are_all_kept(session):
    cand = _candidate()
    sync_exceptions(session, 1, [cand])
    add_remark(session, cand.exception_key, "first", "aditi")
    add_remark(session, cand.exception_key, "second", "sanu")

    remarks = remarks_for(session, cand.exception_key)
    assert [r.remark_text for r in remarks] == ["first", "second"]
    assert [r.created_by for r in remarks] == ["aditi", "sanu"]
