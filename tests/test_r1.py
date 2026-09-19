"""R1 tests — POS bill ↔ gateway transaction.

Pinned to the 20-Jul-2026 daily files, which is where the acceptance figures
in CLAUDE.md section 7 come from.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import mop_reco.ingest as ingest_module
from mop_reco.config import load_config
from mop_reco.db import get_engine, get_session, init_db
from mop_reco.ingest import ingest_axis_edc, ingest_paytm, ingest_pos_bill_wise
from mop_reco.r1_bill_txn import (
    build_gateway_pool,
    link_non_acquiring,
    match_tier1,
    match_tier2_edc_other,
    run_r1,
    successful_acquiring,
)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "test_reco.sqlite")
    init_db(engine)
    sess = get_session(engine)
    yield sess
    sess.close()


@pytest.fixture
def config():
    return load_config()


@pytest.fixture(autouse=True)
def isolated_warehouse(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_module, "WAREHOUSE_ROOT", tmp_path / "warehouse")
    yield


@pytest.fixture
def r1_20jul(config, session):
    """One R1 run over the 20-Jul daily files, shared by the assertions below."""
    root_paytm = config.sources["paytm_settlement"]
    bills, _ = ingest_pos_bill_wise(
        config, session, file_path=config.sources["pos_bill_wise"] / "MOP SALE DATA (20.07.2026).xlsx")
    paytm, _ = ingest_paytm(
        config, session,
        file_paths=[p for p in config.source_status("paytm_settlement").files
                    if p.parent == root_paytm and p.suffix.lower() == ".zip"])
    axis, _ = ingest_axis_edc(
        config, session, file_path=config.sources["axis_edc"] / "Axis_20th_July'26.xlsx")
    return run_r1(bills, paytm, axis, None), bills, paytm, axis


# ---------------------------------------------------------------------------
# The CLAUDE.md section 7 acceptance figures
# ---------------------------------------------------------------------------

def test_digital_bill_universe(r1_20jul):
    result, *_ = r1_20jul
    assert result.summary["digital_bill_lines"] == 17536


def test_tier1_reference_match(r1_20jul):
    result, *_ = r1_20jul
    assert result.summary["tier1_matched"] == 17492


def test_tier2_edc_other_on_residual(r1_20jul):
    """38 matched, 0 ambiguous — restricting to the Tier-1 residual is what
    removes the ambiguity that 1,664 colliding store+date+amount groups would
    otherwise cause."""
    result, *_ = r1_20jul
    assert result.summary["tier2_matched"] == 38
    assert result.summary["ambiguous"] == 0


def test_six_bills_left_open(r1_20jul):
    result, *_ = r1_20jul
    assert result.summary["open_exceptions"] == 6


def test_unmapped_before_tier2_matches_acceptance_figures(config, session):
    """Paytm 57 and Axis 2 before Tier 2 — both exact. (What happens to those
    59 afterwards is where the published figures and the arithmetic diverge;
    see test_unmapped_after_tier2_is_internally_consistent.)"""
    root_paytm = config.sources["paytm_settlement"]
    bills, _ = ingest_pos_bill_wise(
        config, session, file_path=config.sources["pos_bill_wise"] / "MOP SALE DATA (20.07.2026).xlsx")
    paytm, _ = ingest_paytm(
        config, session,
        file_paths=[p for p in config.source_status("paytm_settlement").files
                    if p.parent == root_paytm and p.suffix.lower() == ".zip"])
    axis, _ = ingest_axis_edc(
        config, session, file_path=config.sources["axis_edc"] / "Axis_20th_July'26.xlsx")

    digital = bills[bills["in_sale_universe"] & bills["mop_class"].isin(("digital", "digital_noref"))]
    pool = successful_acquiring(build_gateway_pool(paytm, axis, None))
    _, consumed = match_tier1(digital.copy().reset_index(drop=True), pool)

    residual = pool.loc[~pool.index.isin(consumed)]
    counts = residual["gateway"].value_counts().to_dict()
    assert counts == {"paytm": 57, "axis": 2}


def test_unmapped_after_tier2_is_internally_consistent(r1_20jul):
    """59 unmapped before Tier 2, 38 consumed by it, so exactly 21 must remain.

    The published figures say Paytm 57->19 and Axis 2->0, which together need
    40 consumptions from 38 matches. Axis does reach 0, exactly as published.
    """
    result, *_ = r1_20jul
    assert result.summary["unmapped_txns"] == 21
    assert result.summary["unmapped_txns_by_gateway"] == {"paytm": 21}
    # Every Tier-2 match consumed exactly one transaction.
    assert 59 - result.summary["tier2_matched"] == result.summary["unmapped_txns"]


# ---------------------------------------------------------------------------
# The governing rule: ONE successful match, not one per reference (6.3)
# ---------------------------------------------------------------------------

def test_a_bill_needs_only_one_resolving_reference():
    """A bill carrying a good Paytm ref and a dud Axis ref is MATCHED, and the
    dud reference is not an exception."""
    bills = pd.DataFrame([{
        "bill_no": "B1", "store": "ABD", "business_date": date(2026, 7, 20),
        "mop": "PAYTM EDC", "mop_class": "digital", "amount": 100.0,
        "paytm_ref": "GOOD", "axis_ref": "DUD", "bajaj_ref": None,
    }])
    pool = pd.DataFrame([{
        "gateway": "paytm", "txn_ref": "GOOD", "business_date": date(2026, 7, 20),
        "amount": 100.0, "status": "SUCCESS", "txn_type": "ACQUIRING",
        "payment_mode": "UPI", "store": "ABD", "txn_time": None,
    }])
    out, consumed = match_tier1(bills, successful_acquiring(pool))

    assert bool(out.at[0, "matched"]) is True
    assert out.at[0, "matched_gateway"] == "paytm"
    assert out.at[0, "refs_tried"] == 2      # both references were attempted
    assert out.at[0, "refs_resolved"] == 1   # one resolving reference is enough


def test_a_transaction_is_consumed_and_cannot_match_twice():
    bills = pd.DataFrame([
        {"bill_no": "B1", "store": "ABD", "business_date": date(2026, 7, 20), "mop": "PaytmDQR",
         "mop_class": "digital", "amount": 100.0, "paytm_ref": "T1", "axis_ref": None, "bajaj_ref": None},
        {"bill_no": "B2", "store": "ABD", "business_date": date(2026, 7, 20), "mop": "PaytmDQR",
         "mop_class": "digital", "amount": 100.0, "paytm_ref": "T1", "axis_ref": None, "bajaj_ref": None},
    ])
    pool = pd.DataFrame([{
        "gateway": "paytm", "txn_ref": "T1", "business_date": date(2026, 7, 20),
        "amount": 100.0, "status": "SUCCESS", "txn_type": "ACQUIRING",
        "payment_mode": "UPI", "store": "ABD", "txn_time": None,
    }])
    out, consumed = match_tier1(bills, successful_acquiring(pool))
    assert int(out["matched"].sum()) == 1
    assert len(consumed) == 1


def test_status_filter_is_case_insensitive():
    """The Bajaj file mixes 'Success' and 'SUCCESS' — a case-sensitive filter
    would silently drop 532 good transactions."""
    pool = pd.DataFrame([
        {"gateway": "bajaj", "txn_ref": "A", "business_date": date(2026, 7, 1), "amount": 1.0,
         "status": "Success", "txn_type": "ACQUIRING", "payment_mode": None, "store": None, "txn_time": None},
        {"gateway": "bajaj", "txn_ref": "B", "business_date": date(2026, 7, 1), "amount": 1.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": None, "store": None, "txn_time": None},
        {"gateway": "bajaj", "txn_ref": "C", "business_date": date(2026, 7, 1), "amount": 1.0,
         "status": "Refunded", "txn_type": "ACQUIRING", "payment_mode": None, "store": None, "txn_time": None},
    ])
    assert len(successful_acquiring(pool)) == 2


def test_tier2_never_applied_to_a_mop_that_should_carry_a_reference():
    """A PaytmDQR bill with no match must NOT be rescued by store+date+amount."""
    bills = pd.DataFrame([{
        "bill_no": "B1", "store": "ABD", "business_date": date(2026, 7, 20), "mop": "PaytmDQR",
        "mop_class": "digital", "amount": 100.0, "paytm_ref": None, "axis_ref": None,
        "bajaj_ref": None, "matched": False, "match_tier": pd.NA,
        "matched_gateway": pd.NA, "matched_txn_ref": pd.NA, "refs_tried": 0, "refs_resolved": 0,
    }])
    pool = pd.DataFrame([{
        "gateway": "paytm", "txn_ref": "T9", "business_date": date(2026, 7, 20),
        "amount": 100.0, "status": "SUCCESS", "txn_type": "ACQUIRING",
        "payment_mode": "UPI", "store": "ABD", "txn_time": None,
    }])
    out, consumed = match_tier2_edc_other(bills, pool, set())
    assert bool(out.at[0, "matched"]) is False
    assert consumed == set()


def test_tier2_reports_ambiguity_rather_than_guessing():
    bills = pd.DataFrame([{
        "bill_no": "B1", "store": "ABD", "business_date": date(2026, 7, 20), "mop": "EDC OTHER",
        "mop_class": "digital_noref", "amount": 100.0, "paytm_ref": None, "axis_ref": None,
        "bajaj_ref": None, "matched": False, "match_tier": pd.NA,
        "matched_gateway": pd.NA, "matched_txn_ref": pd.NA, "refs_tried": 0, "refs_resolved": 0,
    }])
    pool = pd.DataFrame([
        {"gateway": "paytm", "txn_ref": "T1", "business_date": date(2026, 7, 20), "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI", "store": "ABD", "txn_time": None},
        {"gateway": "paytm", "txn_ref": "T2", "business_date": date(2026, 7, 20), "amount": 100.0,
         "status": "SUCCESS", "txn_type": "ACQUIRING", "payment_mode": "UPI", "store": "ABD", "txn_time": None},
    ])
    out, consumed = match_tier2_edc_other(bills, pool, set())
    assert out.at[0, "match_tier"] == "Ambiguous"
    assert bool(out.at[0, "matched"]) is False
    assert consumed == set()  # nothing consumed on an ambiguous case


# ---------------------------------------------------------------------------
# Non-ACQUIRING linkage (6.3)
# ---------------------------------------------------------------------------

def test_non_acquiring_links_to_original_and_inherits_mode():
    paytm = pd.DataFrame([
        {"transaction_id": "ORIG1", "transaction_type": "ACQUIRING", "business_date": date(2026, 7, 1),
         "amount": 500.0, "payment_mode": "UPI", "reference_transaction_id": "", "store": "ABD"},
        {"transaction_id": "CB1", "transaction_type": "CHARGEBACK", "business_date": date(2026, 7, 19),
         "amount": 500.0, "payment_mode": "", "reference_transaction_id": "ORIG1", "store": None},
    ])
    linked, exceptions = link_non_acquiring(paytm)

    assert len(linked) == 1
    assert len(exceptions) == 0
    # The blank payment_mode is filled from the original — no special routing branch needed.
    assert linked.iloc[0]["payment_mode"] == "UPI"


def test_non_acquiring_without_its_original_is_an_exception():
    paytm = pd.DataFrame([
        {"transaction_id": "CB1", "transaction_type": "CHARGEBACK_REVERSAL",
         "business_date": date(2026, 5, 28), "amount": 3379.0, "payment_mode": "UPI",
         "reference_transaction_id": "NOT_IN_RANGE", "store": None},
    ])
    linked, exceptions = link_non_acquiring(paytm)
    assert len(linked) == 0
    assert len(exceptions) == 1
    assert exceptions.iloc[0]["reference_transaction_id"] == "NOT_IN_RANGE"


def test_missing_reference_column_raises_rather_than_reporting_zero():
    paytm = pd.DataFrame([{"transaction_id": "X", "transaction_type": "REFUND"}])
    with pytest.raises(KeyError, match="reference_transaction_id"):
        link_non_acquiring(paytm)


def test_20jul_linkage_finds_the_one_out_of_range_refund(r1_20jul):
    """The daily file's single REFUND points at a 29-Apr original that isn't in
    range, so it is an exception — not silently dropped."""
    result, *_ = r1_20jul
    assert result.summary["linkage_exceptions"] == 1
