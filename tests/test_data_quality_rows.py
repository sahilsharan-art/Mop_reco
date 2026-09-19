"""store_sheet_source.data_quality_rows — the Defect B list behind the
Summary page's "Data quality" expander (2026-09-13). Every store-date where
a credit exceeds its deposit, with the CMS slip(s) that fed it attached.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_sheet as ss
from mop_reco import store_sheet_source as sss


def sheet_with_one_excess_row():
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 0.0}])
    ptc = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                        "amount": -100.0, "is_deposit": True}])
    cms_raw = pd.DataFrame([{
        "TYPE": "COL", "store": "ABD", "DEPDATE": "2026-07-01",
        "CRDATE": "2026-07-01", "SLIPAMOUNT": 1_500.0, "ADJ": 0.0,
        "RECOVEREDAMOUNT": 0.0, "PICKUPPOINTCODE": "AX99999", "SLIPNO": "S1",
    }])
    cms_raw["DEPDATE"] = pd.to_datetime(cms_raw["DEPDATE"])
    return ss.build_sheet(
        opening, ss.digital_by_store_date(pd.DataFrame(columns=[
            "business_date", "store", "amount", "matched"])),
        ss.cash_sale_by_store_date(pd.DataFrame(columns=[
            "business_date", "store", "bill_no", "mop", "amount"])),
        ss.ptc_by_store_date(ptc),
        ss.cms_received_by_deposit_date(cms_raw),
        date(2026, 7, 1), date(2026, 7, 1),
    ), cms_raw


def test_data_quality_rows_carries_pickup_code_and_slip_no():
    sheet, cms_raw = sheet_with_one_excess_row()
    dq = sss.data_quality_rows(sheet, cms_raw)
    assert len(dq) == 1
    row = dq.iloc[0]
    assert row["store"] == "ABD"
    assert row["business_date"] == "2026-07-01"
    assert row["I"] == 100.0            # positive magnitude, not -100.0
    assert row["J"] == 1_500.0
    assert row["J_minus_I"] == 1_400.0
    assert row["pickup_code"] == "AX99999"
    assert row["slip_no"] == "S1"


def test_data_quality_rows_empty_when_nothing_exceeds():
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 0.0}])
    ptc = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                        "amount": -9_500.0, "is_deposit": True}])
    sheet = ss.build_sheet(
        opening, ss.digital_by_store_date(pd.DataFrame(columns=[
            "business_date", "store", "amount", "matched"])),
        ss.cash_sale_by_store_date(pd.DataFrame(columns=[
            "business_date", "store", "bill_no", "mop", "amount"])),
        ss.ptc_by_store_date(ptc),
        ss.cms_received_by_deposit_date(pd.DataFrame()),
        date(2026, 7, 1), date(2026, 7, 1),
    )
    dq = sss.data_quality_rows(sheet, pd.DataFrame())
    assert dq.empty
    assert list(dq.columns) == ["store", "business_date", "I", "J",
                                "J_minus_I", "pickup_code", "slip_no"]


def test_data_quality_rows_sorted_descending_by_J_minus_I():
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 0.0},
                           {"store": "XYZ", "opening_cash": 0.0}])
    ptc = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "amount": -100.0, "is_deposit": True},
        {"business_date": "2026-07-01", "store": "XYZ", "amount": -100.0, "is_deposit": True},
    ])
    cms_raw = pd.DataFrame([
        {"TYPE": "COL", "store": "ABD", "DEPDATE": "2026-07-01", "CRDATE": "2026-07-01",
         "SLIPAMOUNT": 200.0, "ADJ": 0.0, "RECOVEREDAMOUNT": 0.0,
         "PICKUPPOINTCODE": "AX1", "SLIPNO": "S1"},
        {"TYPE": "COL", "store": "XYZ", "DEPDATE": "2026-07-01", "CRDATE": "2026-07-01",
         "SLIPAMOUNT": 900.0, "ADJ": 0.0, "RECOVEREDAMOUNT": 0.0,
         "PICKUPPOINTCODE": "AX2", "SLIPNO": "S2"},
    ])
    cms_raw["DEPDATE"] = pd.to_datetime(cms_raw["DEPDATE"])
    sheet = ss.build_sheet(
        opening, ss.digital_by_store_date(pd.DataFrame(columns=[
            "business_date", "store", "amount", "matched"])),
        ss.cash_sale_by_store_date(pd.DataFrame(columns=[
            "business_date", "store", "bill_no", "mop", "amount"])),
        ss.ptc_by_store_date(ptc),
        ss.cms_received_by_deposit_date(cms_raw),
        date(2026, 7, 1), date(2026, 7, 1),
    )
    dq = sss.data_quality_rows(sheet, cms_raw)
    assert list(dq["store"]) == ["XYZ", "ABD"]
