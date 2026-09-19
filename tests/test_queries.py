"""Tests for the read side — every number a view shows comes from here."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import queries as q
from mop_reco.db import (
    Exception_,
    ManualMatch,
    R1BillLine,
    ReconciliationResult,
    Remark,
    Run,
    get_engine,
    get_session,
    init_db,
)
from mop_reco.exceptions import (
    SEVERITY_LABEL,
    date_from_paytm_txn_id,
    severity_for,
    type_label,
)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "q.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def seeded(session):
    """A small, fully-known run so every asserted figure is hand-checkable."""
    run = Run(started_at=datetime(2026, 9, 1, 10, 0), finished_at=datetime(2026, 9, 1, 10, 5),
              business_dates=json.dumps(["2026-07-01", "2026-07-02"]), status="completed",
              triggered_by="test",
              notes=json.dumps({
                  "r1": {"digital_bill_lines": 4, "matched_total": 2},
                  "timings": {"ingest_pos_bill_wise": 300.0, "r1_match": 100.0},
                  "load": {"pos_bill_wise": {"status": "ok", "rows_read": 10,
                                             "control_total": 1234567.0}},
              }))
    session.add(run)
    session.commit()

    lines = [
        # 2026-07-01: 2 lines, 1 matched (600 of 1000 matched)
        ("2026-07-01", "ABD", "B1", 600.0, True, "Tier 1"),
        ("2026-07-01", "ABD", "B2", 400.0, False, None),
        # 2026-07-02: 2 lines, 1 matched
        ("2026-07-02", "XYZ", "B3", 900.0, True, "Tier 1"),
        ("2026-07-02", "XYZ", "B4", 100.0, False, None),
    ]
    for bd, store, bill, amount, matched, tier in lines:
        session.add(R1BillLine(
            run_id=run.id, business_date=bd, store=store, bill_no=bill, line_seq=1,
            mop="PAYTM EDC", mop_class="digital", amount=amount,
            matched=matched, match_tier=tier,
            status=tier or "Not found in gateway",
        ))

    session.add_all([
        Exception_(exception_key="k1", reconciliation="R1", type="unmapped_gateway_transaction",
                   business_date="2026-07-01", store="ABD", amount=250_000.0,
                   detail=json.dumps({"transaction_id": "T1"}), status="open",
                   first_seen_run_id=run.id, last_seen_run_id=run.id),
        Exception_(exception_key="k2", reconciliation="R1", type="bill_not_matched",
                   business_date="2026-07-02", store="XYZ", amount=400.0,
                   detail=json.dumps({"bill_no": "B2"}), status="open",
                   first_seen_run_id=run.id, last_seen_run_id=run.id),
        Exception_(exception_key="k3", reconciliation="R1", type="bill_not_matched",
                   business_date="2026-07-02", store="ABD", amount=100.0,
                   detail=json.dumps({"bill_no": "B4"}), status="closed",
                   first_seen_run_id=run.id, last_seen_run_id=run.id),
    ])
    session.commit()
    return run


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def test_verdict_counts_and_values(session, seeded):
    v = q.verdict(session, seeded.id)
    assert v.digital_lines == 4
    assert v.matched == 2
    assert v.unmatched == 2
    assert v.digital_value == 2000.0
    assert v.matched_value == 1500.0
    assert v.unmatched_value == 500.0
    assert v.match_rate == 0.5


def test_verdict_open_exceptions_exclude_closed(session, seeded):
    """A closed item is not an open exception and its value is not 'at risk'."""
    v = q.verdict(session, seeded.id)
    assert v.open_exceptions == 2          # k3 is closed
    assert v.open_exception_value == 250_400.0


def test_verdict_separates_unmapped_gateway_value(session, seeded):
    v = q.verdict(session, seeded.id)
    assert v.unmapped_txn_count == 1
    assert v.unmapped_txn_value == 250_000.0


def test_verdict_respects_the_date_filter(session, seeded):
    v = q.verdict(session, seeded.id, date(2026, 7, 1), date(2026, 7, 1))
    assert v.digital_lines == 2
    assert v.digital_value == 1000.0


def test_verdict_respects_the_store_filter(session, seeded):
    v = q.verdict(session, seeded.id, store="XYZ")
    assert v.digital_lines == 2
    assert v.digital_value == 1000.0


def test_verdict_cascades_a_manual_match_immediately(session, seeded):
    """B2 (ABD, 400.0) is engine-unmatched. A manual match for it must move
    it from unmatched to matched on THIS read — no re-run required
    (2026-09-13). Bounded: only queries the small set of active manual
    matches, never re-scans the month's bill lines."""
    before = q.verdict(session, seeded.id)
    assert before.matched == 2
    assert before.unmatched == 2

    session.add(ManualMatch(
        bill_key="2026-07-01|ABD|B2|PAYTM EDC", business_date="2026-07-01",
        store="ABD", bill_no="B2", mop="PAYTM EDC", amount=400.0,
        gateway="paytm", transaction_ref="T-B2", status="matched_manual",
        validation_case="a", reason_code="other_see_note", operator="aditi",
        created_at=datetime(2026, 9, 13, 10, 0), lifecycle="active",
    ))
    session.commit()

    after = q.verdict(session, seeded.id)
    assert after.matched == 3
    assert after.unmatched == 1
    assert after.matched_value == 1900.0
    assert after.unmatched_value == 100.0
    # A reversed/superseded/orphaned/conflicted match must NOT count — only
    # 'active' moves a figure (CLAUDE.md §6.9b).
    assert before.digital_lines == after.digital_lines == 4


