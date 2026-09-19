"""Stage 3 tests — POS ingestion.

Acceptance figures are from the real 20-Jul-2026 file in `1. POS Sales`,
which is the same business date the prompt's acceptance numbers describe —
no samples/ fallback was needed; see the reply text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import mop_reco.ingest as ingest_module
from mop_reco.config import load_config
from mop_reco.db import get_engine, get_session, init_db
from mop_reco.ingest import (
    IngestError,
    _assert_known_mops,
    _extract_pos_refs,
    detect_duplicate_candidates,
    ingest_pos_bill_wise,
    ingest_pos_store_wise,
    preflight_bill_vs_store_wise,
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


@pytest.fixture
def daily_pos_file(config):
    """The loose 20-Jul daily .xlsx.

    Depth-1 discovery now finds the 222MB monthly .xlsb too, and the
    most-recently-modified rule would pick it — correct in production, wrong
    for these tests, which assert the daily file's figures (40,870 rows incl.
    the 628 non-bill rows) and must not take 150s each."""
    root = config.sources["pos_bill_wise"]
    return root / "MOP SALE DATA (20.07.2026).xlsx"


@pytest.fixture(autouse=True)
def isolated_warehouse(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_module, "WAREHOUSE_ROOT", tmp_path / "warehouse")
    yield


# ---------------------------------------------------------------------------
# Real-data acceptance figures — 20 July 2026
# ---------------------------------------------------------------------------

def test_bill_wise_row_and_bill_counts(config, session, daily_pos_file):
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)

    assert report.rows_read == 40870
    assert report.breakdown["distinct_bill_no"] == 36889
    assert report.breakdown["multi_line_bills"] == 3276


def test_digital_bill_line_count(config, session, daily_pos_file):
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    digital = report.breakdown["mop_class"]["digital"]["count"] + report.breakdown["mop_class"]["digital_noref"]["count"]
    assert digital == 17536


def test_duplicate_candidate_groups_and_bills(config, session, daily_pos_file):
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    assert report.breakdown["duplicate_candidates"]["groups"] == 1664
    assert report.breakdown["duplicate_candidates"]["bills_covered"] == 4158


def test_detect_duplicate_candidates_returns_bill_numbers_not_just_counts(config, session, daily_pos_file):
    df, _ = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    groups = detect_duplicate_candidates(df)

    assert len(groups) == 1664
    total_bills = sum(len(g["bill_nos"]) for g in groups)
    assert total_bills == 4158

    # Every group must actually be investigable — real bill numbers, store, date, amount.
    sample = groups[0]
    assert isinstance(sample["bill_nos"], list) and len(sample["bill_nos"]) > 1
    assert sample["store"] and sample["business_date"] and isinstance(sample["amount"], float)


# ---------------------------------------------------------------------------
# Reference extraction — robust JSON parsing, failures visible
# ---------------------------------------------------------------------------

def test_reference_extraction_breakdown(config, session, daily_pos_file):
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    ref = report.breakdown["reference_extraction"]

    assert ref["paytm_ref_found"] == 17493
    # Card-conditional (CLAUDE.md 6.2): only the 821 PAYTM EDC rows that carry
    # issuerMaskCardNo yield an Axis reference — NOT all 12,424. The other
    # 11,603 carry a retrievalReferenceNo for a UPI/wallet transaction Axis
    # never processed.
    assert ref["axis_ref_found"] == 821
    assert ref["parse_failed"] == 0
    # No Bajaj MOP rows exist on 20-Jul in the daily file.
    assert ref["bajaj_ref_found"] == 0
    # Cash + Credit Note + EDC OTHER rows carry no TPEDCRESPONSE at all —
    # blank is normal, not a failure.
    assert ref["neither"] == 20051 + 1657 + 1626 + 43


def test_extract_pos_refs_handles_blank_as_none_and_none_as_blank():
    """Both a real None (from openpyxl) and pandas' float('nan') normalisation
    of a blank cell must be treated as 'no reference', not a parse failure."""
    assert _extract_pos_refs(None) == (None, None, None, False)
    assert _extract_pos_refs(float("nan")) == (None, None, None, False)
    assert _extract_pos_refs("") == (None, None, None, False)


def test_extract_pos_refs_reports_genuine_parse_failure_visibly():
    paytm_ref, axis_ref, bajaj_ref, failed = _extract_pos_refs("{not valid json")
    assert failed is True
    assert paytm_ref is None and axis_ref is None and bajaj_ref is None


def test_extract_pos_refs_picks_acquirementid_or_txnid():
    dqr_json = '{"body":{"txnId":"TXN789"}}'
    assert _extract_pos_refs(dqr_json) == ("TXN789", None, None, False)


def test_axis_ref_is_card_conditional():
    """retrievalReferenceNo is taken ONLY when issuerMaskCardNo is present."""
    card = ('{"body":{"acquirementId":"ACQ123","retrievalReferenceNo":"RRN456",'
            '"issuerMaskCardNo":"512345XXXXXX1234"}}')
    assert _extract_pos_refs(card) == ("ACQ123", "RRN456", None, False)

    # Same payload, no card indicator — the RRN must NOT be taken.
    non_card = '{"body":{"acquirementId":"ACQ123","retrievalReferenceNo":"RRN456"}}'
    paytm_ref, axis_ref, bajaj_ref, failed = _extract_pos_refs(non_card)
    assert paytm_ref == "ACQ123"
    assert axis_ref is None
    assert failed is False


def test_bajaj_ref_extracted_from_flat_payload():
    """Bajaj has no head/body envelope — the key is a flat lowercase `rrn`,
    which joins to the settlement column 'Transaction Id'."""
    bajaj_json = ('{"tid":"3095432A","amount":"2244.00","rrn":"864613705866",'
                  '"card_scheme":"UPI","status":"success"}')
    paytm_ref, axis_ref, bajaj_ref, failed = _extract_pos_refs(bajaj_json)
    assert bajaj_ref == "864613705866"
    assert paytm_ref is None and axis_ref is None and failed is False


def test_bill_wise_reports_parse_failures_when_present(config, session, monkeypatch, daily_pos_file):
    """Prove a genuine parse failure surfaces in the report, not just in the
    unit-level helper — inject one via a monkeypatched reader."""
    import mop_reco.io_ as io_mod

    real_read_any = io_mod.read_any

    def _corrupting_read_any(path, *args, **kwargs):
        result = real_read_any(path, *args, **kwargs)
        grid = [list(r) for r in result.grid]
        header = [str(c).strip() for c in grid[0]]
        tpedc_idx = header.index("TPEDCRESPONSE")
        for i, row in enumerate(grid[1:], start=1):
            if row[tpedc_idx] not in (None, "") and not (isinstance(row[tpedc_idx], float)):
                row[tpedc_idx] = "{this is not json"
                break
        from dataclasses import replace
        return replace(result, grid=grid)

    monkeypatch.setattr(ingest_module.io_, "read_any", _corrupting_read_any)

    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    assert report.breakdown["reference_extraction"]["parse_failed"] == 1
    assert df["tpedc_parse_failed"].sum() == 1


# ---------------------------------------------------------------------------
# The sale universe — a row with no bill number is not a bill (CLAUDE.md 6.13)
# ---------------------------------------------------------------------------

def test_cash_total_ties_to_store_wise_dump(config, session, daily_pos_file):
    """THE cash test. Summing every cash row gives -18,482,043; restricted to
    the sale universe it gives +12,941,046, matching the store-wise dump."""
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)

    assert report.breakdown["mop_class"]["cash"]["total_amount"] == 12941046.0

    # And prove the rule is what did it, not a coincidence.
    all_cash = df[df["mop"] == "Cash"]["amount"].sum()
    assert all_cash == -18482043.0


def test_other_three_classes_still_tie(config, session, daily_pos_file):
    """These tied before the fix and must not move because of it."""
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    classes = report.breakdown["mop_class"]
    assert classes["digital"]["total_amount"] == 13660562.0
    assert classes["digital_noref"]["total_amount"] == 42102.0
    assert classes["non_settling"]["total_amount"] == -2092.0


def test_excluded_rows_are_reported_per_mop_not_silently_dropped(config, session, daily_pos_file):
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    su = report.breakdown["sale_universe"]

    assert su["rows_without_bill_no"] == 628
    assert su["rows_in_sale_universe"] == 40242
    # All 628 are Cash and nothing else — which is why only cash failed to tie.
    assert list(su["rows_without_bill_no_per_mop"]) == ["Cash"]
    assert su["rows_without_bill_no_per_mop"]["Cash"]["count"] == 628

    # Excluded but still present in the frame — nothing dropped from what was read.
    assert report.rows_read == 40870
    assert len(df) == 40870
    assert (~df["in_sale_universe"]).sum() == 628


# ---------------------------------------------------------------------------
# Unknown MOP stops that store-day
# ---------------------------------------------------------------------------

def test_unknown_mop_stops_ingestion_naming_mop_store_and_date(config):
    df = pd.DataFrame({
        "MOPDESC": ["Cash", "SomeNewWalletThatIsNotConfigured"],
        "STORE_NAME": ["ABD", "XYZ"],
        "business_date": ["2026-07-20", "2026-07-20"],
    })
    with pytest.raises(IngestError) as exc_info:
        _assert_known_mops(df, config)

    message = str(exc_info.value)
    assert "SomeNewWalletThatIsNotConfigured" in message
    assert "XYZ" in message
    assert "2026-07-20" in message


def test_known_mops_pass_without_error(config):
    df = pd.DataFrame({
        "MOPDESC": ["Cash", "PAYTM EDC"],
        "STORE_NAME": ["ABD", "ABD"],
        "business_date": ["2026-07-20", "2026-07-20"],
    })
    _assert_known_mops(df, config)  # must not raise


# ---------------------------------------------------------------------------
# Store-wise: derived, never authoritative, pre-flight never passes
# ---------------------------------------------------------------------------

def test_store_wise_preflight_never_reports_passed(config):
    result = preflight_bill_vs_store_wise(config)
    assert result["status"] != "passed"
    assert result["status"] == "store-wise dump not supplied"


def test_store_wise_is_labelled_derived_not_authoritative(config, session, daily_pos_file):
    # Pass the daily-file frame in explicitly: without it this re-ingests via
    # latest-file, which is now the 222MB monthly .xlsb.
    bw_df, bw_report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    totals, report = ingest_pos_store_wise(config, session, bill_wise_df=bw_df)

    assert report.status == "derived_not_authoritative"
    assert any("NOT authoritative" in n for n in report.notes)
    assert (totals["source"] == "derived_from_bill_wise_not_authoritative").all()
    # Control total must tie back exactly to the bill-wise total it was derived from.
    assert report.control_total == pytest.approx(bw_report.control_total)


def test_derived_store_wise_cash_ties_and_excludes_non_bill_rows(config, session, daily_pos_file):
    """The derived store-wise totals must apply 6.13 too — otherwise the
    non-bill cash rows come straight back in at store level."""
    bw_df, _ = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    totals, _ = ingest_pos_store_wise(config, session, bill_wise_df=bw_df)

    cash_total = totals.loc[totals["mop_class"] == "cash", "total_amount"].sum()
    assert cash_total == 12941046.0


# ---------------------------------------------------------------------------
# Rows per business date
# ---------------------------------------------------------------------------

def test_rows_per_business_date_reported(config, session, daily_pos_file):
    df, report = ingest_pos_bill_wise(config, session, file_path=daily_pos_file)
    # Sale-universe rows, consistent with every other count in the report:
    # 40,870 read − 628 non-bill = 40,242.
    assert report.breakdown["rows_per_business_date"] == {"2026-07-20": 40242}
