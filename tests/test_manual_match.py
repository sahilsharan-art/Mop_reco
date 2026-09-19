"""Manual matching — the guardrails, not the happy path.

The rule that matters most is that one transaction can back at most one bill.
Assigning the same transaction twice double-counts revenue and hides a genuine
shortfall, so it is tested from several directions: the validator refuses it,
`record` refuses it without a written override, and the database refuses it
even if a code path forgets to ask.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import manual_match as mm
from mop_reco.db import (
    Base,
    Exception_,
    ManualMatch,
    ManualMatchEvent,
    R1BillLine,
    Run,
    get_session,
)
from mop_reco.exceptions import add_remark, bill_exception_key, remarks_for

REASONS = ["gateway_confirmed_by_email", "other_see_note"]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return get_session(engine)


@pytest.fixture
def run(session):
    r = Run(started_at=datetime(2026, 9, 11, 10, 0), status="completed",
            business_dates="2026-07-20")
    session.add(r)
    session.flush()
    return r


def bill_line(session, run, bill_no, amount, matched=False, store="ABD",
              mop="PAYTM EDC", matched_txn=None, day="2026-07-20"):
    line = R1BillLine(
        run_id=run.id, business_date=day, store=store, bill_no=bill_no,
        line_seq=1, mop=mop, mop_class="digital", amount=amount,
        matched=matched, matched_txn_ref=matched_txn,
        matched_gateway="paytm" if matched_txn else None,
        status="Matched" if matched else "Not found in gateway",
    )
    session.add(line)
    session.flush()
    return line


def as_bill(line) -> dict:
    return {"business_date": line.business_date, "store": line.store,
            "bill_no": line.bill_no, "mop": line.mop,
            "amount": float(line.amount)}


def pool(rows):
    """The engine's pool shape — gateway / txn_ref / business_date / store /
    amount / status / txn_type."""
    return pd.DataFrame(rows, columns=["gateway", "txn_ref", "business_date",
                                       "store", "amount", "status", "txn_type"])


CLEAN_POOL = pool([
    ("paytm", "TXN-1", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
    ("paytm", "TXN-2", "2026-07-20", "ABD", 999.0, "SUCCESS", "ACQUIRING"),
    ("paytm", "TXN-WRONG-STORE", "2026-07-20", "XYZ", 500.0, "SUCCESS", "ACQUIRING"),
    ("paytm", "TXN-WRONG-AMOUNT", "2026-07-20", "ABD", 55.0, "SUCCESS", "ACQUIRING"),
    ("paytm", "TXN-FAILED", "2026-07-20", "ABD", 500.0, "FAILURE", "ACQUIRING"),
])


# ---------------------------------------------------------------------------
# Case (a) — clean
# ---------------------------------------------------------------------------

def test_case_a_is_clean_and_needs_no_override(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    assert v.case == mm.CASE_CLEAN
    assert v.ok is True
    assert v.needs_override is False
    assert v.status == mm.STATUS_MATCHED_MANUAL
    assert v.problems == []


def test_recording_case_a_writes_the_overlay_and_an_event(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1",
                      "gateway_confirmed_by_email", "aditi", v)
    assert entry.status == mm.STATUS_MATCHED_MANUAL
    assert entry.lifecycle == mm.ACTIVE
    assert entry.override_used is False
    assert entry.bill_key == bill_exception_key("2026-07-20", "ABD", "B1", "PAYTM EDC")

    events = session.query(ManualMatchEvent).filter_by(manual_match_id=entry.id).all()
    assert [e.event for e in events] == ["created"]


def test_the_engines_own_row_is_never_edited(session, run):
    """A manual match is an overlay. r1_bill_lines is the engine's output and
    must read exactly as the engine left it."""
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note", "aditi", v)

    session.refresh(line)
    assert line.matched is False
    assert line.matched_txn_ref is None
    assert line.status == "Not found in gateway"


# ---------------------------------------------------------------------------
# Case (b) — the one that matters most
# ---------------------------------------------------------------------------

def test_case_b_already_used_by_the_engine_names_the_other_bill(session, run):
    bill_line(session, run, "OTHER", 500.0, matched=True, matched_txn="TXN-1")
    line = bill_line(session, run, "B1", 500.0)

    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    assert v.case == mm.CASE_DISCREPANT
    assert v.needs_override is True
    assert any("OTHER" in p for p in v.problems)
    assert any("cannot pay two bills" in p for p in v.problems)


def test_case_b_already_used_by_another_manual_match_names_it(session, run):
    first = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(first), "paytm", "TXN-1", CLEAN_POOL)
    mm.record(session, as_bill(first), "paytm", "TXN-1", "other_see_note", "aditi", v)

    second = bill_line(session, run, "B2", 500.0, mop="PaytmDQR")
    v2 = mm.validate(session, as_bill(second), "paytm", "TXN-1", CLEAN_POOL)
    assert v2.case == mm.CASE_DISCREPANT
    assert any("already matched manually to bill B1" in p for p in v2.problems)
    assert any("double-count revenue" in p for p in v2.problems)
    assert v2.used_by_manual_id is not None


def test_case_b_is_refused_without_a_written_override(session, run):
    bill_line(session, run, "OTHER", 500.0, matched=True, matched_txn="TXN-1")
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)

    with pytest.raises(ValueError, match="written override reason"):
        mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                  "aditi", v)
    assert session.query(ManualMatch).count() == 0


def test_case_b_with_an_override_records_the_override_and_the_reason(session, run):
    bill_line(session, run, "OTHER", 500.0, matched=True, matched_txn="TXN-1")
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)

    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v, override_reason="Paytm confirmed by email, "
                                                  "ticket 12345")
    assert entry.override_used is True
    assert "ticket 12345" in entry.override_reason
    assert entry.validation_case == mm.CASE_DISCREPANT
    # The problems the operator was shown are kept verbatim.
    assert "cannot pay two bills" in entry.validation_detail


@pytest.mark.parametrize("ref,expected", [
    ("TXN-WRONG-AMOUNT", "Amount differs"),
    ("TXN-WRONG-STORE", "Store differs"),
    ("TXN-FAILED", "status is FAILURE"),
])
def test_case_b_catches_amount_store_and_status_discrepancies(session, run, ref,
                                                              expected):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", ref, CLEAN_POOL)
    assert v.case == mm.CASE_DISCREPANT
    assert any(expected in p for p in v.problems), v.problems


def test_the_database_refuses_a_second_active_match_on_one_transaction(session, run):
    """The partial unique index is the backstop if a code path ever forgets to
    check. Belt and braces on the rule that matters most."""
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note", "aditi", v)

    session.add(ManualMatch(
        bill_key="a-different-key", business_date="2026-07-20", store="ABD",
        bill_no="B9", mop="PAYTM EDC", amount=500.0, gateway="paytm",
        transaction_ref="TXN-1", status=mm.STATUS_MATCHED_MANUAL,
        validation_case="a", reason_code="other_see_note", operator="sneaky",
        created_at=datetime.now(), lifecycle=mm.ACTIVE,
    ))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_one_bill_cannot_hold_two_active_manual_matches(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note", "aditi", v)

    v2 = mm.validate(session, as_bill(line), "paytm", "TXN-2", CLEAN_POOL)
    with pytest.raises(ValueError, match="already has an active manual match"):
        mm.record(session, as_bill(line), "paytm", "TXN-2", "other_see_note",
                  "aditi", v2)


# ---------------------------------------------------------------------------
# Case (c) — asserted, never called matched
# ---------------------------------------------------------------------------

def test_case_c_is_asserted_not_matched(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "NOT-IN-ANY-FILE", CLEAN_POOL)
    assert v.case == mm.CASE_NOT_FOUND
    assert v.status == mm.STATUS_ASSERTED
    assert v.ok is False
    assert any("cannot be called matched" in p for p in v.problems)


def test_an_assertion_is_excluded_from_every_rate(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "NOWHERE", CLEAN_POOL)
    mm.record(session, as_bill(line), "paytm", "NOWHERE", "other_see_note",
              "aditi", v)

    overlaid = mm.apply_overlay(session, pd.DataFrame([{
        "business_date": "2026-07-20", "store": "ABD", "bill_no": "B1",
        "mop": "PAYTM EDC", "amount": 500.0, "matched": False,
    }]))
    assert overlaid.iloc[0]["match_source"] == "asserted"
    assert bool(overlaid.iloc[0]["effective_matched"]) is False

    split = mm.match_rate_split(overlaid)
    assert split.asserted == 1
    assert split.manual == 0
    assert split.effective_rate == 0.0   # an assertion is NOT a match


# ---------------------------------------------------------------------------
# Both sides move together
# ---------------------------------------------------------------------------

def test_the_exception_is_closed_with_a_reason_and_remarks_survive(session, run):
    line = bill_line(session, run, "B1", 500.0)
    key = bill_exception_key("2026-07-20", "ABD", "B1", "PAYTM EDC")
    session.add(Exception_(exception_key=key, reconciliation="R1",
                           type="bill_not_matched", business_date="2026-07-20",
                           store="ABD", amount=500.0, status="open",
                           first_seen_run_id=run.id, last_seen_run_id=run.id))
    session.commit()
    add_remark(session, key, "chasing Paytm support", "aditi")

    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note", "aditi", v)

    row = session.query(Exception_).filter_by(exception_key=key).one()
    assert row.status == "closed"
    assert row.reason_code == "matched manually"
    # Rule 4: remarks are never lost.
    got = [r.remark_text for r in remarks_for(session, key)]
    assert got == ["chasing Paytm support"]


def test_a_manually_consumed_transaction_is_reported_as_consumed(session, run):
    """The engine must never also consume it on a re-run."""
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note", "aditi", v)
    assert mm.consumed_transactions(session) == {("paytm", "TXN-1")}


def test_a_reversed_match_releases_its_transaction(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v)
    mm.reverse(session, entry.id, "aditi", "wrong bill, my mistake")

    assert mm.consumed_transactions(session) == set()
    session.refresh(entry)
    assert entry.lifecycle == mm.REVERSED
    assert entry.reversed_by == "aditi"
    assert "my mistake" in entry.lifecycle_note
    # The record is kept, not deleted (rule 4), and the reversal is an event.
    assert session.query(ManualMatch).count() == 1
    assert {e.event for e in session.query(ManualMatchEvent).all()} == \
        {"created", "reversed"}


def test_a_reversal_must_carry_a_reason(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v)
    with pytest.raises(ValueError, match="reason"):
        mm.reverse(session, entry.id, "aditi", "   ")


def test_after_a_reversal_the_transaction_can_be_used_again(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v)
    mm.reverse(session, entry.id, "aditi", "wrong bill")

    other = bill_line(session, run, "B2", 500.0, mop="PaytmDQR")
    v2 = mm.validate(session, as_bill(other), "paytm", "TXN-1", CLEAN_POOL)
    assert v2.case == mm.CASE_CLEAN, v2.problems


# ---------------------------------------------------------------------------
# The rate is never blended
# ---------------------------------------------------------------------------

def test_the_split_keeps_system_manual_and_asserted_apart(session, run):
    frame = pd.DataFrame([
        {"business_date": "2026-07-20", "store": "ABD", "bill_no": "S1",
         "mop": "PAYTM EDC", "amount": 100.0, "matched": True},
        {"business_date": "2026-07-20", "store": "ABD", "bill_no": "M1",
         "mop": "PAYTM EDC", "amount": 200.0, "matched": False},
        {"business_date": "2026-07-20", "store": "ABD", "bill_no": "A1",
         "mop": "PAYTM EDC", "amount": 400.0, "matched": False},
        {"business_date": "2026-07-20", "store": "ABD", "bill_no": "U1",
         "mop": "PAYTM EDC", "amount": 800.0, "matched": False},
    ])
    for bill_no, ref, amount in (("M1", "TXN-M", 200.0), ("A1", "NOWHERE", 400.0)):
        bill = {"business_date": "2026-07-20", "store": "ABD", "bill_no": bill_no,
                "mop": "PAYTM EDC", "amount": amount}
        p = pool([("paytm", "TXN-M", "2026-07-20", "ABD", 200.0, "SUCCESS",
                   "ACQUIRING")])
        v = mm.validate(session, bill, "paytm", ref, p)
        mm.record(session, bill, "paytm", ref, "other_see_note", "aditi", v)

    split = mm.match_rate_split(mm.apply_overlay(session, frame))
    assert (split.system, split.manual, split.asserted, split.unmatched) == (1, 1, 1, 1)
    assert split.system_rate == 0.25
    assert split.effective_rate == 0.5      # system + manual, NOT asserted
    assert split.effective_rate != 0.75     # the flattering number


# ---------------------------------------------------------------------------
# Conflict handling on re-run
# ---------------------------------------------------------------------------

def test_the_engine_matching_the_same_transaction_marks_it_superseded(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v)

    line.matched = True
    line.matched_txn_ref = "TXN-1"
    session.commit()

    outcome = mm.reconcile_after_run(session, run.id)
    assert [m.id for m in outcome.superseded] == [entry.id]
    session.refresh(entry)
    assert entry.lifecycle == mm.SUPERSEDED
    assert "redundant" in entry.lifecycle_note


def test_the_engine_matching_a_different_transaction_is_a_conflict(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v)

    line.matched = True
    line.matched_txn_ref = "TXN-SOMETHING-ELSE"
    session.commit()

    outcome = mm.reconcile_after_run(session, run.id)
    assert [m.id for m in outcome.conflicted] == [entry.id]
    assert outcome.needs_attention
    session.refresh(entry)
    assert entry.lifecycle == mm.CONFLICTED
    # Neither side is overwritten — both remain readable.
    assert entry.transaction_ref == "TXN-1"
    assert line.matched_txn_ref == "TXN-SOMETHING-ELSE"
    assert "CONFLICT" in entry.lifecycle_note


def test_a_deleted_bill_orphans_the_match_and_releases_the_transaction(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v)

    session.delete(line)     # IT deleted a duplicate bill
    session.commit()

    outcome = mm.reconcile_after_run(session, run.id)
    assert [m.id for m in outcome.orphaned] == [entry.id]
    session.refresh(entry)
    assert entry.lifecycle == mm.ORPHANED
    assert mm.consumed_transactions(session) == set()   # released
    assert session.query(ManualMatch).count() == 1      # kept


def test_a_re_run_that_changes_nothing_leaves_the_overlay_alone(session, run):
    """Rule 2: re-running a date must preserve every manual match."""
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                      "aditi", v)

    outcome = mm.reconcile_after_run(session, run.id)
    assert outcome.unchanged == 1
    assert not outcome.superseded and not outcome.conflicted and not outcome.orphaned
    session.refresh(entry)
    assert entry.lifecycle == mm.ACTIVE


# ---------------------------------------------------------------------------
# Bulk upload
# ---------------------------------------------------------------------------

def test_the_template_has_exactly_the_documented_columns():
    assert list(mm.upload_template().columns) == mm.UPLOAD_COLUMNS


def test_an_upload_missing_a_column_is_refused_by_name(session, run):
    frame = pd.DataFrame([{"business_date": "2026-07-20", "store": "ABD"}])
    with pytest.raises(ValueError, match="missing column"):
        mm.preview_upload(session, frame, CLEAN_POOL, run.id)


def upload_frame(rows):
    """`rows` are (bill_no, business_date, store, transaction_id, remark) —
    the template's exact column order (2026-09-13: no mop, no reason_code;
    mop is looked up from the globally-unique bill_no, and every accepted
    row records a fixed reason code with the remark as its note)."""
    return pd.DataFrame(rows, columns=mm.UPLOAD_COLUMNS)


def test_preview_applies_nothing(session, run):
    bill_line(session, run, "B1", 500.0)
    frame = upload_frame([("B1", "2026-07-20", "ABD", "TXN-1", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.accepted) == 1
    assert session.query(ManualMatch).count() == 0    # nothing written yet


def test_a_row_whose_bill_is_not_unmatched_is_rejected(session, run):
    bill_line(session, run, "B1", 500.0, matched=True, matched_txn="TXN-9")
    frame = upload_frame([("B1", "2026-07-20", "ABD", "TXN-1", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.rejected) == 1
    assert "not an unmatched bill" in preview.rejected[0].why[0]


def test_a_row_whose_transaction_is_already_used_is_rejected_not_warned(session, run):
    bill_line(session, run, "OTHER", 500.0, matched=True, matched_txn="TXN-1")
    bill_line(session, run, "B1", 500.0)
    frame = upload_frame([("B1", "2026-07-20", "ABD", "TXN-1", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.rejected) == 1
    assert not preview.warned


def test_the_same_transaction_twice_inside_one_file_is_rejected(session, run):
    """No per-row check catches this — the double-count is between two rows of
    the same upload."""
    bill_line(session, run, "B1", 500.0)
    bill_line(session, run, "B2", 500.0, mop="PaytmDQR")
    frame = upload_frame([
        ("B1", "2026-07-20", "ABD", "TXN-1", ""),
        ("B2", "2026-07-20", "ABD", "TXN-1", ""),
    ])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.accepted) == 1
    assert len(preview.rejected) == 1
    assert "also used on row 2" in preview.rejected[0].why[0]


def test_bill_no_alone_identifies_the_bill_mop_is_looked_up_not_typed(session, run):
    """The template carries no mop column (2026-09-13) — bill_no is globally
    unique, so the bill's own mop is looked up from the system's record."""
    bill_line(session, run, "B1", 500.0, mop="PaytmDQR")
    frame = upload_frame([("B1", "2026-07-20", "ABD", "TXN-1", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.accepted) == 1
    assert preview.accepted[0].bill["mop"] == "PaytmDQR"


def test_a_typed_date_or_store_disagreeing_with_the_bill_is_rejected(session, run):
    """Rule 5: nothing is guessed. A typed business_date/store that
    disagrees with the bill's own record is surfaced, never silently
    trusted over the system's own data."""
    bill_line(session, run, "B1", 500.0, store="ABD", day="2026-07-20")
    frame = upload_frame([("B1", "2026-07-19", "XYZ", "TXN-1", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.rejected) == 1
    why = " ".join(preview.rejected[0].why)
    assert "business_date" in why and "store" in why


def test_a_blank_typed_date_and_store_is_fine(session, run):
    """The template columns are still required to exist, but a blank
    business_date/store on a row is not itself a mismatch — only a typed
    value that actively disagrees is rejected."""
    bill_line(session, run, "B1", 500.0)
    frame = upload_frame([("B1", "", "", "TXN-1", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.accepted) == 1


def test_every_bulk_uploaded_row_records_the_fixed_reason_code(session, run):
    bill_line(session, run, "B1", 500.0)
    frame = upload_frame([("B1", "2026-07-20", "ABD", "TXN-1", "confirmed by phone")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    row = preview.accepted[0]
    assert row.reason_code == mm.BULK_UPLOAD_REASON_CODE
    assert row.note == "confirmed by phone"


def test_a_discrepant_row_warns_rather_than_rejects(session, run):
    bill_line(session, run, "B1", 500.0)
    frame = upload_frame([("B1", "2026-07-20", "ABD", "TXN-WRONG-AMOUNT", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    assert len(preview.warned) == 1
    assert "Amount differs" in preview.warned[0].why[0]


def test_applying_clean_rows_only_leaves_the_warned_ones_alone(session, run):
    bill_line(session, run, "B1", 500.0)
    bill_line(session, run, "B2", 500.0, mop="PaytmDQR")
    frame = upload_frame([
        ("B1", "2026-07-20", "ABD", "TXN-1", ""),
        ("B2", "2026-07-20", "ABD", "TXN-WRONG-AMOUNT", ""),
    ])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    batch, applied, failed = mm.apply_upload(session, preview, "aditi")
    assert len(applied) == 1 and not failed
    assert applied[0].bill_no == "B1"
    assert batch.rows_submitted == 2 and batch.rows_accepted == 1


def test_applying_warned_rows_needs_a_written_override(session, run):
    bill_line(session, run, "B1", 500.0)
    frame = upload_frame([("B1", "2026-07-20", "ABD", "TXN-WRONG-AMOUNT", "")])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    with pytest.raises(ValueError, match="override reason"):
        mm.apply_upload(session, preview, "aditi", include_warnings=True)
    assert session.query(ManualMatch).count() == 0


def test_a_whole_batch_reverses_as_one_unit(session, run):
    bill_line(session, run, "B1", 500.0)
    bill_line(session, run, "B2", 999.0, mop="PaytmDQR")
    frame = upload_frame([
        ("B1", "2026-07-20", "ABD", "TXN-1", ""),
        ("B2", "2026-07-20", "ABD", "TXN-2", ""),
    ])
    preview = mm.preview_upload(session, frame, CLEAN_POOL, run.id)
    batch, applied, _ = mm.apply_upload(session, preview, "aditi")
    assert len(applied) == 2

    reversed_count = mm.reverse_batch(session, batch.id, "aditi",
                                      "wrong file uploaded")
    assert reversed_count == 2
    assert mm.consumed_transactions(session) == set()
    assert all(m.lifecycle == mm.REVERSED
               for m in session.query(ManualMatch).all())


# ---------------------------------------------------------------------------
# Audit completeness
# ---------------------------------------------------------------------------

def test_every_manual_match_records_who_when_and_on_what_evidence(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), "paytm", "TXN-1",
                      "gateway_confirmed_by_email", "aditi", v,
                      note="ticket 12345")
    for field_name in ("operator", "created_at", "bill_key", "transaction_ref",
                       "validation_case", "reason_code", "validation_detail"):
        assert getattr(entry, field_name) is not None, field_name
    assert entry.note == "ticket 12345"


def test_a_match_without_an_operator_is_refused(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    with pytest.raises(ValueError, match="who made it"):
        mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
                  "", v)


def test_a_match_without_a_reason_code_is_refused(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    with pytest.raises(ValueError, match="reason code"):
        mm.record(session, as_bill(line), "paytm", "TXN-1", "", "aditi", v)


# ---------------------------------------------------------------------------
# The gateway is worked out from the id, never asked for
# ---------------------------------------------------------------------------

MULTI_POOL = pool([
    ("paytm", "P-1", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
    ("axis", "A-1", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
    ("bajaj", "BJ-1", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
    # The same id present in two gateways — a collision, not a choice to make.
    ("paytm", "SHARED", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
    ("axis", "SHARED", "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
])


@pytest.mark.parametrize("ref,expected", [("P-1", "paytm"), ("A-1", "axis"),
                                          ("BJ-1", "bajaj")])
def test_the_gateway_is_identified_from_the_id_alone(session, ref, expected):
    hit = mm.identify_gateway(session, "2026-07-20", ref, MULTI_POOL)
    assert hit.found is True
    assert hit.gateway == expected
    assert hit.problem is None


def test_an_id_in_no_gateway_is_not_found_rather_than_guessed(session):
    hit = mm.identify_gateway(session, "2026-07-20", "NOWHERE", MULTI_POOL)
    assert hit.found is False
    assert hit.gateway is None
    assert hit.problem is None      # simply absent, not an error


def test_an_id_in_two_gateways_is_reported_not_resolved(session):
    """Picking one silently is what rule 5 forbids."""
    hit = mm.identify_gateway(session, "2026-07-20", "SHARED", MULTI_POOL)
    assert hit.found is False
    assert len(hit.candidates) == 2
    assert "more than one gateway" in hit.problem
    assert "paytm, axis" in hit.problem


def test_validate_by_id_reports_the_gateway_it_found(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "A-1", MULTI_POOL)
    assert v.gateway == "axis"
    assert v.case == mm.CASE_CLEAN


def test_validate_by_id_on_a_missing_id_is_case_c(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "NOWHERE", MULTI_POOL)
    assert v.case == mm.CASE_NOT_FOUND
    assert v.status == mm.STATUS_ASSERTED
    assert v.gateway is None
    assert "Paytm, Axis or Bajaj" in v.problems[0]


def test_validate_by_id_still_catches_an_already_used_transaction(session, run):
    """The gateway being derived must not weaken the rule that matters most."""
    bill_line(session, run, "OTHER", 500.0, matched=True, matched_txn="P-1")
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "P-1", MULTI_POOL)
    assert v.case == mm.CASE_DISCREPANT
    assert v.gateway == "paytm"
    assert any("cannot pay two bills" in p for p in v.problems)


def test_recording_takes_the_gateway_from_the_validation(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "BJ-1", MULTI_POOL)
    entry = mm.record(session, as_bill(line), None, "BJ-1", "other_see_note",
                      "aditi", v)
    assert entry.gateway == "bajaj"


def test_a_case_c_record_says_unknown_rather_than_a_gateway(session, run):
    """The register must never imply a gateway confirmed something it did not
    have — the id was found nowhere."""
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "NOWHERE", MULTI_POOL)
    entry = mm.record(session, as_bill(line), None, "NOWHERE", "other_see_note",
                      "aditi", v)
    assert entry.gateway == "unknown"
    assert entry.status == mm.STATUS_ASSERTED


# ---------------------------------------------------------------------------
# The upload template no longer asks for a gateway
# ---------------------------------------------------------------------------

def test_the_template_has_no_gateway_mop_or_reason_code_column():
    """2026-09-13, on instruction — mop and reason_code dropped too: bill_no
    alone identifies the bill (globally unique), and every bulk-uploaded row
    records a fixed reason code with a free-text remark instead."""
    assert "gateway" not in mm.UPLOAD_COLUMNS
    assert "mop" not in mm.UPLOAD_COLUMNS
    assert "reason_code" not in mm.UPLOAD_COLUMNS
    assert mm.UPLOAD_COLUMNS == ["bill_no", "business_date", "store",
                                "transaction_id", "remark"]
    assert list(mm.upload_template().columns) == mm.UPLOAD_COLUMNS


def test_an_upload_derives_the_gateway_per_row(session, run):
    bill_line(session, run, "B1", 500.0)
    bill_line(session, run, "B2", 500.0, mop="PaytmDQR")
    frame = upload_frame([
        ("B1", "2026-07-20", "ABD", "P-1", ""),
        ("B2", "2026-07-20", "ABD", "A-1", ""),
    ])
    preview = mm.preview_upload(session, frame, MULTI_POOL, run.id)
    assert len(preview.accepted) == 2
    assert {r.gateway for r in preview.accepted} == {"paytm", "axis"}

    _, applied, failed = mm.apply_upload(session, preview, "aditi")
    assert not failed
    assert {a.gateway for a in applied} == {"paytm", "axis"}


# ---------------------------------------------------------------------------
# No date restriction at all — a gateway reference is globally unique
# (2026-09-13, supersedes the ±1-day WINDOW from the day before). A window
# still missed genuine matches further out — SA12/045803/0726's transaction
# was dated a full 7 days after the bill — so the search is now the ENTIRE
# loaded table, never a window of any size.
# ---------------------------------------------------------------------------

NEXT_DAY_POOL = pool([
    ("paytm", "NEXTDAY", "2026-07-21", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
])

PREV_DAY_POOL = pool([
    ("paytm", "EARLIER", "2026-07-19", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
])

SEVEN_DAYS_LATER_POOL = pool([
    ("paytm", "FARLATER", "2026-07-27", "ABD", 500.0, "SUCCESS", "ACQUIRING"),
])


def test_a_transaction_one_day_later_is_still_accepted_as_clean(session, run):
    """Bill dated 20 July, transaction actually posted 21 July — a late
    evening sale is often posted by the gateway the following day, and
    rejecting it because the SEARCH was too narrow, not because the
    transaction is missing, was wrong."""
    line = bill_line(session, run, "B1", 500.0, day="2026-07-20")
    v = mm.validate_by_id(session, as_bill(line), "NEXTDAY", NEXT_DAY_POOL)
    assert v.case == mm.CASE_CLEAN
    assert v.ok is True
    assert v.date_lag_days == 1
    assert "21 July" in v.date_lag_note
    assert "20 July" in v.date_lag_note


def test_a_transaction_the_day_before_is_also_accepted(session, run):
    line = bill_line(session, run, "B1", 500.0, day="2026-07-20")
    v = mm.validate_by_id(session, as_bill(line), "EARLIER", PREV_DAY_POOL)
    assert v.case == mm.CASE_CLEAN
    assert v.date_lag_days == -1


def test_a_transaction_seven_days_later_is_still_found_and_accepted(session, run):
    """The case that broke the ±1-day window entirely: SA12/045803/0726's
    real transaction was dated 7 days after the bill. No window would have
    caught it; a global reference search does, unconditionally."""
    line = bill_line(session, run, "B1", 500.0, day="2026-07-20")
    v = mm.validate_by_id(session, as_bill(line), "FARLATER", SEVEN_DAYS_LATER_POOL)
    assert v.case == mm.CASE_CLEAN
    assert v.ok is True
    assert v.date_lag_days == 7
    assert "27 July" in v.date_lag_note
    assert "20 July" in v.date_lag_note


def test_same_date_has_no_lag_note_at_all(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "TXN-1", CLEAN_POOL)
    assert v.date_lag_days == 0
    assert v.date_lag_note is None


def test_absent_from_the_whole_table_is_reported_as_such(session, run):
    line = bill_line(session, run, "B1", 500.0, day="2026-07-20")
    v = mm.validate_by_id(session, as_bill(line), "NOWHERE", NEXT_DAY_POOL)
    assert v.case == mm.CASE_NOT_FOUND
    assert "not in the Paytm, Axis or Bajaj data loaded" in v.problems[0]
    assert "any date" in v.problems[0]


def test_a_bulk_upload_row_records_the_date_lag_but_still_accepts(session, run):
    bill_line(session, run, "B1", 500.0, day="2026-07-20")
    frame = upload_frame([
        ("B1", "2026-07-20", "ABD", "NEXTDAY", ""),
    ])
    preview = mm.preview_upload(session, frame, NEXT_DAY_POOL, run.id)
    assert len(preview.accepted) == 1
    row = preview.accepted[0]
    assert row.validation.date_lag_days == 1
    assert any("21 July" in w for w in row.why)
    assert preview.as_frame().iloc[0]["date_lag_days"] == 1


# ---------------------------------------------------------------------------
# A leading apostrophe (Excel's own text-forcing marker) is not part of the id
# ---------------------------------------------------------------------------

def test_a_leading_apostrophe_is_stripped_before_searching(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "'TXN-1", CLEAN_POOL)
    assert v.case == mm.CASE_CLEAN
    assert v.gateway == "paytm"


def test_recorded_transaction_ref_has_no_apostrophe(session, run):
    line = bill_line(session, run, "B1", 500.0)
    v = mm.validate_by_id(session, as_bill(line), "'TXN-1", CLEAN_POOL)
    entry = mm.record(session, as_bill(line), None, "'TXN-1", "other_see_note",
                      "aditi", v)
    assert entry.transaction_ref == "TXN-1"


# ---------------------------------------------------------------------------
# gateway_pool_for_date reads EVERY date-partitioned file, no restriction
# (2026-09-13 — renamed in spirit though not in name: `business_date` is
# accepted but no longer used to filter anything).
# ---------------------------------------------------------------------------

def test_gateway_pool_for_date_reads_every_loaded_date(tmp_path):
    root = tmp_path
    (root / "paytm_settlement").mkdir()
    for day, ref, amount in (("2026-07-19", "T-19", 100.0),
                            ("2026-07-20", "T-20", 200.0),
                            ("2026-07-21", "T-21", 300.0),
                            ("2026-08-15", "T-AUG", 400.0)):
        pd.DataFrame([{"transaction_id": ref, "amount": amount, "status": "SUCCESS",
                      "transaction_type": "ACQUIRING", "store": "ABD",
                      "payment_mode": "UPI"}]).to_parquet(
            root / "paytm_settlement" / f"{day}.parquet")

    # business_date is accepted but plays no part in the read any more.
    result = mm.gateway_pool_for_date("2026-07-20", warehouse=root)
    assert set(result["txn_ref"]) == {"T-19", "T-20", "T-21", "T-AUG"}
    assert set(result["business_date"]) == {
        "2026-07-19", "2026-07-20", "2026-07-21", "2026-08-15"}

    # Called with no date at all — same result, since date was never used.
    assert set(mm.gateway_pool_for_date(warehouse=root)["txn_ref"]) == \
        {"T-19", "T-20", "T-21", "T-AUG"}


def test_gateway_pool_for_date_on_an_empty_warehouse_is_empty(tmp_path):
    result = mm.gateway_pool_for_date(warehouse=tmp_path)
    assert result.empty


# ---------------------------------------------------------------------------
# The exact bill reported 2026-09-12, against the real run's data (read-only —
# never calls record(), so it cannot mutate the real database)
# ---------------------------------------------------------------------------

def test_the_reported_bill_and_transaction_are_accepted_on_real_data():
    """Bill SA62/024932/0726 (9 July, KGN, PAYTM EDC, Rs 1,132) was rejected
    as "not in the ... data loaded for 2026-07-09" because its transaction —
    pasted with Excel's own leading apostrophe — sits in the 10 July file,
    one day later. Originally proven fixed via manual matching alone; after
    the 2026-09-13 Tier 1 change (no date restriction at all — a gateway
    reference is globally unique) the ENGINE matches it directly on a fresh
    run, which is what this now checks — a stronger proof, since it no
    longer depends on a person pasting the id at all. Read-only: queries the
    real database, mutates nothing."""
    import mop_reco.db as db_module
    from mop_reco.db import R1BillLine, Run
    real_session = db_module.get_session(db_module.init_db())
    run = real_session.query(Run).order_by(Run.started_at.desc()).first()
    line = real_session.query(R1BillLine).filter(
        R1BillLine.run_id == run.id,
        R1BillLine.bill_no == "SA62/024932/0726").one()

    assert line.matched is True
    assert line.matched_gateway == "paytm"
    assert line.matched_txn_ref == "20260710011020000282313546973201838"
    assert line.date_diff_days == 1
    assert "1 day(s) after bill" in line.match_note

    # The manual-matching validator still finds the SAME transaction — it is
    # simply already claimed by the engine now, which is the correct refusal.
    bill = {"business_date": "2026-07-09", "store": "KGN",
           "bill_no": "SA62/024932/0726", "mop": "PAYTM EDC", "amount": 1132.0}
    result = mm.validate_by_id(real_session, bill,
                               "'20260710011020000282313546973201838")
    assert result.case == mm.CASE_DISCREPANT
    assert any("already matched by the engine" in p for p in result.problems)


# ---------------------------------------------------------------------------
# Suggested matches — no reference, same store + amount (2026-09-13)
# ---------------------------------------------------------------------------

def _no_ref_bill(session, run, bill_no, store, amount, day):
    line = R1BillLine(
        run_id=run.id, business_date=day, store=store, bill_no=bill_no,
        line_seq=1, mop="EDC OTHER", mop_class="digital_noref", amount=amount,
        matched=False, status="No reference, no match",
    )
    session.add(line)
    session.flush()
    return line


def _write_paytm_day(root, day, rows):
    (root / "paytm_settlement").mkdir(exist_ok=True)
    pd.DataFrame([{
        "transaction_id": ref, "amount": amount, "status": "SUCCESS",
        "transaction_type": "ACQUIRING", "store": store, "payment_mode": "UPI",
    } for ref, amount, store in rows]).to_parquet(
        root / "paytm_settlement" / f"{day}.parquet")


def test_suggested_matches_is_a_solvable_set_when_counts_are_equal(session, run, tmp_path):
    _no_ref_bill(session, run, "B1", "DBL", 299.0, "2026-07-02")
    _no_ref_bill(session, run, "B2", "DBL", 299.0, "2026-07-02")
    _write_paytm_day(tmp_path, "2026-07-02", [
        ("P-1", 299.0, "DBL"), ("P-2", 299.0, "DBL"),
    ])

    suggestions = mm.suggested_matches(session, run.id, date(2026, 7, 2),
                                       date(2026, 7, 2), warehouse=tmp_path)
    assert len(suggestions) == 1
    sug = suggestions[0]
    assert sug.store == "DBL"
    assert sug.amount == 299.0
    assert sug.is_solvable_set
    assert len(sug.paired) == 2
    assert {p[0]["bill_no"] for p in sug.paired} == {"B1", "B2"}
    assert {p[1]["txn_ref"] for p in sug.paired} == {"P-1", "P-2"}


def test_suggested_matches_is_ambiguous_when_counts_differ(session, run, tmp_path):
    _no_ref_bill(session, run, "B1", "SNP", 100.0, "2026-07-05")
    _write_paytm_day(tmp_path, "2026-07-05", [
        ("P-1", 100.0, "SNP"), ("P-2", 100.0, "SNP"),
    ])

    suggestions = mm.suggested_matches(session, run.id, date(2026, 7, 5),
                                       date(2026, 7, 5), warehouse=tmp_path)
    assert len(suggestions) == 1
    sug = suggestions[0]
    assert not sug.is_solvable_set
    assert sug.paired == []
    assert len(sug.bills) == 1
    assert len(sug.candidates) == 2


def test_suggested_matches_respects_the_date_window(session, run, tmp_path):
    """A candidate more than window_days away must not be suggested at all —
    time never decides a match, but it does bound the search."""
    _no_ref_bill(session, run, "B1", "GLA", 500.0, "2026-07-10")
    _write_paytm_day(tmp_path, "2026-07-14", [("P-1", 500.0, "GLA")])  # 4 days away

    suggestions = mm.suggested_matches(session, run.id, date(2026, 7, 10),
                                       date(2026, 7, 14), window_days=2,
                                       warehouse=tmp_path)
    assert suggestions == []


def test_suggested_matches_ignores_a_bill_that_carries_a_reference(session, run, tmp_path):
    session.add(R1BillLine(
        run_id=run.id, business_date="2026-07-02", store="DBL", bill_no="B1",
        line_seq=1, mop="PAYTM EDC", mop_class="digital", amount=500.0,
        matched=False, status="Not found in gateway", paytm_ref="SOME-REF",
    ))
    session.flush()
    _write_paytm_day(tmp_path, "2026-07-02", [("P-1", 500.0, "DBL")])
    suggestions = mm.suggested_matches(session, run.id, date(2026, 7, 2),
                                       date(2026, 7, 2), warehouse=tmp_path)
    assert suggestions == []


# ---------------------------------------------------------------------------
# find_bill_for_match — the reverse direction: "By transaction" mode's own
# Matched bill no column (Request B, part 2). bill_no is globally unique, so
# no date/store scope is needed to find it.
# ---------------------------------------------------------------------------

def test_find_bill_for_match_returns_an_open_bill(session, run):
    bill_line(session, run, "B1", 500.0, matched=False)
    bill, is_open, why = mm.find_bill_for_match(session, run.id, "B1")
    assert is_open
    assert why is None
    assert bill["bill_no"] == "B1"
    assert bill["amount"] == 500.0


def test_find_bill_for_match_reports_a_missing_bill_number(session, run):
    bill, is_open, why = mm.find_bill_for_match(session, run.id, "NO-SUCH-BILL")
    assert bill is None
    assert not is_open
    assert "not found" in why


def test_find_bill_for_match_reports_an_engine_matched_bill(session, run):
    bill_line(session, run, "B2", 500.0, matched=True, matched_txn="T1")
    bill, is_open, why = mm.find_bill_for_match(session, run.id, "B2")
    assert bill is not None
    assert not is_open
    assert "already matched by the engine" in why
    assert "T1" in why


def test_find_bill_for_match_reports_an_existing_active_manual_match(session, run):
    line = bill_line(session, run, "B3", 500.0, matched=False)
    validation = mm.validate(session, as_bill(line), "paytm", "TXN-1", CLEAN_POOL)
    mm.record(session, as_bill(line), "paytm", "TXN-1", "other_see_note",
             "tester", validation)

    bill, is_open, why = mm.find_bill_for_match(session, run.id, "B3")
    assert bill is not None
    assert not is_open
    assert "already matched manually" in why
    assert "TXN-1" in why


def test_find_bill_for_match_with_a_blank_bill_number(session, run):
    bill, is_open, why = mm.find_bill_for_match(session, run.id, "  ")
    assert bill is None
    assert not is_open
    assert "no bill number" in why


def test_reverse_validation_uses_the_same_validate_function(session, run):
    """The 'By transaction' mode's own paste-a-bill-number flow reuses
    manual_match.validate() directly — the identical two-sided check the
    forward direction (paste-a-transaction-id) already runs, just started
    from the bill side instead."""
    bill_line(session, run, "B4", 500.0, matched=False)
    bill, is_open, _ = mm.find_bill_for_match(session, run.id, "B4")
    assert is_open
    validation = mm.validate(session, bill, "paytm", "TXN-1", CLEAN_POOL)
    assert validation.case == mm.CASE_CLEAN
    assert validation.ok