def test_not_received_by_store_cascades_a_manual_match_immediately(session, seeded):
    """The same cascade for the 'amount not received by store' cut (2026-09-13)
    — B2 must drop out of ABD's not-received total the moment it is matched
    manually, matching verdict()'s own behaviour."""
    before = q.not_received_by_store(session, seeded.id)
    abd_before = before[before["store"] == "ABD"].iloc[0]
    assert abd_before["bills"] == 1
    assert abd_before["not_received"] == 400.0

    session.add(ManualMatch(
        bill_key="2026-07-01|ABD|B2|PAYTM EDC", business_date="2026-07-01",
        store="ABD", bill_no="B2", mop="PAYTM EDC", amount=400.0,
        gateway="paytm", transaction_ref="T-B2", status="matched_manual",
        validation_case="a", reason_code="other_see_note", operator="aditi",
        created_at=datetime(2026, 9, 13, 10, 0), lifecycle="active",
    ))
    session.commit()

    after = q.not_received_by_store(session, seeded.id)
    assert after[after["store"] == "ABD"].empty

    bills_after = q.not_received_bills(session, seeded.id, "ABD")
    assert bills_after.empty


def test_verdict_sentence_is_plain_words_with_indian_units(session, seeded):
    v = q.verdict(session, seeded.id)
    sentence = q.verdict_sentence(v, "1-2 July")
    assert sentence.startswith("1-2 July: 50.00% matched.")
    assert "Rs 500 not received" in sentence      # money we did NOT get
    assert "Rs 2.50 L" in sentence                # money we DID get, unbilled
    assert "no bill behind" in sentence


def test_verdict_sentence_never_sums_the_two_opposite_figures(session, seeded):
    """Rs 500 not received and Rs 2.50 L received-but-unbilled point in
    opposite directions. A combined 'at risk' total means nothing, and the
    sentence must not present one."""
    v = q.verdict(session, seeded.id)
    sentence = q.verdict_sentence(v, "1-2 July")
    # Not a string test on the sum — the short form rounds, so Rs 500 + Rs 2.50 L
    # renders as "Rs 2.50 L" too. Test the claim instead: no combined "at risk"
    # figure, and each amount carries a label saying which direction it points.
    assert "at risk" not in sentence
    assert "not received" in sentence
    assert "no bill behind" in sentence
    assert v.open_exception_value > v.unmatched_value  # the sum exists...
    assert "open exceptions" not in sentence           # ...but is never shown


