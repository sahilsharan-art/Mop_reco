"""Stage 2 tests — raw report normalisers.

Runs against the real files in the working folders (read-only) and a fresh,
throwaway SQLite DB per test — never the dev db at db/reco.sqlite, and never
the real warehouse/ folder (redirected to tmp_path).
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from datetime import date
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import mop_reco.ingest as ingest_module
from mop_reco.config import load_config
from mop_reco.db import get_engine, get_session, init_db
from mop_reco.ingest import (
    IngestError,
    ingest_axis_cms,
    ingest_axis_edc,
    ingest_bajaj,
    ingest_bank_statement,
    ingest_paytm,
)
from mop_reco.io_ import clean_ref, find_header_row


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
def daily_files(config):
    """The loose single-day files in each source root.

    Depth-1 discovery now also finds the full-July dumps, and latest-file would
    pick those — correct in production, wrong for these tests, which assert the
    20-Jul figures. Each test says which file it means."""
    return {
        "paytm": [p for p in config.source_status("paytm_settlement").files
                  if p.parent == config.sources["paytm_settlement"] and p.suffix.lower() == ".zip"],
        "axis_edc": config.sources["axis_edc"] / "Axis_20th_July'26.xlsx",
        "axis_cms": config.sources["axis_cms"] / "CMS 21st_July'26.xlsx",
    }


@pytest.fixture(autouse=True)
def isolated_warehouse(tmp_path, monkeypatch):
    """Every test writes Parquet to its own tmp_path, never the real
    warehouse/ folder — ingestion tests must not leave output behind in the
    working tree."""
    warehouse_dir = tmp_path / "warehouse"
    monkeypatch.setattr(ingest_module, "WAREHOUSE_ROOT", warehouse_dir)
    yield warehouse_dir


# ---------------------------------------------------------------------------
# Paytm — 3 zip parts, dedupe, settlement fields, REFUND preserved
# ---------------------------------------------------------------------------

def test_paytm_row_counts_and_settlement_fields(config, session, daily_files):
    df, report = ingest_paytm(config, session, file_paths=daily_files['paytm'])

    assert report.rows_before_dedupe == 17099
    assert report.rows_after_dedupe == 17099  # no overlap between the 3 parts on this sample
    assert len(df) == 17099

    assert df["payout_id"].nunique() == 279
    assert df["settled_amount"].notna().all()
    assert (df["settled_amount"] != 0).all()


def test_paytm_refund_row_is_present_and_classified(config, session, daily_files):
    df, report = ingest_paytm(config, session, file_paths=daily_files['paytm'])

    refund_rows = df[df["transaction_type"] == "REFUND"]
    assert len(refund_rows) == 1
    assert report.breakdown["transaction_type"]["REFUND"] == 1
    assert report.breakdown["transaction_type"]["ACQUIRING"] == 17098


def test_paytm_writes_one_parquet_part_per_business_date(config, session, isolated_warehouse, daily_files):
    df, report = ingest_paytm(config, session, file_paths=daily_files['paytm'])
    assert report.business_dates == ["2026-07-20"]
    out_path = Path(report.warehouse_paths["2026-07-20"])
    assert out_path.exists()
    assert out_path.is_relative_to(isolated_warehouse)


# ---------------------------------------------------------------------------
# Axis EDC — real numbers: 453 rows, 452 resolve, 1 known-unmapped MID
# ---------------------------------------------------------------------------

def test_axis_edc_row_count(config, session, daily_files):
    df, report = ingest_axis_edc(config, session, file_path=daily_files['axis_edc'])
    assert report.rows_read == 453
    assert len(df) == 453


def test_axis_edc_store_resolution_matches_known_inventory_gap(config, session, daily_files):
    """452 of 453 rows resolve to a store. The one that doesn't (MID
    037135034530134) is a real, previously-confirmed master-data gap (see
    docs/DATA_INVENTORY.md) — not something this stage should paper over."""
    df, report = ingest_axis_edc(config, session, file_path=daily_files['axis_edc'])

    assert report.rows_with_store == 452
    assert len(report.unmapped) == 1
    assert report.unmapped[0].identifier == "037135034530134"

    unresolved_rows = df[df["store"].isna()]
    assert len(unresolved_rows) == 1
    assert unresolved_rows.iloc[0]["MID"] == "037135034530134"


# ---------------------------------------------------------------------------
# Axis CMS — real numbers from the actual "3. CMS received" file: 365 rows,
# COL 357 / ZPO 8, no ADJ rows. (See the reply text for why this differs from
# the acceptance figures quoted in the prompt.)
# ---------------------------------------------------------------------------

def test_axis_cms_row_count_and_type_breakdown(config, session, daily_files):
    df, report = ingest_axis_cms(config, session, file_path=daily_files['axis_cms'])

    assert report.rows_read == 365
    assert report.breakdown["TYPE"] == {"COL": 357, "ZPO": 8}


def test_axis_cms_all_dated_rows_resolve_to_a_store(config, session, daily_files):
    """All 357 COL rows resolve. The 8 ZPO rows have no DEPDATE at all in this
    file, so they're never passed to resolve_store — reported as
    rows_without_date, not silently dropped and not guessed a store."""
    df, report = ingest_axis_cms(config, session, file_path=daily_files['axis_cms'])

    assert report.rows_without_date == 8
    assert report.rows_with_store == 357
    assert report.unmapped == []

    dated_rows = df[df["business_date"].notna()]
    assert dated_rows["store"].notna().all()


def test_axis_cms_splits_the_shared_pickup_code_by_location(config, session, daily_files):
    """Defect A (2026-09-12): AX31918 is APM/MMR's shared pickup code. The
    21-Jul file carries both locations under it — ARRAH must resolve to APM,
    MADHB to MMR, and neither shows up as unmapped or triggers the
    pre-flight note, since this code IS registered in the pickup_location
    master."""
    df, report = ingest_axis_cms(config, session, file_path=daily_files['axis_cms'])

    shared = df[df["PICKUPPOINTCODE"] == "AX31918"]
    assert not shared.empty
    by_location = shared.set_index("LOCAT")["store"]
    assert (by_location.loc["ARRAH"] == "APM").all()
    assert (by_location.loc["MADHB"] == "MMR").all()
    assert shared["store"].notna().all()
    assert not any("AX31918" in n for n in report.notes)


def test_axis_cms_credit_lag_distribution_is_reported(config, session, daily_files):
    """Not asserted against an external figure — just confirm the shape is
    right so the number in the reply text can be trusted. See the reply for
    the actual counts to compare against your separate workbook."""
    df, report = ingest_axis_cms(config, session, file_path=daily_files['axis_cms'])

    lag = report.breakdown["credit_lag_days"]
    assert sum(lag.values()) == 357  # every COL/ZPO-dated row with both dates present
    assert all(k >= 0 for k in lag)


# ---------------------------------------------------------------------------
# Column-position assertion — proven with a deliberately shuffled copy of the
# real file, not by inspecting the position table.
# ---------------------------------------------------------------------------

def test_axis_cms_preflight_flags_an_unregistered_shared_code(config, session, tmp_path, daily_files):
    """The pre-flight check (CLAUDE.md 6.11, added 2026-09-12): a pickup code
    seen against more than one location that ISN'T in the pickup_location
    master must be reported, not silently resolved on the code alone — that
    silence is exactly how defect A went unnoticed for a month. Built by
    taking a real single-location code (AX31911) and giving half its rows a
    second, fabricated location, rather than waiting for the agency to do it
    for real."""
    real_file = daily_files["axis_cms"]

    wb = openpyxl.load_workbook(real_file, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    header = rows[0]
    code_idx = header.index("PICKUPPOINTCODE")
    locat_idx = header.index("LOCAT")

    hit_one = False
    for row in rows[1:]:
        if row[code_idx] == "AX31911" and not hit_one:
            row[locat_idx] = "FABRICATED-SECOND-LOCATION"
            hit_one = True
    assert hit_one, "fixture assumption: AX31911 has at least one row"

    mutated_dir = tmp_path / "mutated_cms"
    mutated_dir.mkdir()
    out_wb = openpyxl.Workbook()
    out_ws = out_wb.active
    for row in rows:
        out_ws.append(row)
    mutated_path = mutated_dir / "CMS mutated.xlsx"
    out_wb.save(mutated_path)

    mutated_config = dataclasses.replace(
        config, sources={**config.sources, "axis_cms": mutated_dir})

    df, report = ingest_axis_cms(mutated_config, session, file_path=mutated_path)

    assert any("AX31911" in n for n in report.notes)
    # AX31918 (the one code that IS registered) must never be named here.
    assert not any("AX31918" in n for n in report.notes)


def test_cms_column_position_assertion_fires_on_shuffled_header(config, session, tmp_path, daily_files):
    real_file = daily_files["axis_cms"]

    wb = openpyxl.load_workbook(real_file, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]

    # Swap the CLGDATE (index 9 / column J) and CRDATE (index 10 / column K)
    # header cells only — the data underneath is untouched, so this is
    # exactly "a header got moved", not "the data changed".
    header = rows[0]
    header[9], header[10] = header[10], header[9]

    shuffled_dir = tmp_path / "shuffled_cms"
    shuffled_dir.mkdir()
    out_wb = openpyxl.Workbook()
    out_ws = out_wb.active
    for row in rows:
        out_ws.append(row)
    shuffled_path = shuffled_dir / "CMS shuffled.xlsx"
    out_wb.save(shuffled_path)

    shuffled_config = dataclasses.replace(
        config, sources={**config.sources, "axis_cms": shuffled_dir}
    )

    with pytest.raises(IngestError) as exc_info:
        ingest_axis_cms(shuffled_config, session)

    message = str(exc_info.value)
    assert "position J (9)" in message or "position K (10)" in message
    assert "CLGDATE" in message and "CRDATE" in message


# ---------------------------------------------------------------------------
# Re-running the same date twice produces identical Parquet output
# ---------------------------------------------------------------------------

def test_rerun_produces_identical_parquet(config, session, isolated_warehouse, daily_files):
    df1, report1 = ingest_axis_edc(config, session, file_path=daily_files['axis_edc'])
    path1 = Path(report1.warehouse_paths["2026-07-20"])
    frame1 = pd.read_parquet(path1)

    # Move the first output aside so the second run can't just be reading it
    # back — prove a fresh write produces the same content, not the same file.
    moved = path1.with_suffix(".first.parquet")
    shutil.move(path1, moved)

    df2, report2 = ingest_axis_edc(config, session, file_path=daily_files['axis_edc'])
    path2 = Path(report2.warehouse_paths["2026-07-20"])
    frame2 = pd.read_parquet(path2)

    pd.testing.assert_frame_equal(frame1.reset_index(drop=True), frame2.reset_index(drop=True))


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def test_bajaj_stub_reports_no_source(config, session, tmp_path, monkeypatch):
    # Genuinely no Bajaj file anywhere for the source — distinct from a file
    # that exists but is empty (test_ingest_bajaj_* elsewhere covers that).
    # A dedicated subdirectory, not tmp_path itself, since other fixtures in
    # this test run (e.g. the test database) may already have written into
    # tmp_path.
    empty_dir = tmp_path / "no_bajaj_here"
    empty_dir.mkdir()
    monkeypatch.setitem(config.sources, "bajaj", empty_dir)
    df, report = ingest_bajaj(config, session)
    assert report.status == "no_source_report"
    assert len(df) == 0


def test_bank_statement_stub_reports_no_source(config, session):
    df, report = ingest_bank_statement(config, session)
    assert report.status == "no_source_report"
    assert len(df) == 0


# ---------------------------------------------------------------------------
# io_.py utilities
# ---------------------------------------------------------------------------

def test_find_header_row_skips_title_and_blank_rows():
    grid = [
        ["Cube Name:PETTY CASH REG", "Start Date:31-DEC-2015", "End Date:31-MAR-2027"],
        [],
        ["Source Short Name", "Billdate", "PTC Head"],
        ["MMR", "21-JUL-2026", "Cash/CMS Deposit"],
    ]
    idx = find_header_row(grid, ["Source Short Name", "Billdate", "PTC Head"])
    assert idx == 2


def test_find_header_row_raises_when_not_found():
    grid = [["A", "B"], ["1", "2"]]
    with pytest.raises(Exception):
        find_header_row(grid, ["NOT_A_REAL_FIELD"])


@pytest.mark.parametrize("raw,expected", [
    # A leading apostrophe is Excel's "force text" marker (CLAUDE.md 6.2) —
    # not the same thing as Paytm's CSV values being wrapped in quotes on
    # both ends, which is a separate concern handled in ingest.py.
    ("'20260720011060000285973002491941526", "20260720011060000285973002491941526"),
    ("", None),
    ("0", None),
    (0, None),
    ("nan", None),
    ("#N/A", None),
    ("0x2a", None),
    ("123.0", "123"),
    ("620111716333", "620111716333"),
])
def test_clean_ref(raw, expected):
    assert clean_ref(raw) == expected
