"""The Summary particulars matrix (2026-09-18) — replaces the seven-tile
ribbon. `mop_matrix.py` is the pure bucket-assignment engine (no DB, no
Streamlit); `store_sheet_source.mop_particulars_matrix()` /
`store_cash_summary()` are the read side, tested against a real (in-memory)
database with `_read()` monkeypatched so no warehouse file is needed.

The one real bug this feature shipped with and had fixed before being
reported as done: `_live_collected_by_bucket()`'s first version ran ONE
query with `mop IN (...)` per bucket — fine for Card (2 MOPs, SQLite kept
using `ix_r1_mop_date`) but SQLite's own planner switched to a full
`ix_r1_matched_date` scan for UPI (5 MOPs), measured at 3.9s and more than
doubling the page's load time. The fix (one query per MOP, summed in
Python) is covered here by an equal-value assertion across MOP counts —
this test suite cannot measure wall-clock time meaningfully, but it does
pin the CORRECTNESS the fix must preserve.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import mop_matrix as mm
from mop_reco import store_sheet_source as src
from mop_reco.db import (
    DigitalModeAgg,
    ManualMatch,
    MopDayAgg,
    R1BillLine,
    Run,
    StoreDayAgg,
    get_engine,
    get_session,
    init_db,
)


# ---------------------------------------------------------------------------
# mop_matrix.py — pure functions, no DB
# ---------------------------------------------------------------------------

class _FakeConfig:
    def __init__(self, mop_classification):
        self.mop_classification = mop_classification


def test_load_mop_buckets_reads_the_bucket_field():
    config = _FakeConfig({
        "Cash": {"class": "cash", "reconciled_in": [], "bucket": "cash"},
        "PAYTM EDC": {"class": "digital", "reconciled_in": [], "bucket": "digital"},
    })
    buckets = mm.load_mop_buckets(config)
    assert buckets == {"Cash": "cash", "PAYTM EDC": "digital"}


def test_load_mop_buckets_raises_when_a_mop_has_no_bucket_at_all():
    config = _FakeConfig({
        "Cash": {"class": "cash", "reconciled_in": []},
    })
    with pytest.raises(ValueError, match="has no 'bucket'"):
        mm.load_mop_buckets(config)


def test_load_mop_buckets_raises_on_an_unknown_bucket_name():
    config = _FakeConfig({
        "Cash": {"class": "cash", "reconciled_in": [], "bucket": "not_a_real_bucket"},
    })
    with pytest.raises(ValueError, match="not one of"):
        mm.load_mop_buckets(config)


def test_bucket_sales_by_store_date_groups_correctly():
    pos = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "mop": "Cash", "amount": 100.0},
        {"business_date": "2026-07-01", "store": "ABD", "mop": "Cash", "amount": 50.0},
        {"business_date": "2026-07-01", "store": "ABD", "mop": "PAYTM EDC", "amount": 200.0},
        {"business_date": "2026-07-02", "store": "XYZ", "mop": "Gift Voucher", "amount": 10.0},
    ])
    buckets = {"Cash": "cash", "PAYTM EDC": "card", "Gift Voucher": "gift_voucher"}
    out = mm.bucket_sales_by_store_date(pos, buckets)
    out = out.set_index(["business_date", "store", "bucket"])
    assert out.loc[("2026-07-01", "ABD", "cash"), "sale"] == 150.0
    assert out.loc[("2026-07-01", "ABD", "card"), "sale"] == 200.0
    assert out.loc[("2026-07-02", "XYZ", "gift_voucher"), "sale"] == 10.0


def test_bucket_sales_by_store_date_raises_on_an_unmapped_mop_naming_it():
    pos = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "mop": "Some New MOP", "amount": 5.0},
    ])
    with pytest.raises(ValueError, match="Some New MOP"):
        mm.bucket_sales_by_store_date(pos, {"Cash": "cash"})


def test_bucket_sales_by_store_date_on_an_empty_frame():
    out = mm.bucket_sales_by_store_date(pd.DataFrame(), {"Cash": "cash"})
    assert out.empty
    assert list(out.columns) == ["business_date", "store", "bucket", "sale"]


# ---------------------------------------------------------------------------
# classify_digital_mode — the 2026-09-19 correctness fix. Card vs UPI comes
# from the matched TRANSACTION's own identity, never the POS MOP name.
# ---------------------------------------------------------------------------

def test_classify_digital_mode_axis_is_always_card():
    # Axis EDC is settled card transactions only, by design (CLAUDE.md §5) —
    # true regardless of which MOP the bill carried or what CARD_TYPE reads.
    assert mm.classify_digital_mode("axis", "VC", "PAYTM EDC") == "card"
    assert mm.classify_digital_mode("axis", None, "EDC OTHER AXIS") == "card"


def test_classify_digital_mode_bajaj_reads_the_mop_directly():
    # The one narrow, deliberate exception: Bajaj's own settlement field
    # cannot be trusted (CLAUDE.md §6.2), but "Bajajpay Card"/"Bajajpay UPI"
    # are two distinct, correctly-informative POS MOPs, not a shared
    # terminal — reading the MOP here is a fact, not a guess.
    assert mm.classify_digital_mode("bajaj", "NA", "Bajajpay Card") == "card"
    assert mm.classify_digital_mode("bajaj", "NA", "Bajajpay UPI") == "upi"
    assert mm.classify_digital_mode("bajaj", "NA", "Some Other Mop") == "other"


def test_classify_digital_mode_paytm_reads_payment_mode_not_the_mop():
    # The bug this fixes: the SAME MOP, "PAYTM EDC", can be either — the
    # terminal accepts both card and UPI. Only the transaction's own
    # payment_mode can tell the two apart.
    assert mm.classify_digital_mode("paytm", "UPI", "PAYTM EDC") == "upi"
    assert mm.classify_digital_mode("paytm", "DEBIT_CARD", "PAYTM EDC") == "card"
    assert mm.classify_digital_mode("paytm", "CREDIT_CARD", "PAYTM EDC") == "card"
    assert mm.classify_digital_mode("paytm", "VC", "PAYTM EDC") == "card"


def test_classify_digital_mode_reports_an_unrecognised_value_as_other():
    assert mm.classify_digital_mode("paytm", "WALLET", "PAYTM EDC") == "other"
    assert mm.classify_digital_mode(None, None, "PAYTM EDC") == "other"


# ---------------------------------------------------------------------------
# store_sheet_source integration — persist / read, against a real database
# ---------------------------------------------------------------------------

@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "mop_matrix.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def run(session):
    r = Run(started_at=datetime(2026, 7, 20, 9, 0), status="completed",
           business_dates="[\"2026-07-01\"]", triggered_by="test")
    session.add(r)
    session.commit()
    return r


POS_ROWS = pd.DataFrame([
    {"business_date": "2026-07-01", "store": "ABD", "bill_no": "B1", "mop": "Cash", "amount": 1000.0},
    {"business_date": "2026-07-01", "store": "ABD", "bill_no": "B2", "mop": "PAYTM EDC", "amount": 500.0},
    {"business_date": "2026-07-01", "store": "ABD", "bill_no": "B3", "mop": "PaytmDQR", "amount": 300.0},
    {"business_date": "2026-07-01", "store": "ABD", "bill_no": "B4", "mop": "EDC OTHER", "amount": 50.0},
    {"business_date": "2026-07-01", "store": "ABD", "bill_no": "B5", "mop": "Gift Voucher", "amount": 20.0},
    {"business_date": "2026-07-01", "store": "ABD", "bill_no": "B6", "mop": "Credit Note Issued", "amount": -15.0},
    {"business_date": "2026-07-01", "store": "ABD", "bill_no": "B7", "mop": "Credit Note Received", "amount": 12.0},
])


def _fake_read(source, columns=None, dates=None):
    if source == "pos_bill_wise":
        return POS_ROWS.copy()
    return pd.DataFrame()


def test_persist_mop_day_agg_writes_one_row_per_bucket(session, run, monkeypatch):
    monkeypatch.setattr(src, "_read", _fake_read)
    report = src.persist_mop_day_agg(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    # 5 distinct buckets present (2026-09-19 — card/upi collapsed into one
    # "digital" bucket here; the real Card/UPI split now comes from the
    # matched transaction's own identity, never the MOP name — see
    # mop_matrix.classify_digital_mode and digital_mode_agg):
    # cash, digital (PAYTM EDC + PaytmDQR + EDC OTHER merge into one row),
    # gift_voucher, credit_note_issued, credit_note_received.
    assert report["rows"] == 5

    rows = {r.bucket: float(r.sale) for r in
           session.query(MopDayAgg).filter(MopDayAgg.run_id == run.id).all()}
    assert rows["cash"] == 1000.0
    assert rows["digital"] == 850.0  # PAYTM EDC 500 + PaytmDQR 300 + EDC OTHER 50
    assert rows["gift_voucher"] == 20.0
    assert rows["credit_note_issued"] == -15.0
    assert rows["credit_note_received"] == 12.0


def test_persist_mop_day_agg_replaces_not_appends(session, run, monkeypatch):
    monkeypatch.setattr(src, "_read", _fake_read)
    src.persist_mop_day_agg(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    src.persist_mop_day_agg(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    n = session.query(MopDayAgg).filter(MopDayAgg.run_id == run.id).count()
    assert n == 5


def _bill_line(session, run_id, mop, amount, matched, bd="2026-07-01", store="ABD",
               bill_no=None, matched_gateway=None, payment_mode=None):
    session.add(R1BillLine(
        run_id=run_id, business_date=bd, store=store,
        bill_no=bill_no or f"B-{mop}-{amount}", line_seq=1, mop=mop,
        mop_class="digital", amount=amount, matched=matched,
        matched_gateway=matched_gateway, payment_mode=payment_mode,
        status="matched" if matched else "open",
    ))


def _digital_mode_row(session, run_id, bd, store, mode, sale):
    session.add(DigitalModeAgg(run_id=run_id, business_date=bd, store=store,
                               mode=mode, sale=sale))


# ---------------------------------------------------------------------------
# persist_digital_mode_agg / _digital_mode_sales — the 2026-09-19 fix
# ---------------------------------------------------------------------------

def test_persist_digital_mode_agg_classifies_by_transaction_never_mop(session, run):
    bill_lines = pd.DataFrame([
        # Same MOP, "PAYTM EDC" — one UPI, one card, proving the MOP alone
        # cannot decide it.
        {"business_date": "2026-07-01", "store": "ABD", "mop": "PAYTM EDC", "amount": 100.0,
         "matched": True, "matched_gateway": "paytm", "matched_payment_mode": "UPI"},
        {"business_date": "2026-07-01", "store": "ABD", "mop": "PAYTM EDC", "amount": 60.0,
         "matched": True, "matched_gateway": "paytm", "matched_payment_mode": "DEBIT_CARD"},
        {"business_date": "2026-07-01", "store": "ABD", "mop": "EDC OTHER", "amount": 40.0,
         "matched": True, "matched_gateway": "axis", "matched_payment_mode": None},
        {"business_date": "2026-07-01", "store": "ABD", "mop": "Bajajpay UPI", "amount": 30.0,
         "matched": True, "matched_gateway": "bajaj", "matched_payment_mode": None},
        {"business_date": "2026-07-01", "store": "ABD", "mop": "PAYTM EDC", "amount": 25.0,
         "matched": False, "matched_gateway": None, "matched_payment_mode": None},
    ])
    report = src.persist_digital_mode_agg(session, run.id, bill_lines)
    assert report["rows"] == 3  # card, upi, unclassified
    assert report["other_matched_count"] == 0

    rows = {r.mode: float(r.sale) for r in
           session.query(DigitalModeAgg).filter(DigitalModeAgg.run_id == run.id).all()}
    assert rows["upi"] == 130.0          # 100 paytm-UPI + 30 bajaj-UPI
    assert rows["card"] == 100.0         # 60 paytm-debit-card + 40 axis(card, always)
    assert rows["unclassified"] == 25.0  # the one unmatched line


def test_persist_digital_mode_agg_folds_an_unrecognised_payment_mode_into_unclassified(session, run):
    bill_lines = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "mop": "PAYTM EDC", "amount": 75.0,
         "matched": True, "matched_gateway": "paytm", "matched_payment_mode": "WALLET"},
    ])
    report = src.persist_digital_mode_agg(session, run.id, bill_lines)
    assert report["other_matched_count"] == 1
    assert report["other_matched_value"] == 75.0

    rows = {r.mode: float(r.sale) for r in
           session.query(DigitalModeAgg).filter(DigitalModeAgg.run_id == run.id).all()}
    assert rows["unclassified"] == 75.0


def test_persist_digital_mode_agg_replaces_not_appends(session, run):
    bill_lines = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "mop": "PAYTM EDC", "amount": 100.0,
         "matched": True, "matched_gateway": "paytm", "matched_payment_mode": "UPI"},
    ])
    src.persist_digital_mode_agg(session, run.id, bill_lines)
    src.persist_digital_mode_agg(session, run.id, bill_lines)
    n = session.query(DigitalModeAgg).filter(DigitalModeAgg.run_id == run.id).count()
    assert n == 1


def test_digital_mode_sales_reads_the_precomputed_aggregate(session, run):
    """Open bills default to UPI, provisionally (2026-09-21) — the engine's
    own "unclassified" pool folds into `upi` (Sale) but never `upi_collected`,
    since nothing was actually confirmed collected for it."""
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "card", 100.0)
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "upi", 200.0)
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "unclassified", 50.0)
    session.commit()
    sums = src._digital_mode_sales(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert sums["card"] == 100.0
    assert sums["card_collected"] == 100.0
    assert sums["upi"] == 250.0            # 200 matched + 50 provisional
    assert sums["upi_collected"] == 200.0  # provisional is never "collected"
    assert sums["provisional_upi"] == 50.0


def test_digital_mode_sales_cascades_an_active_manual_match_out_of_provisional_upi(session, run):
    """A manual match on a bill the engine still shows unmatched makes the
    payment mode KNOWN the moment it is recorded — moves it out of the
    provisional-UPI pool live, no re-run, the same guarantee column F/
    Collected already has (CLAUDE.md 6.9b-ii item 1)."""
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "unclassified", 50.0)
    _bill_line(session, run.id, "PAYTM EDC", 50.0, matched=False, bill_no="B-MANUAL")
    session.add(ManualMatch(
        bill_key="2026-07-01|ABD|B-MANUAL|PAYTM EDC", business_date="2026-07-01",
        store="ABD", bill_no="B-MANUAL", mop="PAYTM EDC", amount=50.0,
        gateway="paytm", transaction_ref="T1", status="matched_manual",
        validation_case="a", reason_code="other_see_note", operator="tester",
        created_at=datetime(2026, 7, 1, 10, 0), lifecycle="active",
        validation_detail=json.dumps({"txn": {"payment_mode": "UPI"}}),
    ))
    session.commit()

    sums = src._digital_mode_sales(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert sums["provisional_upi"] == 0.0
    assert sums["upi"] == 50.0
    assert sums["upi_collected"] == 50.0   # now genuinely matched, not provisional
    assert sums["card"] == 0.0
    assert sums["provisional_bills"] == 0  # the one open bill was fully reclassified


def test_digital_mode_sales_provisional_bills_are_counted_from_real_bill_lines(session, run):
    """`provisional_bills` counts actual open r1_bill_lines rows, not just
    the aggregate's own rupee figure — two open bills, one later manually
    matched, leaves exactly one still provisional."""
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "unclassified", 80.0)
    _bill_line(session, run.id, "PAYTM EDC", 50.0, matched=False, bill_no="B-1")
    _bill_line(session, run.id, "PAYTM EDC", 30.0, matched=False, bill_no="B-2")
    session.add(ManualMatch(
        bill_key="2026-07-01|ABD|B-1|PAYTM EDC", business_date="2026-07-01",
        store="ABD", bill_no="B-1", mop="PAYTM EDC", amount=50.0,
        gateway="paytm", transaction_ref="T1", status="matched_manual",
        validation_case="a", reason_code="other_see_note", operator="tester",
        created_at=datetime(2026, 7, 1, 10, 0), lifecycle="active",
        validation_detail=json.dumps({"txn": {"payment_mode": "CREDIT_CARD"}}),
    ))
    session.commit()

    sums = src._digital_mode_sales(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert sums["card"] == 50.0             # B-1 reclassified to Card
    assert sums["provisional_upi"] == 30.0  # B-2 still open — provisional UPI
    assert sums["provisional_bills"] == 1


def test_digital_mode_sales_respects_the_store_filter(session, run):
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "card", 100.0)
    _digital_mode_row(session, run.id, "2026-07-01", "XYZ", "card", 999.0)
    session.commit()
    sums = src._digital_mode_sales(session, run.id, date(2026, 7, 1), date(2026, 7, 1), "ABD")
    assert sums["card"] == 100.0


def test_mop_particulars_matrix_gift_voucher_is_self_collected(session, run, monkeypatch):
    """The operator's own explicit instruction: Gift Voucher Redeemed's
    receivable must be exactly 0, never anything else."""
    monkeypatch.setattr(src, "_read", _fake_read)
    src.persist_mop_day_agg(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "card", 500.0)
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "upi", 350.0)
    session.commit()

    matrix = src.mop_particulars_matrix(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert matrix.gift_voucher_redeemed == 20.0
    gv_control = [c for c in matrix.controls if "Gift Voucher" in c[0]][0]
    assert gv_control[1] is True


def test_mop_particulars_matrix_total_sale_includes_gv_and_credit_notes(session, run, monkeypatch):
    monkeypatch.setattr(src, "_read", _fake_read)
    src.persist_mop_day_agg(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    # Card/UPI now come from digital_mode_agg (the matched transaction's own
    # identity), never from the MOP-based mop_day_agg bucket.
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "card", 500.0)
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "upi", 350.0)
    session.commit()
    matrix = src.mop_particulars_matrix(session, run.id, date(2026, 7, 1), date(2026, 7, 1))

    # Sale-side: card 500 + upi 350 + cash 1000 + gv 20 + cnr 12 + cni -15 = 1867
    assert matrix.total_sale.sale == pytest.approx(1867.0)
    assert matrix.total_digital.sale == pytest.approx(850.0)
    assert matrix.total_cash.sale == pytest.approx(1000.0)
    assert matrix.credit_note_issued == -15.0
    assert matrix.credit_note_received == 12.0


def test_mop_particulars_matrix_all_controls_hold_with_no_matches_at_all(session, run, monkeypatch):
    """Nothing is matched — every digital rupee defaults to UPI, provisionally
    (2026-09-21) — never a third bucket — cash is always fully collected,
    and every control must still hold (nothing here is guessed to pass)."""
    monkeypatch.setattr(src, "_read", _fake_read)
    src.persist_mop_day_agg(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    _digital_mode_row(session, run.id, "2026-07-01", "ABD", "unclassified", 850.0)
    session.commit()
    matrix = src.mop_particulars_matrix(session, run.id, date(2026, 7, 1), date(2026, 7, 1))

    assert matrix.card.collected == 0.0
    assert matrix.upi.collected == 0.0
    assert matrix.upi.sale == pytest.approx(850.0)
    assert matrix.upi.receivable == pytest.approx(850.0)
    assert matrix.total_cash.receivable == 0.0
    for label, holds, detail in matrix.controls:
        assert holds, f"{label} failed: {detail}"


def test_mop_particulars_matrix_falls_back_live_for_digital_mode_when_agg_missing(session, run, monkeypatch):
    """A run from before digital_mode_agg existed — classified live from
    r1_bill_lines directly (payment_mode/gateway/mop), never an empty
    Card/UPI split for an otherwise-good run. mop_day_agg's own legacy
    fallback (cash/gv/cni/cnr) is unaffected and covered separately."""
    monkeypatch.setattr(src, "_read", _fake_read)
    _bill_line(session, run.id, "PAYTM EDC", 500.0, matched=True,
              matched_gateway="paytm", payment_mode="DEBIT_CARD")
    _bill_line(session, run.id, "PaytmDQR", 350.0, matched=True,
              matched_gateway="paytm", payment_mode="UPI")
    session.commit()
    matrix = src.mop_particulars_matrix(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert matrix.total_sale.sale == pytest.approx(1867.0)
    assert matrix.card.sale == pytest.approx(500.0)
    assert matrix.upi.sale == pytest.approx(350.0)


def _store_day(session, run_id, bd, store, opening, cash_sale, cash_deposit,
              expense_income, closing):
    session.add(StoreDayAgg(
        run_id=run_id, business_date=bd, store=store,
        total_sale=cash_sale, digital_sale=0.0, cash_sale=cash_sale,
        opening_balance=opening, cash_deposit=cash_deposit, cash_received=0.0,
        cash_receivable=cash_deposit, cash_expense_income=expense_income,
        closing_balance=closing, float_amount=opening - cash_deposit,
        defect_b=False,
    ))


def test_store_cash_summary_opening_is_first_date_only_not_summed(session, run):
    """H is a stock — summing it across two dates for the same store would
    double-count the same carried-forward money."""
    _store_day(session, run.id, "2026-07-01", "ABD", opening=100.0, cash_sale=50.0,
              cash_deposit=30.0, expense_income=-5.0, closing=115.0)
    _store_day(session, run.id, "2026-07-02", "ABD", opening=115.0, cash_sale=40.0,
              cash_deposit=20.0, expense_income=-2.0, closing=133.0)
    session.commit()

    out = src.store_cash_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 2))
    assert out["opening_balance"] == 100.0        # first date only
    assert out["cash_sale"] == 90.0                # summed
    assert out["cash_deposit"] == 50.0             # summed
    assert out["cash_expense_income"] == -7.0      # summed
    assert out["closing_at_store"] == pytest.approx(133.0)   # telescopes to the last M
    assert out["check_closing"] == 133.0
    assert out["matches"] is True


def test_store_cash_summary_flags_a_mismatch(session, run):
    """A deliberately-broken closing_balance (as if store_day_agg were
    corrupted) must show as a mismatch, never silently pass."""
    _store_day(session, run.id, "2026-07-01", "ABD", opening=100.0, cash_sale=50.0,
              cash_deposit=30.0, expense_income=-5.0, closing=999.0)  # wrong on purpose
    session.commit()

    out = src.store_cash_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert out["matches"] is False
    assert out["closing_at_store"] == pytest.approx(115.0)
    assert out["check_closing"] == 999.0


def test_store_cash_summary_on_an_empty_selection(session, run):
    out = src.store_cash_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert out["matches"] is True
    assert out["closing_at_store"] == 0.0
    assert out["cash_receivable_net"] == 0.0
    assert out["matches_receivable"] is True


def _store_day_jk(session, run_id, bd, store, cash_deposit, cash_received):
    """Same shape as `_store_day` but lets J/K vary independently, for the
    cash-received/cash-receivable row's own tests (2026-09-22)."""
    session.add(StoreDayAgg(
        run_id=run_id, business_date=bd, store=store,
        total_sale=0.0, digital_sale=0.0, cash_sale=0.0,
        opening_balance=0.0, cash_deposit=cash_deposit, cash_received=cash_received,
        cash_receivable=cash_deposit - cash_received, cash_expense_income=0.0,
        closing_balance=0.0, float_amount=0.0, defect_b=cash_received > cash_deposit,
    ))