def test_verdict_sentence_says_clean_when_it_is(session):
    v = q.Verdict(digital_lines=10, matched=10, unmatched=0, digital_value=5_000_000.0)
    sentence = q.verdict_sentence(v, "1-31 July")
    assert "Nothing open" in sentence


# ---------------------------------------------------------------------------
# Where the money is
# ---------------------------------------------------------------------------

def test_top_stores_ranks_on_amount_not_received_only(session, seeded):
    """ABD's 2.5 L is an unmapped gateway transaction — money RECEIVED and not
    billed. Ranking it as exposure would let a store top the chart on
    collections it merely failed to record, so the default excludes it."""
    df = q.top_stores_by_exception_value(session)
    assert list(df["store"]) == ["XYZ"]
    assert df.iloc[0]["value"] == 400.0


def test_top_stores_can_still_be_asked_for_another_group(session, seeded):
    df = q.top_stores_by_exception_value(
        session, types=q.RECEIVED_NOT_BILLED_TYPES)
    assert list(df["store"]) == ["ABD"]
    assert df.iloc[0]["value"] == 250_000.0


def test_top_stores_ranked_by_value_not_count(session, seeded):
    """Ordering is by value, never by how many exceptions a store has."""
    df = q.top_stores_by_exception_value(
        session, types=q.NOT_RECEIVED_TYPES + q.RECEIVED_NOT_BILLED_TYPES)
    assert list(df["store"]) == ["ABD", "XYZ"]
    assert df.iloc[0]["value"] == 250_000.0


def test_exceptions_by_type_biggest_value_first(session, seeded):
    df = q.exceptions_by_type(session)
    assert list(df["type"]) == ["unmapped_gateway_transaction", "bill_not_matched"]
    assert df.iloc[0]["value"] == 250_000.0


def test_daily_trend_one_row_per_date_with_rate(session, seeded):
    df = q.daily_trend(session, seeded.id)
    assert list(df["business_date"]) == ["2026-07-01", "2026-07-02"]
    assert df.iloc[0]["match_rate"] == 50.0
    assert df.iloc[0]["unmatched_value"] == 400.0


# ---------------------------------------------------------------------------
# The exception list
# ---------------------------------------------------------------------------

def test_exception_rows_sorted_by_value_descending(session, seeded):
    df = q.exception_rows(session)
    assert df.iloc[0]["amount"] == 250_000.0
    assert list(df["amount"]) == sorted(df["amount"], reverse=True)


def test_exception_rows_show_dates_not_run_numbers(session, seeded):
    """first_seen / last_seen must be dates — they used to read '1'."""
    df = q.exception_rows(session)
    assert df.iloc[0]["first_seen"] == "2026-09-01"
    assert df.iloc[0]["last_seen"] == "2026-09-01"
    assert df.iloc[0]["age_days"] >= 0


def test_exception_rows_carry_a_readable_reference_not_the_key(session, seeded):
    df = q.exception_rows(session)
    assert df.iloc[0]["reference"] == "T1"          # from detail, not the composite key
    assert "exception_key" in df.columns            # identity still available underneath


def test_exception_rows_open_only_by_default(session, seeded):
    assert len(q.exception_rows(session)) == 2
    assert len(q.exception_rows(session, open_only=False)) == 3


def test_exception_rows_search_finds_by_reference(session, seeded):
    df = q.exception_rows(session, search="T1")
    assert len(df) == 1
    assert df.iloc[0]["reference"] == "T1"


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def test_transaction_page_defaults_can_filter_to_unmatched(session, seeded):
    df, total = q.transaction_page(session, seeded.id, matched=False)
    assert total == 2
    assert set(df["bill_no"]) == {"B2", "B4"}


def test_transaction_page_reports_total_for_showing_x_of_n(session, seeded):
    df, total = q.transaction_page(session, seeded.id, limit=1, offset=0)
    assert total == 4
    assert len(df) == 1


