"""Formatting tests — the numbers on screen are the product, so they get tests."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco.formatting import (
    blank,
    count,
    date_range_label,
    display_date,
    indian_group,
    money_cr,
    money_short,
    money_table,
    percent,
)


@pytest.mark.parametrize("raw,expected", [
    (0, "0.00"),
    (5, "5.00"),
    (999, "999.00"),
    (1000, "1,000.00"),
    (99999, "99,999.00"),
    (100000, "1,00,000.00"),          # one lakh — grouping switches to pairs
    (1234567.5, "12,34,567.50"),
    (10000000, "1,00,00,000.00"),     # one crore
    (541400988.21, "54,14,00,988.21"),
    (-359310, "-3,59,310.00"),
])
def test_indian_group(raw, expected):
    assert indian_group(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (541400988.21, "Rs 54.14 Cr"),
    (229238, "Rs 2.29 L"),
    (61996, "Rs 61,996"),
    (641422.55, "Rs 6.41 L"),
    (999, "Rs 999"),
    (-359310, "-Rs 3.59 L"),
    (0, "Rs 0"),
])
def test_money_short_uses_indian_units(raw, expected):
    assert money_short(raw) == expected


def test_money_table_is_full_number_not_short_form():
    assert money_table(541400988.21) == "54,14,00,988.21"
    assert money_table(226055) == "2,26,055.00"


# ---------------------------------------------------------------------------
# money_cr — ALWAYS crores, never switches to Lakh for a small value (unlike
# money_short) so a whole table stays one consistent unit (2026-09-21, the
# Summary hero band and particulars matrix).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (1031045827.00, "Rs 103.10 Cr"),
    (221914.00, "Rs 0.02 Cr"),
    (0, "Rs 0.00 Cr"),
    (-268592.83 * 1000, "-Rs 26.86 Cr"),
])
def test_money_cr_always_shows_crores(raw, expected):
    assert money_cr(raw) == expected


def test_money_cr_never_switches_to_lakh():
    # money_short would render this as "Rs 2.29 L" — money_cr keeps the
    # unit fixed at Cr even for a cell far below one crore.
    assert money_cr(229238) == "Rs 0.02 Cr"
    assert "L" not in money_cr(229238)


def test_money_never_renders_as_a_bare_integer():
    """A money figure always carries grouping and either a unit or decimals."""
    for value in (61996, 226055, 1000):
        assert money_table(value) != str(value)
        assert "," in money_table(value)
        assert money_short(value).startswith("Rs ")


def test_counts_are_not_money():
    assert count(660150) == "660,150"
    assert "Rs" not in count(660150)
    assert count(850) == "850"


def test_none_is_an_em_dash_not_zero():
    """A missing figure must not read as a real zero."""
    assert money_table(None) == "—"
    assert money_short(None) == "—"
    assert count(None) == "—"


def test_percent():
    assert percent(659945, 660150) == "99.97%"
    assert percent(1, 0) == "—"


def test_date_range_label():
    assert date_range_label(date(2026, 7, 1), date(2026, 7, 31)) == "1-31 July"
    assert date_range_label(date(2026, 7, 20), date(2026, 7, 20)) == "20 July"


# ---------------------------------------------------------------------------
# display_date — the one shared DD/MM/YYYY helper (slashes since 2026-09-21).
# Every displayed/exported date goes through this; dates are stored as a
# date type internally, only the display changes.
# ---------------------------------------------------------------------------

def test_display_date_formats_a_date_object():
    assert display_date(date(2026, 7, 20)) == "20/07/2026"


def test_display_date_formats_an_iso_string():
    assert display_date("2026-07-20") == "20/07/2026"


def test_display_date_formats_a_pandas_timestamp():
    import pandas as pd
    assert display_date(pd.Timestamp("2026-07-20")) == "20/07/2026"


def test_display_date_never_shows_iso_form():
    assert display_date("2026-07-20") != "2026-07-20"


@pytest.mark.parametrize("value", [None, "", "  "])
def test_display_date_blank_input_is_empty_not_a_placeholder(value):
    assert display_date(value) == ""


def test_display_date_of_nat_is_empty():
    import pandas as pd
    assert display_date(pd.NaT) == ""


# ---------------------------------------------------------------------------
# blank — a null must render as an empty cell, never the literal "nan"
# (Request B, part 1's fix instruction).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, float("nan")])
def test_blank_of_null_is_empty(value):
    assert blank(value) == ""


def test_blank_never_shows_the_literal_string_nan():
    import pandas as pd
    assert blank(pd.NA) == ""
    assert blank(float("nan")) == ""


def test_blank_passes_through_a_real_value():
    assert blank("bulk_upload") == "bulk_upload"
    assert blank(0) == "0"
