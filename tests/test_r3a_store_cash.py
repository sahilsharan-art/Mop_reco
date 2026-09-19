"""R3a — the cash book, its carry-forward, and the double-count test it
depends on."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import r3a_store_cash as r3a


def pos_rows(rows):
    return pd.DataFrame(rows, columns=["business_date", "store", "bill_no", "mop", "amount"])


def ptc_rows(rows):
    df = pd.DataFrame(rows, columns=["business_date", "store", "amount", "is_deposit"])
    df["deposit_amount"] = 0.0
    df.loc[df["is_deposit"], "deposit_amount"] = -df.loc[df["is_deposit"], "amount"]
    return df


# ---------------------------------------------------------------------------
# The components
# ---------------------------------------------------------------------------

def test_cash_sale_excludes_rows_with_no_bill_number():
    """§6.13: a row with no BILLNO is not a bill. It is also the PTC ledger
    mirrored into the POS export, so counting it here double-counts."""
    pos = pos_rows([
        ("2026-07-01", "ABD", "SB58/000001/0726", "Cash", 500.0),
        ("2026-07-01", "ABD", None, "Cash", -265135.0),
        ("2026-07-01", "ABD", "", "Cash", 1478.0),
        ("2026-07-01", "ABD", "SB58/000002/0726", "PAYTM EDC", 999.0),
    ])
    out = r3a.cash_sale_by_store_date(pos)
    assert len(out) == 1
    assert out.iloc[0]["cash_sale"] == 500.0   # cash bill only


def test_net_ptc_excludes_deposit_vouchers():
    """The deposit is subtracted by its own term. Counting those rows in
    net_ptc as well subtracts every deposit twice — Rs 48.99 Cr on full July."""
    ptc = ptc_rows([
        ("2026-07-01", "ABD", -185.0, False),     # an expense
        ("2026-07-01", "ABD", 40.0, False),      # income (RECEIVE)
        ("2026-07-01", "ABD", -56003.0, True),    # a deposit
    ])
    net = r3a.net_ptc_by_store_date(ptc)
    assert net.iloc[0]["net_ptc"] == -145.0
    deposits = r3a.cms_deposit_by_store_date(ptc)
    assert deposits.iloc[0]["cms_deposit"] == 56003.0   # sign-flipped positive


def test_income_expense_split_is_information_only_and_nets_to_net_ptc():
    ptc = ptc_rows([
        ("2026-07-01", "ABD", -300.0, False),
        ("2026-07-01", "ABD", 100.0, False),
        ("2026-07-01", "ABD", -56003.0, True),
    ])
    split = r3a.ptc_income_expense_split(ptc)
    assert split == {"income": 100.0, "expense": -300.0, "net": -200.0}
    assert split["net"] == r3a.net_ptc_by_store_date(ptc).iloc[0]["net_ptc"]


# ---------------------------------------------------------------------------
# The double-count test (the check demanded before trusting any figure)
# ---------------------------------------------------------------------------

def test_overlap_detects_the_same_ledger_counted_twice():
    """The real 20-Jul case, in miniature: the POS no-bill rows ARE the PTC
    rows. Identical count, total and per-store net."""
    pos = pos_rows([
        ("2026-07-20", "ABD", "SB58/1/0726", "Cash", 500.0),   # a real bill
        ("2026-07-20", "ABD", None, "Cash", -265135.0),
        ("2026-07-20", "GDA", None, "Cash", -234780.0),
        ("2026-07-20", "MTH", None, "Cash", 1478.0),
    ])
    ptc = ptc_rows([
        ("2026-07-20", "ABD", -265135.0, True),
        ("2026-07-20", "GDA", -234780.0, True),
        ("2026-07-20", "MTH", 1478.0, False),
    ])
    result = r3a.overlap_with_ptc(pos, ptc)
    assert result.pos_rows == 3 and result.ptc_rows == 3
    assert result.pos_total == result.ptc_total
    assert result.stores_agreeing == result.stores_compared == 3
    assert result.movements_matched == 3
    assert result.overlaps is True
    assert "SAME MOVEMENTS" in result.verdict


def test_overlap_reports_an_empty_population_rather_than_claiming_no_overlap():
    """Full July has zero no-BILLNO rows, so the test cannot fire. That must be
    reported as 'the population is empty', never as a clean pass."""
    pos = pos_rows([("2026-07-01", "ABD", "SB58/1/0726", "Cash", 500.0)])
    result = r3a.overlap_with_ptc(pos, ptc_rows([("2026-07-01", "ABD", -185.0, False)]))
    assert result.pos_rows == 0
    assert result.overlaps is False
    assert "cannot overlap" in result.verdict


def test_overlap_says_no_when_the_populations_genuinely_differ():
    pos = pos_rows([("2026-07-20", "ABD", None, "Cash", -100.0)])
    ptc = ptc_rows([("2026-07-20", "ABD", -999.0, False),
                    ("2026-07-20", "GDA", -5.0, False)])
    result = r3a.overlap_with_ptc(pos, ptc)
    assert result.overlaps is False
    assert "not simply the same ledger twice" in result.verdict


# ---------------------------------------------------------------------------
# The cash book and its carry-forward
# ---------------------------------------------------------------------------

@pytest.fixture
def book():
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 76938.0}])
    cash_sale = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "cash_sale": 82608.0},
        {"business_date": "2026-07-02", "store": "ABD", "cash_sale": 50000.0},
    ])
    net_ptc = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "net_ptc": -185.0},
    ])
    deposits = pd.DataFrame([
        {"business_date": "2026-07-02", "store": "ABD", "cms_deposit": 100000.0},
    ])
    return (opening,
            r3a.build_cash_book(opening, cash_sale, net_ptc, deposits,
                                date(2026, 7, 1), date(2026, 7, 2)))


def test_first_day_uses_the_opening_balance_file(book):
    _, table = book
    day1 = table[table["business_date"] == "2026-07-01"].iloc[0]
    assert day1["opening_cash"] == 76938.0
    # 76,938 + 82,608 - 185 - 0 = 159,361 — the section 7 ABD acceptance figure
    assert day1["closing_cash"] == 159361.0


def test_later_days_open_on_the_previous_closing(book):
    _, table = book
    day2 = table[table["business_date"] == "2026-07-02"].iloc[0]
    assert day2["opening_cash"] == 159361.0
    assert day2["closing_cash"] == 159361.0 + 50000.0 - 100000.0


def test_a_day_with_no_movement_is_a_row_not_a_gap():
    """Rule 6: a missing input is a gap, never filled from a neighbour. Here a
    store-day with no movement carries forward and stays visible."""
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 1000.0}])
    empty = pd.DataFrame(columns=["business_date", "store", "cash_sale"])
    table = r3a.build_cash_book(opening, empty, empty, empty,
                                date(2026, 7, 1), date(2026, 7, 3))
    assert len(table) == 3
    assert list(table["closing_cash"]) == [1000.0, 1000.0, 1000.0]


def test_a_store_with_no_opening_balance_starts_at_zero_not_dropped():
    """Three July stores (BGP, KGT, RSG) are absent from the opening file.
    They must still appear, seeded at zero, rather than vanish."""
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 1000.0}])
    sale = pd.DataFrame([{"business_date": "2026-07-01", "store": "NEW",
                          "cash_sale": 5.0}])
    empty = pd.DataFrame(columns=["business_date", "store", "cash_sale"])
    table = r3a.build_cash_book(opening, sale, empty, empty,
                                date(2026, 7, 1), date(2026, 7, 1))
    assert set(table["store"]) == {"ABD", "NEW"}
    new = table[table["store"] == "NEW"].iloc[0]
    assert new["opening_cash"] == 0.0 and new["closing_cash"] == 5.0


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def test_estate_identity_holds_with_nil_tolerance(book):
    opening, table = book
    identity = r3a.estate_identity(table, opening, date(2026, 7, 1), date(2026, 7, 2))
    assert identity["holds"] is True
    assert identity["difference"] == 0.0
    assert identity["opening_total"] == identity["opening_from_file"]


def test_carry_forward_chain_is_unbroken(book):
    _, table = book
    assert len(r3a.carry_forward_breaks(table, date(2026, 7, 1))) == 0


def test_a_tampered_opening_is_caught_as_a_carry_forward_break(book):
    _, table = book
    table = table.copy()
    table.loc[table["business_date"] == "2026-07-02", "opening_cash"] = 1.0
    breaks = r3a.carry_forward_breaks(table, date(2026, 7, 1))
    assert len(breaks) == 1
    assert breaks.iloc[0]["previous_closing"] == 159361.0


def test_negative_closing_is_always_reported():
    """A store cannot hold negative cash — a missing sale, an overstated
    deposit or a mapping error."""
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 100.0}])
    deposits = pd.DataFrame([{"business_date": "2026-07-01", "store": "ABD",
                              "cms_deposit": 5000.0}])
    empty = pd.DataFrame(columns=["business_date", "store", "cash_sale"])
    table = r3a.build_cash_book(opening, empty, empty, deposits,
                                date(2026, 7, 1), date(2026, 7, 1))
    hits = r3a.negative_closing(table)
    assert len(hits) == 1
    assert hits.iloc[0]["closing_cash"] == -4900.0


def test_high_cash_threshold_is_configurable():
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 250000.0}])
    empty = pd.DataFrame(columns=["business_date", "store", "cash_sale"])
    table = r3a.build_cash_book(opening, empty, empty, empty,
                                date(2026, 7, 1), date(2026, 7, 1))
    assert len(r3a.high_cash(table, 200_000.0)) == 1
    assert len(r3a.high_cash(table, 300_000.0)) == 0


def test_a_day_the_agency_collected_from_nobody_does_not_blame_the_store():
    """The three July Sundays have zero estate-wide deposits and Mondays run at
    double. Counting those as the store failing to deposit turned 11 genuine
    runs into 88."""
    opening = pd.DataFrame([{"store": "ABD", "opening_cash": 0.0}])
    sale = pd.DataFrame([{"business_date": f"2026-07-0{d}", "store": "ABD",
                          "cash_sale": 1000.0} for d in (1, 2, 3, 4)])
    # The agency collects on the 1st and the 4th; nobody is collected from on
    # the 2nd and 3rd.
    deposits = pd.DataFrame([
        {"business_date": "2026-07-01", "store": "ABD", "cms_deposit": 1000.0},
        {"business_date": "2026-07-04", "store": "ABD", "cms_deposit": 1000.0},
    ])
    table = r3a.build_cash_book(opening, sale, sale.assign(net_ptc=0.0)
                                .drop(columns=["cash_sale"]), deposits,
                                date(2026, 7, 1), date(2026, 7, 4))
    skipped = r3a.agency_non_collection_dates(table)
    assert skipped == {"2026-07-02", "2026-07-03"}
    # With those days ignored, the store has no dry run at all.
    assert len(r3a.no_deposit_runs(table, min_days=2)) == 0
    # Counting raw calendar days instead would flag it.
    assert len(r3a.no_deposit_runs(table, min_days=2, ignore_dates=set())) == 1


def test_r3a_states_that_it_has_no_independent_confirmation():
    """Step 3: do not invent a comparison we do not have."""
    assert "COMPUTED cash book" in r3a.NO_INDEPENDENT_CONFIRMATION
    assert "no daily declared cash balance" in r3a.NO_INDEPENDENT_CONFIRMATION