def test_transaction_search_finds_a_bill_without_paging(session, seeded):
    df, total = q.transaction_page(session, seeded.id, search="B3")
    assert total == 1
    assert df.iloc[0]["bill_no"] == "B3"


# ---------------------------------------------------------------------------
# Severity and labels
# ---------------------------------------------------------------------------

def test_severity_type_first_then_value_escalates():
    # Gateway money with no bill is serious regardless of size.
    assert severity_for("unmapped_gateway_transaction", 50) == "action"
    # A small ambiguous item is only worth a review...
    assert severity_for("ambiguous_edc_other", 50) == "review"
    # ...but a large one escalates.
    assert severity_for("ambiguous_edc_other", 250_000) == "action"
    assert severity_for("ambiguous_edc_other", 20_000) == "attention"


def test_every_severity_has_a_word_not_only_a_colour():
    for level in ("action", "attention", "review"):
        assert SEVERITY_LABEL[level]


def test_type_labels_are_readable_english():
    assert type_label("unmapped_gateway_transaction") == "Gateway payment with no bill"
    assert "_" not in type_label("bill_not_matched")


# ---------------------------------------------------------------------------
# The two-dates fix for reversals
# ---------------------------------------------------------------------------

def test_paytm_id_encodes_its_date():
    assert date_from_paytm_txn_id("20260523010890000265069854857039128") == date(2026, 5, 23)
    assert date_from_paytm_txn_id("not-an-id") is None
    assert date_from_paytm_txn_id(None) is None


def test_linkage_detail_carries_both_dates_and_flags_disagreement():
    """A July reversal against a May sale must show BOTH dates, or it reads as
    May data leaking into a July run."""
    from mop_reco.exceptions import _linkage_detail

    row = pd.Series({
        "transaction_type": "CHARGEBACK_REVERSAL",
        "transaction_id": "20260716010341284543248332861440",   # id says July
        "reference_transaction_id": "20260523010890000265069854857039128",
        "business_date": date(2026, 5, 28),                      # field says May
    })
    detail = _linkage_detail(row)

    assert detail["reversal_date"] == "2026-05-28"
    assert detail["reversal_date_from_id"] == "2026-07-16"
    assert detail["original_transaction_date"] == "2026-05-23"
    assert detail["age_of_original_days"] == 54
    assert "disagrees with itself" in detail["date_disagreement"]


def test_linkage_detail_without_disagreement_has_no_flag():
    from mop_reco.exceptions import _linkage_detail

    row = pd.Series({
        "transaction_type": "REFUND",
        "transaction_id": "20260729021082893667277462732814681",
        "reference_transaction_id": "20260501010970000256962461686004681",
        "business_date": date(2026, 7, 29),
    })
    detail = _linkage_detail(row)
    assert "date_disagreement" not in detail
    assert detail["original_transaction_date"] == "2026-05-01"


# ---------------------------------------------------------------------------
# matched_bills_page / matched_bills_export — Summary's Matched section
# (2026-09-19)
# ---------------------------------------------------------------------------

