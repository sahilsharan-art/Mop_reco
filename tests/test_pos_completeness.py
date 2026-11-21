"""The POS completeness check: Excel's row limit and gaps in the consecutive bill numbers (2026-11-21)."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import pos_completeness as pc


def _month(month="2026-11", stores=("ABD", "XYZ"), per_store=400, drop=0.0, seed=1, suffix="1126"):
    rng = random.Random(seed)
    rows = []
    for s, store in enumerate(stores):
        for seq in range(1000, 1000 + per_store):
            if rng.random() < drop and seq not in (1000, 999 + per_store):      # the ends stay: they define the range
                continue
            rows.append({"store": store, "bill_no": f"SA{s}/{seq:06d}/{suffix}", "business_date": f"{month}-{1 + seq % 28:02d}"})
    return pd.DataFrame(rows)


def test_a_complete_month_is_ok():
    r = pc.assess(_month())
    assert r["ok"] is True and r["flagged_months"] == [] and pc.warning_lines(r) == []
    assert r["by_month"]["2026-11"]["missing"] == 0


def test_a_month_with_a_fifth_of_its_bills_missing_is_flagged_and_the_worst_stores_are_named():
    r = pc.assess(_month(drop=0.2))
    m = r["by_month"]["2026-11"]
    assert r["ok"] is False and r["flagged_months"] == ["2026-11"]
    assert 15 < m["missing_pct"] < 25 and m["expected"] == 800 and m["present"] + m["missing"] == 800
    assert {w["store"] for w in r["worst_stores"]["2026-11"]} == {"ABD", "XYZ"}
    line = pc.warning_lines(r)[0]
    assert "2026-11" in line and "missing" in line and "unmapped" in line


def test_a_normal_trickle_of_missing_numbers_is_not_flagged():
    r = pc.assess(_month(per_store=2000, drop=0.002))
    assert r["ok"] is True, r["by_month"]


def test_months_are_judged_separately():
    df = pd.concat([_month("2026-10", suffix="1026"), _month("2026-11", drop=0.25, suffix="1126")])
    r = pc.assess(df)
    assert r["flagged_months"] == ["2026-11"] and r["by_month"]["2026-10"]["missing"] == 0


def test_bill_numbers_that_do_not_look_like_series_sequence_suffix_are_ignored_not_fatal():
    df = pd.DataFrame({"store": ["ABD", "ABD", "ABD"], "bill_no": ["ODD-1", None, "SA1/000010/1126"],
                       "business_date": ["2026-11-01"] * 3})
    r = pc.assess(df)
    assert r["ok"] is True and r["by_month"]["2026-11"]["expected"] == 1


def test_an_empty_frame_is_ok():
    assert pc.assess(pd.DataFrame(columns=["store", "bill_no", "business_date"]))["ok"] is True


def test_excel_row_limit_only_counts_a_FULL_LAST_sheet():
    full = pc.EXCEL_SHEET_ROW_LIMIT - 1                       # data rows when the header takes the first row
    assert pc.excel_row_limit_hits({"nov.xlsx": {"Sheet1": full}})[0]["data_rows"] == full
    # a month legitimately spread over sheets: the FIRST is full by design, the last is not
    assert pc.excel_row_limit_hits({"jul.xlsb": {"Sheet1": full, "Sheet1 (2)": 698_975}}) == []
    assert pc.excel_row_limit_hits({"oct.xlsx": {"Data": 792_270, "Data(2)": 916_037}}) == []
    # ...but if the LAST of several sheets is full, the export probably went on
    assert len(pc.excel_row_limit_hits({"big.xlsx": {"A": full, "B": full}})) == 1
    assert pc.excel_row_limit_hits({}) == [] and pc.excel_row_limit_hits({"x": {}}) == []


def test_the_row_limit_alone_makes_the_assessment_not_ok():
    r = pc.assess(_month(), {"nov.xlsx": {"Pos sale Nov": pc.EXCEL_SHEET_ROW_LIMIT - 1}})
    assert r["ok"] is False and "Excel's" in pc.warning_lines(r)[0]
