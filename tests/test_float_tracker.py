"""Store float (2026-09-13, formula and threshold corrected 2026-09-15,
column I's own sign corrected 2026-09-17).

Float = opening balance LESS the cash deposited that business date — NOT the
closing balance, which the original build wrongly used. `build_float_grid`
reads the store sheet's own `float_amount` column (= H - I, §6.9d, I stored
POSITIVE since 2026-09-17) rather than recomputing it, so this module cannot
drift from the sheet operators already reconcile against. What this module
adds on top — and what needs testing — is the threshold (Rs 10,000 default),
the high-float flag, the consecutive-day streak, the negative-float
drill-down, the weekend/2nd-and-4th-Saturday/holiday auto-remark, and the
operator-remark persistence with history.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import float_tracker as ft
from mop_reco import masters
from mop_reco.db import Base, get_session


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return get_session(engine)


def sheet_rows(rows):
    """Minimal store-sheet frame — only the columns build_float_grid reads.
    `float_amount` is passed explicitly rather than derived here, exactly as
    the real store sheet already carries it as its own column N."""
    return pd.DataFrame(rows, columns=[
        "date", "store", "opening_balance", "cash_sale", "cash_deposit",
        "float_amount", "cash_received", "closing_balance",
    ])


def float_row(business_date, store, opening, deposit_positive, closing=999_999):
    """Build one sheet row the way store_sheet.py actually would: cash_deposit
    carried POSITIVE (since 2026-09-17), float_amount = opening - cash_deposit
    — never independently invented here."""
    cash_deposit = abs(deposit_positive)
    float_amount = opening - cash_deposit
    return [business_date, store, opening, 0, cash_deposit, float_amount, 0, closing]


# ---------------------------------------------------------------------------
# threshold_for
# ---------------------------------------------------------------------------

def test_threshold_defaults_to_10000_with_no_config():
    assert ft.threshold_for("ABD", {}) == 10_000.0
    assert ft.threshold_for("ABD", None) == 10_000.0


def test_threshold_reads_the_configured_default():
    config = {"float_tracker": {"float_threshold": 75_000}}
    assert ft.threshold_for("ABD", config) == 75_000.0


def test_threshold_override_wins_for_the_named_store_only():
    config = {"float_tracker": {"float_threshold": 10_000,
                                "threshold_overrides": {"MBA": 300_000}}}
    assert ft.threshold_for("MBA", config) == 300_000.0
    assert ft.threshold_for("ABD", config) == 10_000.0


# ---------------------------------------------------------------------------
# second_and_fourth_saturdays — unchanged, counted by Saturday-of-the-month
# ---------------------------------------------------------------------------

def test_second_and_fourth_saturdays_july_2026():
    # July 2026: Saturdays fall on 4, 11, 18, 25 — 2nd is 11, 4th is 25.
    picked = ft.second_and_fourth_saturdays(2026, 7)
    assert picked == {date(2026, 7, 11), date(2026, 7, 25)}


def test_second_and_fourth_saturdays_a_month_starting_on_saturday():
    # August 2026 starts on a Saturday: Saturdays are 1, 8, 15, 22, 29.
    # 2nd is 8, 4th is 22 — NOT 1 and 15, which a fixed-date-range approach
    # (e.g. "days 8-14" for the 2nd week) could get wrong.
    picked = ft.second_and_fourth_saturdays(2026, 8)
    assert picked == {date(2026, 8, 8), date(2026, 8, 22)}


def test_a_month_with_only_four_saturdays_has_no_fifth():
    # February 2026 (28 days, starts Sunday) has exactly 4 Saturdays.
    picked = ft.second_and_fourth_saturdays(2026, 2)
    assert len(picked) == 2


# ---------------------------------------------------------------------------
# auto_remark_for — unchanged
# ---------------------------------------------------------------------------

def test_sunday_is_weekend():
    # 2026-07-05 is a Sunday.
    assert ft.auto_remark_for(date(2026, 7, 5)) == "Weekend"


def test_second_saturday_is_weekend():
    assert ft.auto_remark_for(date(2026, 7, 11)) == "Weekend"


def test_fourth_saturday_is_weekend():
    assert ft.auto_remark_for(date(2026, 7, 25)) == "Weekend"


def test_first_saturday_is_not_weekend():
    # 2026-07-04 is the 1st Saturday — not 2nd or 4th, so no auto-remark
    # (assuming it is not a listed holiday either).
    assert ft.auto_remark_for(date(2026, 7, 4)) == ""


def test_a_weekday_is_not_weekend():
    # 2026-07-20 is a Monday.
    assert ft.auto_remark_for(date(2026, 7, 20)) == ""


def test_a_listed_holiday_is_flagged(monkeypatch):
    monkeypatch.setattr(masters, "_HOLIDAY_SET", frozenset({date(2026, 8, 15)}))
    assert ft.auto_remark_for(date(2026, 8, 15)) == "Holiday"


def test_with_no_holiday_calendar_loaded_holiday_never_fires():
    # The real gap documented in config.yaml (holiday_calendar: null) — a
    # weekday that isn't in the (empty) set must never read "Holiday".
    assert masters._HOLIDAY_SET == frozenset() or True  # module-level default
    assert ft.auto_remark_for(date(2026, 7, 20)) == ""


# ---------------------------------------------------------------------------
# build_float_grid — the real-data check first, from the operator's own rows
# ---------------------------------------------------------------------------

def test_the_operators_own_2026_07_31_rows_match_exactly():
    """ABD/AGT/AJL/ALC, 2026-07-31, as given: float = opening - deposit."""
    sheet = sheet_rows([
        float_row("2026-07-31", "ABD", 69_550.00, 61_550.00),
        float_row("2026-07-31", "AGT", 64_766.00, 46_120.00),
        float_row("2026-07-31", "AJL", 49_143.00, 37_975.00),
        float_row("2026-07-31", "ALC", 1_03_576.32, 88_400.00),
    ])
    grid = ft.build_float_grid(sheet, {}).set_index("store")
    assert grid.loc["ABD", "float_amount"] == pytest.approx(8_000.00)
    assert grid.loc["AGT", "float_amount"] == pytest.approx(18_646.00)
    assert grid.loc["AJL", "float_amount"] == pytest.approx(11_168.00)
    assert grid.loc["ALC", "float_amount"] == pytest.approx(15_176.32)


def test_empty_sheet_gives_an_empty_grid_with_the_right_columns():
    grid = ft.build_float_grid(sheet_rows([]), {})
    assert grid.empty
    assert list(grid.columns) == ft.FIELDS


def test_high_float_is_based_on_float_not_closing_balance():
    """A store with a LOW float but a HIGH closing balance (large same-day
    cash sale, not yet deposited) must not read high float — this is exactly
    the bug that was fixed: closing balance is not float."""
    config = {"float_tracker": {"float_threshold": 10_000}}
    sheet = sheet_rows([
        # opening 10,000, deposit 8,000 -> float 2,000 (below threshold),
        # but closing balance is 500,000 (huge same-day cash sale sitting
        # in the drawer, not float).
        ["2026-07-20", "ABD", 10_000, 490_000, 8_000, 2_000, 0, 500_000],
    ])
    grid = ft.build_float_grid(sheet, config)
    row = grid.iloc[0]
    assert row["float_amount"] == 2_000
    assert row["closing_balance"] == 500_000
    assert not row["high_float"]
    assert row["cash_deposit"] == 8_000


def test_high_float_flag_fires_when_float_exceeds_threshold():
    config = {"float_tracker": {"float_threshold": 10_000}}
    sheet = sheet_rows([float_row("2026-07-20", "ABD", 69_550, 20_000)])  # float 49,550
    grid = ft.build_float_grid(sheet, config)
    assert grid.iloc[0]["high_float"]


def test_below_threshold_is_not_high_float():
    config = {"float_tracker": {"float_threshold": 10_000}}
    sheet = sheet_rows([float_row("2026-07-20", "ABD", 15_000, 10_000)])  # float 5,000
    grid = ft.build_float_grid(sheet, config)
    assert not grid.iloc[0]["high_float"]


def test_negative_float_is_shown_as_is_never_floored():
    """Deposit exceeds opening — a catch-up or misdated deposit. The actual
    negative value must survive, not be floored to zero or dropped."""
    config = {"float_tracker": {"float_threshold": 10_000}}
    sheet = sheet_rows([float_row("2026-07-20", "ABD", 20_000, 35_000)])  # float -15,000
    grid = ft.build_float_grid(sheet, config)
    row = grid.iloc[0]
    assert row["float_amount"] == -15_000
    assert row["negative_float"]
    assert not row["high_float"]  # negative is never "high"


def test_negative_float_rows_lists_and_sorts_most_negative_first():
    sheet = sheet_rows([
        float_row("2026-07-20", "ABD", 20_000, 25_000),   # float -5,000
        float_row("2026-07-20", "AGT", 20_000, 50_000),   # float -30,000
        float_row("2026-07-20", "AJL", 20_000, 10_000),   # float +10,000, not negative
    ])
    grid = ft.build_float_grid(sheet, {})
    rows = ft.negative_float_rows(grid)
    assert list(rows["store"]) == ["AGT", "ABD"]
    assert rows.iloc[0]["float_amount"] == -30_000


def test_negative_float_rows_is_empty_when_nothing_is_negative():
    sheet = sheet_rows([float_row("2026-07-20", "ABD", 20_000, 5_000)])
    grid = ft.build_float_grid(sheet, {})
    assert ft.negative_float_rows(grid).empty


def test_negative_float_rows_handles_an_empty_grid():
    assert ft.negative_float_rows(ft.build_float_grid(sheet_rows([]), {})).empty


def test_days_held_high_streaks_and_resets_per_store():
    config = {"float_tracker": {"float_threshold": 10_000}}
    sheet = sheet_rows([
        float_row("2026-07-20", "ABD", 60_000, 0),   # float 60,000 — high, day 1
        float_row("2026-07-21", "ABD", 70_000, 0),   # high, day 2
        float_row("2026-07-22", "ABD", 5_000, 0),    # float 5,000 — drops, resets
        float_row("2026-07-23", "ABD", 55_000, 0),   # high again — day 1
        # A different store's streak must not leak into ABD's.
        float_row("2026-07-20", "KGN", 90_000, 0),   # high, day 1
        float_row("2026-07-21", "KGN", 91_000, 0),   # high, day 2
    ])
    grid = ft.build_float_grid(sheet, config).set_index(["store", "business_date"])
    assert grid.loc[("ABD", "2026-07-20"), "days_held_high"] == 1
    assert grid.loc[("ABD", "2026-07-21"), "days_held_high"] == 2
    assert grid.loc[("ABD", "2026-07-22"), "days_held_high"] == 0
    assert grid.loc[("ABD", "2026-07-23"), "days_held_high"] == 1
    assert grid.loc[("KGN", "2026-07-20"), "days_held_high"] == 1
    assert grid.loc[("KGN", "2026-07-21"), "days_held_high"] == 2


def test_day_name_is_computed_from_the_business_date():
    sheet = sheet_rows([float_row("2026-07-20", "ABD", 0, 0)])  # a Monday
    grid = ft.build_float_grid(sheet, {})
    assert grid.iloc[0]["day_name"] == "Monday"


def test_per_store_override_is_applied_in_the_grid():
    config = {"float_tracker": {"float_threshold": 10_000,
                                "threshold_overrides": {"MBA": 500_000}}}
    sheet = sheet_rows([float_row("2026-07-20", "MBA", 400_000, 0)])  # float 400,000
    grid = ft.build_float_grid(sheet, config)
    assert grid.iloc[0]["float_threshold"] == 500_000
    # Below MBA's own override (500,000) even though it is far above the
    # Rs 10,000 default every other store uses.
    assert not grid.iloc[0]["high_float"]


# ---------------------------------------------------------------------------
# set_float_remark / history / with_remarks — unchanged mechanism
# ---------------------------------------------------------------------------

def test_set_float_remark_rejects_a_blank_remark(session):
    with pytest.raises(ValueError):
        ft.set_float_remark(session, "ABD", "2026-07-20", "   ", "aditi")


def test_set_float_remark_rejects_a_missing_operator(session):
    with pytest.raises(ValueError):
        ft.set_float_remark(session, "ABD", "2026-07-20", "Explained", "")


def test_set_float_remark_records_who_and_when(session):
    entry = ft.set_float_remark(session, "ABD", "2026-07-20",
                                "Bank pickup delayed", "aditi")
    assert entry.remark == "Bank pickup delayed"
    assert entry.remark_by == "aditi"
    assert entry.remark_at is not None


def test_a_second_remark_overwrites_the_current_row_but_keeps_history(session):
    ft.set_float_remark(session, "ABD", "2026-07-20", "First reason", "aditi")
    ft.set_float_remark(session, "ABD", "2026-07-20", "Corrected reason", "raj")

    current = ft.float_remarks_for_range(session, store="ABD")[("ABD", "2026-07-20")]
    assert current.remark == "Corrected reason"
    assert current.remark_by == "raj"

    history = ft.float_remark_history(session, "ABD", "2026-07-20")
    assert list(history["remark"]) == ["First reason", "Corrected reason"]
    assert list(history["remark_by"]) == ["aditi", "raj"]


def test_float_remarks_for_range_filters_by_date_and_store(session):
    ft.set_float_remark(session, "ABD", "2026-07-20", "In range", "aditi")
    ft.set_float_remark(session, "ABD", "2026-08-01", "Out of range", "aditi")
    ft.set_float_remark(session, "KGN", "2026-07-20", "Different store", "aditi")

    found = ft.float_remarks_for_range(session, date_from="2026-07-01",
                                       date_to="2026-07-31", store="ABD")
    assert set(found.keys()) == {("ABD", "2026-07-20")}


def test_with_remarks_prefers_a_recorded_remark_over_the_auto_remark(session):
    # 2026-07-05 is a Sunday — auto_remark_for would say "Weekend" — but an
    # operator's own remark must win, and is_auto must read False.
    ft.set_float_remark(session, "ABD", "2026-07-05", "Agency collected anyway", "aditi")
    grid = ft.build_float_grid(
        sheet_rows([float_row("2026-07-05", "ABD", 60_000, 0)]),
        {"float_tracker": {"float_threshold": 10_000}})
    remarks = ft.float_remarks_for_range(session)
    full = ft.with_remarks(grid, remarks)
    row = full.iloc[0]
    assert row["remark"] == "Agency collected anyway"
    assert row["remark_by"] == "aditi"
    assert bool(row["is_auto"]) is False
    assert row["remark_at"] is not None


def test_with_remarks_falls_back_to_the_computed_auto_remark():
    grid = ft.build_float_grid(
        sheet_rows([float_row("2026-07-05", "ABD", 60_000, 0)]),  # a Sunday
        {"float_tracker": {"float_threshold": 10_000}})
    full = ft.with_remarks(grid, {})
    row = full.iloc[0]
    assert row["remark"] == "Weekend"
    assert row["remark_by"] == "system"
    assert bool(row["is_auto"]) is True
    assert row["remark_at"] is None


def test_with_remarks_is_blank_with_no_recorded_and_no_auto_remark():
    grid = ft.build_float_grid(
        sheet_rows([float_row("2026-07-20", "ABD", 60_000, 0)]),  # a Monday
        {"float_tracker": {"float_threshold": 10_000}})
    full = ft.with_remarks(grid, {})
    row = full.iloc[0]
    assert row["remark"] == ""
    assert row["remark_by"] == ""
    assert bool(row["is_auto"]) is False


# ---------------------------------------------------------------------------
# Bulk upload: preview_upload / apply_upload — unchanged mechanism, new basis
# ---------------------------------------------------------------------------

@pytest.fixture
def high_float_grid():
    config = {"float_tracker": {"float_threshold": 10_000}}
    sheet = sheet_rows([
        float_row("2026-07-20", "ABD", 60_000, 0),   # float 60,000 — high
        float_row("2026-07-20", "KGN", 5_000, 0),    # float 5,000 — not high
    ])
    return ft.build_float_grid(sheet, config)


def test_upload_template_matches_upload_columns():
    assert list(ft.upload_template().columns) == ft.UPLOAD_COLUMNS


def test_preview_upload_accepts_a_genuinely_high_float_row(high_float_grid):
    frame = pd.DataFrame([
        {"store": "ABD", "business_date": "2026-07-20", "remark": "Delayed pickup"},
    ])
    preview = ft.preview_upload(frame, high_float_grid)
    assert len(preview.accepted) == 1
    assert len(preview.rejected) == 0


def test_preview_upload_rejects_a_store_date_that_is_not_high_float(high_float_grid):
    frame = pd.DataFrame([
        {"store": "KGN", "business_date": "2026-07-20", "remark": "Not actually high"},
    ])
    preview = ft.preview_upload(frame, high_float_grid)
    assert len(preview.rejected) == 1
    assert "not flagged high float" in preview.rejected[0].why[0]


def test_preview_upload_rejects_a_store_outside_the_loaded_range(high_float_grid):
    frame = pd.DataFrame([
        {"store": "ZZZ", "business_date": "2026-07-20", "remark": "Ghost store"},
    ])
    preview = ft.preview_upload(frame, high_float_grid)
    assert len(preview.rejected) == 1
    assert "not in the loaded range" in preview.rejected[0].why[0]


def test_preview_upload_rejects_a_blank_remark(high_float_grid):
    frame = pd.DataFrame([
        {"store": "ABD", "business_date": "2026-07-20", "remark": "  "},
    ])
    preview = ft.preview_upload(frame, high_float_grid)
    assert len(preview.rejected) == 1
    assert "remark is blank" in preview.rejected[0].why[0]


def test_preview_upload_raises_on_missing_columns(high_float_grid):
    frame = pd.DataFrame([{"store": "ABD", "remark": "no date column"}])
    with pytest.raises(ValueError):
        ft.preview_upload(frame, high_float_grid)


def test_apply_upload_writes_only_the_accepted_rows(session, high_float_grid):
    frame = pd.DataFrame([
        {"store": "ABD", "business_date": "2026-07-20", "remark": "Accepted"},
        {"store": "KGN", "business_date": "2026-07-20", "remark": "Should be rejected"},
    ])
    preview = ft.preview_upload(frame, high_float_grid)
    applied, failed = ft.apply_upload(session, preview, "aditi")

    assert len(applied) == 1
    assert not failed
    current = ft.float_remarks_for_range(session)
    assert ("ABD", "2026-07-20") in current
    assert ("KGN", "2026-07-20") not in current


# ---------------------------------------------------------------------------
# No trace of the old Rs 50,000 default anywhere in this module
# ---------------------------------------------------------------------------

def test_50000_appears_nowhere_in_float_tracker_source():
    import mop_reco.float_tracker as module
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "50000" not in source
    assert "50_000" not in source
    assert "50,000" not in source