@pytest.fixture
def matched_run(session):
    run = Run(started_at=datetime(2026, 9, 19, 9, 0), status="completed",
              business_dates=json.dumps(["2026-07-09", "2026-07-10"]))
    session.add(run)
    session.flush()

    # Tier 1, same-date — lag 0.
    session.add(R1BillLine(
        run_id=run.id, business_date="2026-07-09", store="KGN",
        bill_no="SA62/024925/0726", line_seq=1, mop="PAYTM EDC",
        mop_class="digital", amount=299.0, matched=True, match_tier="Tier 1",
        matched_gateway="paytm", matched_txn_ref="TXN-SAME-DATE",
        date_diff_days=0, status="matched",
    ))
    # Tier 1, cross-date — the four-bill incident: txn dated 1 day after bill.
    session.add(R1BillLine(
        run_id=run.id, business_date="2026-07-09", store="KGN",
        bill_no="SA62/024926/0726", line_seq=1, mop="PAYTM EDC",
        mop_class="digital", amount=649.0, matched=True, match_tier="Tier 1",
        matched_gateway="paytm", matched_txn_ref="TXN-NEXT-DAY",
        date_diff_days=1, status="matched",
    ))
    # Tier 2 (EDC OTHER) — same-date by construction.
    session.add(R1BillLine(
        run_id=run.id, business_date="2026-07-10", store="RBL",
        bill_no="SB10/000001/0726", line_seq=1, mop="EDC OTHER",
        mop_class="digital_noref", amount=404.0, matched=True,
        match_tier="Matched (EDC OTHER)", matched_gateway="axis",
        matched_txn_ref="TXN-TIER2", date_diff_days=0, status="matched",
    ))
    # An open bill closed only by a manual match — engine matched stays False.
    session.add(R1BillLine(
        run_id=run.id, business_date="2026-07-10", store="RBL",
        bill_no="SA12/045803/0726", line_seq=1, mop="PAYTM EDC",
        mop_class="digital", amount=59.0, matched=False, status="Not found",
    ))
    session.flush()

    session.add(ManualMatch(
        bill_key="business_date=2026-07-10|store=RBL|bill_no=SA12/045803/0726|mop=PAYTM EDC",
        business_date="2026-07-10", store="RBL", bill_no="SA12/045803/0726",
        mop="PAYTM EDC", amount=59.0, gateway="paytm",
        transaction_ref="TXN-MANUAL", status="matched_manual",
        validation_case="a", reason_code="gateway_confirmed_by_email",
        operator="aditi", created_at=datetime(2026, 9, 19, 9, 5),
        lifecycle="active",
        validation_detail=json.dumps({"date_lag_days": 7}),
    ))
    session.commit()
    return run


def test_matched_bills_page_carries_tier_and_lag_correctly(session, matched_run):
    df, total = q.matched_bills_page(session, matched_run.id)
    assert total == 4
    by_bill = df.set_index("bill_no")

    same_date = by_bill.loc["SA62/024925/0726"]
    assert same_date["match_source"] == "tier1"
    assert same_date["lag"] == 0
    assert same_date["txn_date"] == "2026-07-09"

    next_day = by_bill.loc["SA62/024926/0726"]
    assert next_day["match_source"] == "tier1"
    assert next_day["lag"] == 1
    assert next_day["txn_date"] == "2026-07-10"
    assert next_day["gateway"] == "paytm"
    assert next_day["transaction_id"] == "TXN-NEXT-DAY"

    tier2 = by_bill.loc["SB10/000001/0726"]
    assert tier2["match_source"] == "tier2"
    assert tier2["lag"] == 0


def test_matched_bills_page_reads_a_manual_matchs_own_date_lag(session, matched_run):
    """A manually-matched bill's lag comes from its own validation_detail —
    never assumed to be 0, which rule 5 forbids guessing."""
    df, _ = q.matched_bills_page(session, matched_run.id)
    manual_row = df.set_index("bill_no").loc["SA12/045803/0726"]
    assert manual_row["match_source"] == "manual"
    assert manual_row["gateway"] == "paytm"
    assert manual_row["transaction_id"] == "TXN-MANUAL"
    assert manual_row["lag"] == 7
    assert manual_row["txn_date"] == "2026-07-17"


def test_matched_bills_page_is_paged_and_ordered_by_date_then_store(session, matched_run):
    df, total = q.matched_bills_page(session, matched_run.id, limit=2, offset=0)
    assert total == 4
    assert len(df) == 2
    assert list(df["date"]) == sorted(df["date"])


def test_matched_bills_export_returns_every_row_not_a_page(session, matched_run):
    df = q.matched_bills_export(session, matched_run.id)
    assert len(df) == 4
    assert set(df["bill_no"]) == {"SA62/024925/0726", "SA62/024926/0726",
                                  "SB10/000001/0726", "SA12/045803/0726"}