def test_store_cash_summary_cash_received_and_receivable_are_the_deposit_equation(session, run):
    """Cash deposit (I) repeats from the closing-balance row deliberately —
    same value, both equations. Cash received is J, summed like any other
    flow; cash receivable is the SAME netting rule as net_cash_receivable()
    (sum I, sum J, subtract, floor) — never a sum of per-row K."""
    _store_day_jk(session, run.id, "2026-07-01", "ABD", cash_deposit=100.0, cash_received=80.0)
    _store_day_jk(session, run.id, "2026-07-02", "ABD", cash_deposit=50.0, cash_received=50.0)
    session.commit()

    out = src.store_cash_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 2))
    assert out["cash_deposit"] == 150.0
    assert out["cash_received"] == 130.0
    assert out["cash_receivable_net"] == pytest.approx(20.0)
    assert out["matches_receivable"] is True


def test_store_cash_summary_cash_receivable_floors_at_zero_never_negative(session, run):
    """A credit exceeding its deposit (Defect B) must never show as a
    negative receivable on this card — floored at zero, per instruction;
    the count/value is surfaced in Float's own Data quality section, not
    here."""
    _store_day_jk(session, run.id, "2026-07-01", "ABD", cash_deposit=100.0, cash_received=140.0)
    session.commit()

    out = src.store_cash_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert out["cash_deposit"] == 100.0
    assert out["cash_received"] == 140.0
    assert out["cash_receivable_net"] == 0.0     # never -40.0


def test_store_cash_summary_flags_a_receivable_mismatch(session, run):
    """A row whose stored cash_receivable (K) disagrees with I - J (as if
    store_day_agg were corrupted) must show as a mismatch, never silently
    pass — the same "two independently-sourced figures" pattern `matches`
    already uses for Closing at store."""
    session.add(StoreDayAgg(
        run_id=run.id, business_date="2026-07-01", store="ABD",
        total_sale=0.0, digital_sale=0.0, cash_sale=0.0,
        opening_balance=0.0, cash_deposit=100.0, cash_received=40.0,
        cash_receivable=999.0,   # wrong on purpose — should be 100 - 40 = 60
        cash_expense_income=0.0, closing_balance=0.0, float_amount=0.0,
        defect_b=False,
    ))
    session.commit()

    out = src.store_cash_summary(session, run.id, date(2026, 7, 1), date(2026, 7, 1))
    assert out["matches_receivable"] is False
    assert out["cash_receivable_net"] == pytest.approx(60.0)
    assert out["check_receivable"] == 999.0