def test_matched_bills_export_is_empty_not_a_crash_when_nothing_matched(session):
    run = Run(started_at=datetime(2026, 9, 19, 9, 0), status="completed",
              business_dates=json.dumps(["2026-07-01"]))
    session.add(run)
    session.commit()
    df = q.matched_bills_export(session, run.id)
    assert df.empty
    assert list(df.columns) == ["date", "store", "bill_no", "mop", "amount",
                                "gateway", "transaction_id", "txn_date",
                                "lag", "match_source"]


# ---------------------------------------------------------------------------
# exception_counts — the cheap header aggregate for Summary's collapsed
# Exceptions section (2026-09-19)
# ---------------------------------------------------------------------------

def test_exception_counts_matches_the_full_exception_rows_breakdown(session, seeded):
    """The cheap aggregate must agree with the full per-row read — it is a
    faster path to the same answer, not a different one."""
    bill_types = ("bill_not_matched", "ambiguous_edc_other", "no_source_report")
    txn_types = ("unmapped_gateway_transaction",)

    bill_n, bill_v, txn_n, txn_v = q.exception_counts(
        session, None, None, None, bill_types, txn_types, statuses=("open",))
    assert (bill_n, bill_v) == (1, 400.0)     # k2 only — k3 is closed
    assert (txn_n, txn_v) == (1, 250_000.0)   # k1

    full = q.exception_rows(session, None, None, None, open_only=False,
                            types=bill_types + txn_types)
    full_open = full[full["status"] == "open"]
    assert len(full_open[full_open["type"].isin(bill_types)]) == bill_n
    assert len(full_open[full_open["type"].isin(txn_types)]) == txn_n


def test_exception_counts_respects_status_scope(session, seeded):
    bill_types = ("bill_not_matched", "ambiguous_edc_other", "no_source_report")
    bill_n, bill_v, _, _ = q.exception_counts(
        session, None, None, None, bill_types, (), statuses=("closed", "auto_resolved"))
    assert (bill_n, bill_v) == (1, 100.0)     # k3 only


def test_exception_counts_on_no_matches_is_zero_not_a_crash(session, seeded):
    bill_n, bill_v, txn_n, txn_v = q.exception_counts(
        session, None, None, None, ("store_ambiguous",), ("original_txn_not_found",))
    assert (bill_n, bill_v, txn_n, txn_v) == (0, 0.0, 0, 0.0)


# ---------------------------------------------------------------------------
# Run page inputs
# ---------------------------------------------------------------------------

def test_run_history_and_timings(session, seeded):
    hist = q.run_history(session)
    assert len(hist) == 1
    assert hist.iloc[0]["status"] == "completed"
    assert hist.iloc[0]["duration_s"] == 300.0

    timings = q.run_timings(seeded)
    assert list(timings["stage"])[0] == "ingest_pos_bill_wise"
    assert timings.iloc[0]["share"] == pytest.approx(75.0)


def test_control_totals_read_from_the_run(session, seeded):
    ct = q.control_totals(seeded)
    assert ct.iloc[0]["source"] == "pos_bill_wise"
    assert ct.iloc[0]["rows_read"] == 10


# ---------------------------------------------------------------------------
# latest_remarks — bulk lookup for Summary's merged Exceptions section
# ---------------------------------------------------------------------------

def test_latest_remarks_returns_the_most_recent_remark_per_key(session, seeded):
    exc = session.query(Exception_).filter_by(exception_key="k1").one()
    session.add_all([
        Remark(exception_id=exc.id, remark_text="First", created_by="aditi",
              created_at=datetime(2026, 9, 10, 9, 0)),
        Remark(exception_id=exc.id, remark_text="Corrected", created_by="raj",
              created_at=datetime(2026, 9, 10, 10, 0)),
    ])
    session.commit()

    found = q.latest_remarks(session, ["k1", "k2"])
    assert found["k1"] == ("Corrected", "raj")
    assert "k2" not in found      # no remark recorded — simply absent


def test_latest_remarks_on_no_keys_or_no_remarks_is_empty(session, seeded):
    assert q.latest_remarks(session, []) == {}
    assert q.latest_remarks(session, ["k1", "k2"]) == {}
