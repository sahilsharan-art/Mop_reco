"""The daily store-wise sale and cash sheet.

Two rules decide whether this sheet is right, and both are tested from the
direction that would break them:

1. Column J belongs to the DEPOSIT date, not the credit date. A T+1 credit
   attributed to CRDATE would make K read nil on the deposit day and show a
   phantom positive on the credit day.
2. J counts TYPE = COL only, but ADJ.CR./ADJ.DB./ZPO are reported rather than
   discarded — otherwise an adjustment and its later recovery cancel where
   nobody is looking.

**Column I is stored POSITIVE (2026-09-17)** — a deposit is a positive
amount of money — with K = I - J, M = H + E - I + L, N = H - I restated to
match. K's own polarity swapped with it: a POSITIVE K is now cash in
transit (deposited, not yet credited) and a NEGATIVE K is Defect B (credit
exceeds deposit) — the reverse of the convention this suite tested before
that date. The PTC raw ledger's own sign is untouched: a PAYMENT (deposit)
row still arrives negative from the source file; it is `ptc_by_store_date()`
that now negates it into a positive `cash_deposit`.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_sheet as ss


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def opening(rows):
    return pd.DataFrame(rows, columns=["store", "opening_cash"])


def bills(rows):
    return pd.DataFrame(rows, columns=["business_date", "store", "amount",
                                       "matched"])


def pos(rows):
    return pd.DataFrame(rows, columns=["business_date", "store", "bill_no",
                                       "mop", "amount"])


def ptc(rows):
    return pd.DataFrame(rows, columns=["business_date", "store", "amount",
                                       "is_deposit"])


def cms(rows, adj=None, recovered=None):
    """`adj` and `recovered` are optional parallel lists for the ADJ and
    RECOVEREDAMOUNT columns, so most tests (which only care about COL /
    SLIPAMOUNT) do not have to pad every row with zeros."""
    frame = pd.DataFrame(rows, columns=["TYPE", "store", "DEPDATE", "CRDATE",
                                        "SLIPAMOUNT"])
    frame["ADJ"] = adj if adj is not None else 0.0
    frame["RECOVEREDAMOUNT"] = recovered if recovered is not None else 0.0
    return frame


def sheet_for(opening_frame=None, bills_frame=None, pos_frame=None,
              ptc_frame=None, cms_frame=None,
              lo=date(2026, 7, 1), hi=date(2026, 7, 1)):
    return ss.build_sheet(
        opening_frame if opening_frame is not None else opening([]),
        ss.digital_by_store_date(bills_frame if bills_frame is not None
                                 else bills([])),
        ss.cash_sale_by_store_date(pos_frame if pos_frame is not None
                                   else pos([])),
        ss.ptc_by_store_date(ptc_frame if ptc_frame is not None else ptc([])),
        ss.cms_received_by_deposit_date(cms_frame if cms_frame is not None
                                        else cms([])),
        lo, hi)


# ---------------------------------------------------------------------------
# The format itself
# ---------------------------------------------------------------------------

def test_the_columns_are_A_to_N_in_the_exact_order():
    letters = [letter for letter, _, _ in ss.COLUMNS]
    assert letters == list("ABCDEFGHIJKLMN")
    assert [label for _, _, label in ss.COLUMNS] == [
        "Date", "Store", "Total sale", "Digital sale", "Cash sale",
        "Digital received", "Digital receivable", "Opening balance",
        "Cash deposit", "Cash received", "Cash receivable",
        "Cash expense/income", "Closing balance", "Float amount"]


def test_the_sheet_frame_carries_those_columns_in_that_order():
    table = sheet_for(opening([("ABD", 1000.0)]))
    assert list(table.columns) == ss.FIELDS


def test_credit_notes_are_nowhere_in_the_sheet():
    """Total sale is digital plus cash only."""
    assert not any("credit" in name and "note" in name for name in ss.FIELDS)
    table = sheet_for(
        opening([("ABD", 0.0)]),
        pos_frame=pos([("2026-07-01", "ABD", "B1", "Cash", 500.0),
                       ("2026-07-01", "ABD", "B2", "Credit Note Issued", 999.0)]),
        bills_frame=bills([("2026-07-01", "ABD", 300.0, True)]))
    row = table.iloc[0]
    # 999 of credit notes is in neither D nor E, so C is 300 + 500.
    assert row["cash_sale"] == 500.0
    assert row["digital_sale"] == 300.0
    assert row["total_sale"] == 800.0


# ---------------------------------------------------------------------------
# The derived columns
# ---------------------------------------------------------------------------

def test_the_five_derived_columns_are_computed_as_specified():
    table = sheet_for(
        opening([("ABD", 10_000.0)]),
        bills_frame=bills([("2026-07-01", "ABD", 700.0, True),
                           ("2026-07-01", "ABD", 300.0, False)]),
        pos_frame=pos([("2026-07-01", "ABD", "B1", "Cash", 5_000.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -4_000.0, True),
                       ("2026-07-01", "ABD", -100.0, False)]),
        cms_frame=cms([("COL", "ABD", "2026-07-01", "2026-07-02", 4_000.0)]))
    row = table.iloc[0]
    assert row["digital_sale"] == 1_000.0                 # D
    assert row["digital_received"] == 700.0               # F
    assert row["cash_sale"] == 5_000.0                    # E
    assert row["total_sale"] == 6_000.0                   # C = D + E
    assert row["digital_receivable"] == 300.0             # G = D - F
    assert row["opening_balance"] == 10_000.0             # H
    assert row["cash_deposit"] == 4_000.0                 # I, positive
    assert row["cash_received"] == 4_000.0                # J, positive
    assert row["cash_receivable"] == 0.0                  # K = I - J -> nil
    assert row["cash_expense_income"] == -100.0           # L
    assert row["closing_balance"] == 10_900.0             # M = H+E-I+L
    assert row["float_amount"] == 6_000.0                 # N = H - I


def test_the_deposit_is_carried_positive_and_the_credit_positive():
    """The operator's sign convention (2026-09-17), and the reason K nets to
    nil once a deposit is fully credited."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -5_000.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-01", "2026-07-01", 5_000.0)]))
    row = table.iloc[0]
    assert row["cash_deposit"] > 0
    assert row["cash_received"] > 0
    assert row["cash_receivable"] == 0.0


def test_an_uncredited_deposit_leaves_a_non_nil_K():
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -5_000.0, True)]))
    assert table.iloc[0]["cash_receivable"] == 5_000.0


def test_L_excludes_the_deposit_head():
    """I already carries the deposit. Counting it in L as well would subtract
    every deposit twice — Rs 48.99 Cr on full July."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -9_000.0, True),
                       ("2026-07-01", "ABD", -50.0, False),
                       ("2026-07-01", "ABD", 20.0, False)]))
    row = table.iloc[0]
    assert row["cash_deposit"] == 9_000.0
    assert row["cash_expense_income"] == -30.0
    assert row["closing_balance"] == -9_030.0


def test_cash_sale_excludes_a_row_with_no_bill_number():
    """§6.13 — and those rows are the PTC ledger mirrored into the POS export,
    so counting them here would double-count against column L."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        pos_frame=pos([("2026-07-01", "ABD", "B1", "Cash", 500.0),
                       ("2026-07-01", "ABD", None, "Cash", -265_135.0),
                       ("2026-07-01", "ABD", "", "Cash", 1_478.0)]))
    assert table.iloc[0]["cash_sale"] == 500.0


# ---------------------------------------------------------------------------
# RULE 1 — J belongs to the deposit date
# ---------------------------------------------------------------------------

def test_a_T_plus_1_credit_lands_on_the_DEPOSIT_date():
    """Deposited 1 July, credited 2 July. The credit belongs to the 1 July row,
    so K is nil there and the 2 July row shows no phantom positive."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -7_000.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-01", "2026-07-02", 7_000.0)]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 2))
    first = table[table["date"] == "2026-07-01"].iloc[0]
    second = table[table["date"] == "2026-07-02"].iloc[0]
    assert first["cash_received"] == 7_000.0
    assert first["cash_receivable"] == 0.0
    assert second["cash_received"] == 0.0, "the credit date must not pull it forward"
    assert second["cash_receivable"] == 0.0


def test_attributing_to_the_credit_date_would_be_wrong_and_is_not_what_happens():
    """The failure this rule prevents: K non-nil on the deposit day and a
    positive K on the credit day, which reads as money received twice."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -7_000.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-01", "2026-07-02", 7_000.0)]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 2))
    # If J had been grouped on CRDATE these would be +7,000 and -7,000.
    assert list(table.sort_values("date")["cash_receivable"]) == [0.0, 0.0]


def test_a_month_end_T_plus_1_deposit_shows_a_real_non_zero_K():
    """A deposit on the last loaded date whose credit falls outside the range.
    That is cash in transit, and it must NOT be suppressed."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-31", "ABD", -9_500.0, True)]),
        cms_frame=cms([]),          # the credit lands in August, not loaded
        lo=date(2026, 7, 31), hi=date(2026, 7, 31))
    row = table.iloc[0]
    assert row["cash_receivable"] == 9_500.0
    tail = ss.in_transit_tail(table, on_date="2026-07-31")
    assert len(tail) == 1
    # Already a positive magnitude — no cash receivable figure displays
    # negative anywhere; the sheet's own column stays signed above.
    assert tail.iloc[0]["cash_receivable"] == 9_500.0


def test_in_transit_tail_excludes_a_credit_exceeding_its_deposit():
    """K < 0 (polarity swapped 2026-09-17 with column I's sign) is a Defect B
    artefact, not cash in transit — it must never appear in this list, even
    though it is also non-zero."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-31", "ABD", -1_000.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-31", "2026-07-31", 1_500.0)]),
        lo=date(2026, 7, 31), hi=date(2026, 7, 31))
    row = table.iloc[0]
    assert row["cash_receivable"] == -500.0
    assert ss.in_transit_tail(table, on_date="2026-07-31").empty


def test_the_in_transit_tail_is_empty_when_everything_is_credited():
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-31", "ABD", -9_500.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-31", "2026-08-01", 9_500.0)]),
        lo=date(2026, 7, 31), hi=date(2026, 7, 31))
    assert ss.in_transit_tail(table).empty


# ---------------------------------------------------------------------------
# Cash receivable — NET, floored at zero, never a sum of pre-floored rows
# (2026-09-13). "There is no such thing as excess credit" — a K<0 store-date
# (polarity swapped 2026-09-17 with column I's sign) is a Defect B artefact,
# reported separately, never its own tile.
# ---------------------------------------------------------------------------

def test_net_cash_receivable_nets_before_flooring_not_after():
    """The whole point: a store with one receivable day and one (bigger, in
    magnitude, artefact) excess day must net across BOTH before flooring —
    summing only the positive-K (apparently-receivable) day, the naive
    approach, would report a receivable that does not exist."""
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -1_000.0, True),
                      ("2026-07-02", "ABD", -100.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-02", "2026-07-02", 1_600.0)]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 2))
    # Day 1: I=1000, J=0, K=+1000 (a receivable). Day 2: I=100, J=1600,
    # K=-1500 (an "excess" artefact). Net over the two days: I=1100, J=1600,
    # K=-500 -> net_cash_receivable floors this to 0, NOT to 1000 (which is
    # what summing "positive K only" would have wrongly reported).
    assert float(table["cash_receivable"].sum()) == -500.0
    assert ss.net_cash_receivable(table) == 0.0
    # The naive computation for comparison — this is exactly the bug being
    # fixed: it would report a receivable that the range does not have.
    naive = float(table.loc[table["cash_receivable"] > 0, "cash_receivable"].sum())
    assert naive == 1_000.0
    assert naive != ss.net_cash_receivable(table)


def test_net_cash_receivable_is_the_genuine_receivable_when_net_is_positive():
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -9_500.0, True)]),
        cms_frame=cms([]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 1))
    assert ss.net_cash_receivable(table) == 9_500.0


def test_net_cash_receivable_is_never_negative():
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -100.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_500.0)]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 1))
    assert float(table["cash_receivable"].sum()) == -1_400.0
    assert ss.net_cash_receivable(table) == 0.0


def test_net_cash_receivable_on_an_empty_frame_is_zero():
    assert ss.net_cash_receivable(pd.DataFrame(columns=ss.FIELDS)) == 0.0
    assert ss.net_cash_receivable(None) == 0.0


def test_credit_exceeds_deposit_rows_lists_only_K_negative_store_dates():
    table = sheet_for(
        opening([("ABD", 0.0), ("XYZ", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -100.0, True),
                      ("2026-07-01", "XYZ", -900.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_500.0)]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 1))
    bad = ss.credit_exceeds_deposit_rows(table)
    assert len(bad) == 1
    assert bad.iloc[0]["store"] == "ABD"
    assert bad.iloc[0]["cash_receivable"] == -1_400.0


def test_credit_exceeds_deposit_rows_sorted_worst_first_and_empty_when_clean():
    table = sheet_for(
        opening([("ABD", 0.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -9_500.0, True)]),
        cms_frame=cms([]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 1))
    assert ss.credit_exceeds_deposit_rows(table).empty


# ---------------------------------------------------------------------------
# RULE 2 — J is built from three types, each via its own column; ZPO ignored
# ---------------------------------------------------------------------------

def test_COL_uses_SLIPAMOUNT():
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0)])
    received = ss.cms_received_by_deposit_date(rows)
    assert len(received) == 1
    assert received.iloc[0]["cash_received"] == 1_000.0


def test_ADJ_CR_uses_the_ADJ_column_not_SLIPAMOUNT():
    """The one rule that matters most here: SLIPAMOUNT on an ADJ.CR. row is
    the ORIGINAL slip being adjusted, not the size of the adjustment. Using it
    would book the whole original slip a second time. Confirmed against the
    raw July file: the three ADJ.CR. rows carry ADJ 103/7/8 against SLIPAMOUNT
    47,400/1,27,722/95,760 — wildly different figures."""
    rows = cms([("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 47_400.0)],
              adj=[103.0])
    received = ss.cms_received_by_deposit_date(rows)
    assert len(received) == 1
    assert received.iloc[0]["cash_received"] == 103.0   # ADJ, not SLIPAMOUNT


def test_ADJ_DB_subtracts_RECOVEREDAMOUNT():
    """The bank recovering an earlier overcredit reduces what the store is
    owed, so it is SUBTRACTED from J."""
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.DB.", "ABD", "2026-07-01", "2026-07-01", 0.0)],
              recovered=[0.0, 250.0])
    received = ss.cms_received_by_deposit_date(rows)
    assert received.iloc[0]["cash_received"] == 750.0   # 1,000 - 250


def test_ZPO_never_reaches_J_regardless_of_SLIPAMOUNT():
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ZPO", "ABD", "2026-07-01", "2026-07-01", 9_999.0)])
    received = ss.cms_received_by_deposit_date(rows)
    assert len(received) == 1
    assert received.iloc[0]["cash_received"] == 1_000.0


def test_all_three_types_combine_on_the_same_store_date():
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 500.0),
               ("ADJ.DB.", "ABD", "2026-07-01", "2026-07-01", 0.0)],
              adj=[0.0, 50.0, 0.0], recovered=[0.0, 0.0, 20.0])
    received = ss.cms_received_by_deposit_date(rows)
    # 1,000 (COL/SLIPAMOUNT) + 50 (ADJ.CR/ADJ) - 20 (ADJ.DB/RECOVEREDAMOUNT)
    assert received.iloc[0]["cash_received"] == 1_030.0


def test_cms_contribution_by_type_reports_the_source_column():
    """So the operator can see the split rather than one blended J figure."""
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 47_400.0)],
              adj=[0.0, 103.0])
    report = ss.cms_contribution_by_type(rows)
    by_type = report.set_index("type")
    assert by_type.loc["COL", "source_column"] == "SLIPAMOUNT"
    assert by_type.loc["COL", "value"] == 1_000.0
    assert by_type.loc["ADJ.CR.", "source_column"] == "ADJ"
    assert by_type.loc["ADJ.CR.", "value"] == 103.0    # not 47,400
    assert by_type.loc["ADJ.DB.", "value"] == 0.0


def test_only_ZPO_is_reported_as_excluded_now():
    """ADJ.CR. and ADJ.DB. feed J via their own columns and are no longer
    'excluded' — only ZPO is."""
    rows = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0),
               ("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 500.0),
               ("ADJ.DB.", "ABD", "2026-07-05", "2026-07-05", 0.0),
               ("ZPO", "ABD", None, None, 9_999.0)])
    report = ss.excluded_credit_types(rows)
    assert set(report["type"]) == {"ZPO"}
    assert "COL" not in set(report["type"])
    assert "ADJ.CR." not in set(report["type"])
    assert "ADJ.DB." not in set(report["type"])


def test_a_ZPO_row_with_no_deposit_date_is_not_forced_onto_a_date():
    """Rule 6: a figure is never attributed to a day it does not belong to."""
    report = ss.excluded_credit_types(cms([("ZPO", None, None, None, 500.0)]))
    assert report.iloc[0]["date"] == "(no deposit date)"
    assert report.iloc[0]["store"] == "(unmapped)"


# ---------------------------------------------------------------------------
# The carry-forward
# ---------------------------------------------------------------------------

def test_opening_on_the_first_date_comes_from_the_balance_file():
    table = sheet_for(opening([("ABD", 76_938.0)]),
                      pos_frame=pos([("2026-07-01", "ABD", "B1", "Cash", 82_608.0)]),
                      ptc_frame=ptc([("2026-07-01", "ABD", -185.0, False)]))
    row = table.iloc[0]
    assert row["opening_balance"] == 76_938.0
    # The section 7 ABD figure: 76,938 + 82,608 - 185 = 159,361.
    assert row["closing_balance"] == 159_361.0


def test_opening_after_the_first_date_is_the_previous_closing():
    table = sheet_for(
        opening([("ABD", 1_000.0)]),
        pos_frame=pos([("2026-07-01", "ABD", "B1", "Cash", 500.0)]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 3))
    closings = table.sort_values("date")["closing_balance"].tolist()
    openings = table.sort_values("date")["opening_balance"].tolist()
    assert openings == [1_000.0, 1_500.0, 1_500.0]
    assert closings == [1_500.0, 1_500.0, 1_500.0]


def test_a_day_with_no_movement_is_a_visible_row_not_a_gap():
    table = sheet_for(opening([("ABD", 500.0)]),
                      lo=date(2026, 7, 1), hi=date(2026, 7, 3))
    assert len(table) == 3
    assert table["closing_balance"].tolist() == [500.0, 500.0, 500.0]


def test_a_store_absent_from_the_opening_file_starts_at_zero_not_dropped():
    table = sheet_for(
        opening([("ABD", 500.0)]),
        pos_frame=pos([("2026-07-01", "NEW", "B1", "Cash", 25.0)]))
    assert set(table["store"]) == {"ABD", "NEW"}
    new = table[table["store"] == "NEW"].iloc[0]
    assert new["opening_balance"] == 0.0 and new["closing_balance"] == 25.0


# ---------------------------------------------------------------------------
# The summary block
# ---------------------------------------------------------------------------

@pytest.fixture
def two_days():
    return sheet_for(
        opening([("ABD", 1_000.0), ("XYZ", 2_000.0)]),
        bills_frame=bills([("2026-07-01", "ABD", 300.0, True),
                           ("2026-07-02", "XYZ", 400.0, False)]),
        pos_frame=pos([("2026-07-01", "ABD", "B1", "Cash", 100.0),
                       ("2026-07-02", "XYZ", "B2", "Cash", 200.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -50.0, True)]),
        cms_frame=cms([("COL", "ABD", "2026-07-01", "2026-07-02", 50.0)]),
        lo=date(2026, 7, 1), hi=date(2026, 7, 2))


def test_the_summary_is_one_row_per_date_plus_a_total(two_days):
    summary = ss.summarise_by_date(two_days)
    assert list(summary["date"]) == ["2026-07-01", "2026-07-02", "TOTAL"]
    assert list(summary.columns) == ss.FIELDS


def test_the_summary_opening_is_the_first_date_not_a_sum_down_the_column(two_days):
    """Summing opening down the month would count the carry-forward once per
    day — 31 times on a full month."""
    summary = ss.summarise_by_date(two_days)
    total = summary[summary["date"] == "TOTAL"].iloc[0]
    first_date = two_days[two_days["date"] == "2026-07-01"]
    last_date = two_days[two_days["date"] == "2026-07-02"]
    assert total["opening_balance"] == first_date["opening_balance"].sum()
    assert total["closing_balance"] == last_date["closing_balance"].sum()
    assert total["opening_balance"] != two_days["opening_balance"].sum()


# ---------------------------------------------------------------------------
# Controls (a)..(h)
# ---------------------------------------------------------------------------

def test_every_control_holds_on_a_well_formed_sheet(two_days):
    summary = ss.summarise_by_date(two_days)
    checks = ss.run_controls(two_days, summary, None,
                             dashboard_digital=float(two_days["digital_sale"].sum()))
    assert [c.key for c in checks.controls] == list("abcdefgh")
    assert checks.all_hold, [(c.key, c.detail) for c in checks.failed]


def test_control_a_catches_C_not_equal_to_D_plus_E(two_days):
    broken = two_days.copy()
    broken.loc[0, "total_sale"] = 999_999.0
    checks = ss.run_controls(broken, ss.summarise_by_date(two_days), None,
                             dashboard_digital=float(broken["digital_sale"].sum()))
    assert not checks.get("a").holds
    assert len(checks.get("a").rows) == 1


def test_control_b_catches_a_wrong_closing(two_days):
    broken = two_days.copy()
    broken.loc[0, "closing_balance"] += 1.0
    checks = ss.run_controls(broken, ss.summarise_by_date(two_days), None,
                             dashboard_digital=float(broken["digital_sale"].sum()))
    assert not checks.get("b").holds


def test_control_c_catches_a_broken_carry_forward(two_days):
    broken = two_days.copy()
    mask = broken["date"] == "2026-07-02"
    broken.loc[mask, "opening_balance"] = 0.0
    checks = ss.run_controls(broken, ss.summarise_by_date(two_days), None,
                             dashboard_digital=float(broken["digital_sale"].sum()))
    assert not checks.get("c").holds
    assert len(checks.get("c").rows) >= 1


def test_control_d_lists_every_negative_closing_with_store_date_and_amount():
    table = sheet_for(
        opening([("ABD", 100.0)]),
        ptc_frame=ptc([("2026-07-01", "ABD", -5_000.0, True)]))
    checks = ss.run_controls(table, ss.summarise_by_date(table), None,
                             dashboard_digital=0.0)
    control = checks.get("d")
    assert not control.holds
    assert list(control.rows.columns[:2]) == ["date", "store"]
    assert control.rows.iloc[0]["closing_balance"] == -4_900.0


def test_control_e_is_the_estate_identity(two_days):
    checks = ss.run_controls(two_days, ss.summarise_by_date(two_days), None,
                             dashboard_digital=float(two_days["digital_sale"].sum()))
    assert checks.get("e").holds
    assert "difference 0.00" in checks.get("e").detail


def test_control_f_fails_loudly_when_no_dashboard_figure_is_given(two_days):
    """Silence would let an unverified control read as a pass."""
    checks = ss.run_controls(two_days, ss.summarise_by_date(two_days), None,
                             dashboard_digital=None)
    assert not checks.get("f").holds
    assert "NOT verified" in checks.get("f").detail


def test_control_g_does_not_apply_when_20_july_is_out_of_range(two_days):
    checks = ss.run_controls(two_days, ss.summarise_by_date(two_days), None,
                             dashboard_digital=float(two_days["digital_sale"].sum()))
    assert checks.get("g").holds
    assert "does not apply" in checks.get("g").detail


def test_control_g_checks_the_20_july_cash_figure():
    table = sheet_for(
        opening([("ABD", 0.0)]),
        pos_frame=pos([("2026-07-20", "ABD", "B1", "Cash", 12_941_046.0)]),
        lo=date(2026, 7, 20), hi=date(2026, 7, 20))
    checks = ss.run_controls(table, ss.summarise_by_date(table), None,
                             dashboard_digital=0.0)
    assert checks.get("g").holds

    short = table.copy()
    short.loc[0, "cash_sale"] -= 1.0
    checks = ss.run_controls(short, ss.summarise_by_date(short), None,
                             dashboard_digital=0.0)
    assert not checks.get("g").holds


def test_control_h_catches_a_summary_that_disagrees_with_the_detail(two_days):
    summary = ss.summarise_by_date(two_days)
    summary.loc[summary["date"] == "TOTAL", "cash_sale"] += 100.0
    checks = ss.run_controls(two_days, summary, None,
                             dashboard_digital=float(two_days["digital_sale"].sum()))
    assert not checks.get("h").holds
    assert "Cash sale" in checks.get("h").rows["column"].tolist()


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------

SHEET_NAMES = ["By date", "By store and date", "By store", "Excluded types",
              "Controls"]


def test_the_workbook_has_exactly_the_five_tabs_in_order(two_days):
    import io

    import openpyxl

    from mop_reco.store_sheet_export import build_workbook

    data = build_workbook(two_days, ss.summarise_by_date(two_days),
                          ss.excluded_credit_types(cms([])), run_id=1,
                          date_from=date(2026, 7, 1), date_to=date(2026, 7, 2))
    assert openpyxl.load_workbook(io.BytesIO(data)).sheetnames == SHEET_NAMES


def test_the_derived_columns_are_written_as_excel_formulas_on_the_detail_tab(two_days):
    import io

    import openpyxl

    from mop_reco.store_sheet_export import build_workbook

    summary = ss.summarise_by_date(two_days)
    data = build_workbook(two_days, summary, ss.excluded_credit_types(cms([])),
                          run_id=1, date_from=date(2026, 7, 1),
                          date_to=date(2026, 7, 2))
    book = openpyxl.load_workbook(io.BytesIO(data))
    ws = book["By store and date"]

    # No letter prefixes any more (2026-09-30) — the column-header row (the
    # SECOND header row, below the group-header row) is found by its plain
    # "Store" label, Store now being column A.
    header = next(r for r in range(1, 20)
                  if str(ws.cell(r, 1).value or "") == "Store")
    row = header + 1                          # the first data row
    # New column order: A Store, B Date, C Total sale, D Digital sale,
    # E Digital received, F Digital receivable, G spacer, H Opening balance,
    # I Cash sale, J Cash deposit, K Cash received, L Cash receivable,
    # M Cash expense/income, N Closing balance, O Float amount.
    assert ws.cell(row, 3).value == f"=D{row}+I{row}"           # C total_sale
    assert ws.cell(row, 6).value == f"=D{row}-E{row}"           # F digital_receivable
    assert ws.cell(row, 12).value == f"=J{row}-K{row}"          # L cash_receivable
    assert ws.cell(row, 14).value == f"=H{row}+I{row}-J{row}+M{row}"   # N closing_balance
    assert ws.cell(row, 15).value == f"=H{row}-J{row}"          # O float_amount
    # The source columns stay values, Indian-formatted.
    assert isinstance(ws.cell(row, 4).value, (int, float))
    assert ws.cell(row, 4).number_format == "#,##,##0.00"


def test_only_ZPO_is_written_as_excluded_in_the_export():
    """ADJ.CR. feeds J via its own column and must not be reported as
    NOT feeding J — only ZPO carries "No — ignored"."""
    import io

    import openpyxl

    from mop_reco.store_sheet_export import build_workbook

    table = sheet_for(opening([("ABD", 0.0)]))
    raw_cms = cms([("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 500.0),
                   ("ZPO", None, None, None, 9_999.0)], adj=[118.0, 0.0])
    excluded = ss.excluded_credit_types(raw_cms)
    contribution = ss.cms_contribution_by_type(raw_cms)
    other_types = ss.other_type_summary(raw_cms)
    data = build_workbook(table, ss.summarise_by_date(table), excluded,
                          run_id=1, date_from=date(2026, 7, 1),
                          date_to=date(2026, 7, 1), contribution=contribution,
                          other_types=other_types)
    book = openpyxl.load_workbook(io.BytesIO(data))
    ws = book["Excluded types"]
    # The "Non-COL CMS types" table (Type/Rows/Value/Feeds column J?) sits
    # ABOVE the "Column J — full contribution" table, which also has an
    # ADJ.CR. row but in a different column layout — restrict the scan to
    # before that heading so the two tables are never confused.
    contribution_heading = next(
        r for r in range(1, ws.max_row + 1)
        if str(ws.cell(r, 1).value or "").startswith("Column J — full"))
    rows = [[ws.cell(r, c).value for c in range(1, 5)]
           for r in range(1, contribution_heading)]
    by_type = {row[0]: row for row in rows if row[0] in ("ADJ.CR.", "ADJ.DB.", "ZPO")}
    assert by_type["ADJ.CR."][3] == "Yes"
    assert by_type["ZPO"][3] == "No — ignored"

    all_rows = [[ws.cell(r, c).value for c in range(1, 5)]
               for r in range(1, ws.max_row + 1)]
    text = " ".join(str(v or "") for row in all_rows for v in row)
    assert "ZPO" in text and "ADJ.CR." in text
    # The full J-contribution table is on the same tab, for the whole figure
    # in one place.
    flat = " ".join(str(ws.cell(r, c).value or "")
                    for r in range(1, ws.max_row + 1) for c in range(1, 6))
    assert "Column J" in flat and "contribution" in flat.lower()


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

def test_no_input_gives_an_empty_sheet_with_the_right_columns():
    table = sheet_for()
    assert table.empty
    assert list(table.columns) == ss.FIELDS


def test_controls_on_an_empty_sheet_report_rather_than_crash():
    checks = ss.run_controls(sheet_for(), pd.DataFrame(), None)
    assert not checks.all_hold
    assert checks.controls[0].key == "empty"


# ---------------------------------------------------------------------------
# The three-block summary (2026-09-11)
# ---------------------------------------------------------------------------

def test_summarise_by_store_is_one_row_per_store_for_the_whole_range(two_days):
    by_store = ss.summarise_by_store(two_days)
    assert set(by_store["store"]) == {"ABD", "XYZ"}
    assert len(by_store) == 2


def test_by_store_opening_and_closing_are_that_stores_first_and_last(two_days):
    """Not a sum of daily balances — that would double-count the
    carry-forward."""
    by_store = ss.summarise_by_store(two_days)
    abd_detail = two_days[two_days["store"] == "ABD"].sort_values("date")
    abd_summary = by_store[by_store["store"] == "ABD"].iloc[0]
    assert abd_summary["opening_balance"] == abd_detail.iloc[0]["opening_balance"]
    assert abd_summary["closing_balance"] == abd_detail.iloc[-1]["closing_balance"]
    assert abd_summary["opening_balance"] != abd_detail["opening_balance"].sum()


def test_by_store_additive_columns_sum_across_the_stores_dates(two_days):
    by_store = ss.summarise_by_store(two_days)
    abd_detail = two_days[two_days["store"] == "ABD"]
    abd_summary = by_store[by_store["store"] == "ABD"].iloc[0]
    assert abd_summary["cash_sale"] == abd_detail["cash_sale"].sum()
    assert abd_summary["total_sale"] == abd_detail["total_sale"].sum()


def test_grand_total_row_is_one_row_and_matches_the_by_date_total(two_days):
    grand = ss.grand_total_row(two_days)
    by_date_total = ss.summarise_by_date(two_days)
    by_date_total = by_date_total[by_date_total["date"] == "TOTAL"].iloc[0]
    assert len(grand) == 1
    assert grand.iloc[0]["date"] == "TOTAL"
    # The TOTAL row's own convention (summarise_by_date) reuses the store
    # column to show the date span, since "ALL STORES" is already used on
    # each per-date row.
    assert grand.iloc[0]["store"] == "2026-07-01 to 2026-07-02"
    for column in ("cash_sale", "digital_sale", "closing_balance",
                   "opening_balance"):
        assert grand.iloc[0][column] == by_date_total[column]


def test_by_store_total_closing_equals_grand_total_closing(two_days):
    """Both blocks must describe the same estate — one summed by store, one
    as a single figure — and they must never disagree."""
    by_store = ss.summarise_by_store(two_days)
    grand = ss.grand_total_row(two_days)
    assert abs(by_store["closing_balance"].sum()
              - grand.iloc[0]["closing_balance"]) < 0.01


def test_grand_total_on_an_empty_sheet_is_empty_not_a_crash():
    assert ss.grand_total_row(pd.DataFrame(columns=ss.FIELDS)).empty
    assert ss.summarise_by_store(pd.DataFrame(columns=ss.FIELDS)).empty


def test_each_tabs_last_row_is_a_total_and_they_agree_with_the_detail(two_days):
    """The by-date TOTAL, the by-store grand total, and the actual sum of the
    detail tab's own rows must never quietly drift apart — they are the same
    SUM over the same range, just written on three different tabs."""
    import io

    import openpyxl

    from mop_reco.store_sheet_export import build_workbook

    by_store = ss.summarise_by_store(two_days)
    grand = ss.grand_total_row(two_days)
    data = build_workbook(two_days, ss.summarise_by_date(two_days),
                          ss.excluded_credit_types(cms([])), run_id=1,
                          date_from=date(2026, 7, 1), date_to=date(2026, 7, 2),
                          summary_by_store=by_store, grand_total=grand)
    # xlsxwriter caches a computed value alongside every formula it writes,
    # so data_only=True reads that cached number rather than the formula
    # text — this is what "the export re-proves its own arithmetic" means
    # for a reader that does not evaluate formulas.
    book = openpyxl.load_workbook(io.BytesIO(data), data_only=True)

    by_date_ws = book["By date"]
    by_store_ws = book["By store"]
    detail_ws = book["By store and date"]

    # No letter prefixes (2026-09-30); Store is column A now, so the header
    # row is found by "Store".
    detail_header = next(r for r in range(1, 20)
                         if detail_ws.cell(r, 1).value == "Store")
    detail_rows = range(detail_header + 1, detail_ws.max_row + 1)
    detail_total_D = sum(float(detail_ws.cell(r, 4).value or 0) for r in detail_rows)

    # "TOTAL" now lives in column B (Date) — Store/Date swapped, and the
    # TOTAL row's own Store cell carries the date span instead (§ store_sheet
    # summarise_by_date's own convention, unchanged).
    by_date_total_row = next(
        r for r in range(1, by_date_ws.max_row + 1)
        if str(by_date_ws.cell(r, 2).value or "") == "TOTAL")
    by_store_total_row = next(
        r for r in range(1, by_store_ws.max_row + 1)
        if str(by_store_ws.cell(r, 2).value or "") == "TOTAL")

    assert abs(by_date_ws.cell(by_date_total_row, 4).value - detail_total_D) < 0.01
    assert abs(by_store_ws.cell(by_store_total_row, 4).value - detail_total_D) < 0.01
    assert abs(by_date_ws.cell(by_date_total_row, 4).value
              - by_store_ws.cell(by_store_total_row, 4).value) < 0.01


def test_the_total_rows_formulas_point_at_the_detail_tab_not_a_summary(two_days):
    """Tracing a total must never depend on trusting an intermediate block —
    every TOTAL/grand-total row points straight at the detail tab."""
    import io

    import openpyxl

    from mop_reco.store_sheet_export import build_workbook

    data = build_workbook(two_days, ss.summarise_by_date(two_days),
                          ss.excluded_credit_types(cms([])), run_id=1,
                          date_from=date(2026, 7, 1), date_to=date(2026, 7, 2),
                          summary_by_store=ss.summarise_by_store(two_days),
                          grand_total=ss.grand_total_row(two_days))
    book = openpyxl.load_workbook(io.BytesIO(data))

    for tab in ("By date", "By store"):
        ws = book[tab]
        # Store/Date swapped (2026-09-30) — "TOTAL" now carries in column B
        # (Date), with column A (Store) holding the date span instead; no
        # provenance line's own value is literally "TOTAL", so this is
        # unambiguous.
        total_row = next(
            r for r in range(1, ws.max_row + 1)
            if str(ws.cell(r, 2).value or "") == "TOTAL")
        d_formula = ws.cell(total_row, 4).value    # column D — digital_sale,
                                                    # unchanged position
        assert d_formula.startswith("=SUM('By store and date'!D")


def test_expand_all_and_collapse_all_are_not_export_concerns():
    """The grouped on-screen table is a page concern (the export already
    carries the by-date and by-store cuts as separate tabs); the engine
    function backing it lives in store_sheet, not the export module."""
    import mop_reco.store_sheet_export as export_module
    assert not hasattr(export_module, "build_grouped_table")


# ---------------------------------------------------------------------------
# The merged, expandable on-screen table (2026-09-11)
# ---------------------------------------------------------------------------

def test_collapsed_grouped_table_is_one_row_per_date_plus_total(two_days):
    by_date = ss.summarise_by_date(two_days)
    grouped = ss.build_grouped_table(two_days, by_date, expanded_dates=[])
    assert list(grouped["date"]) == ["2026-07-01", "2026-07-02", "TOTAL"]
    assert list(grouped["store"])[:2] == ["ALL STORES", "ALL STORES"]
    assert list(grouped.columns) == ss.FIELDS


def test_expanding_a_date_splices_in_its_store_rows_beneath_it(two_days):
    by_date = ss.summarise_by_date(two_days)
    grouped = ss.build_grouped_table(two_days, by_date,
                                     expanded_dates=["2026-07-01"])
    rows = list(zip(grouped["date"], grouped["store"]))
    # ALL STORES for 07-01, then its two stores indented, then 07-02
    # untouched, then TOTAL.
    assert rows[0] == ("2026-07-01", "ALL STORES")
    assert rows[1] == ("", "    ABD")
    assert rows[2] == ("", "    XYZ")
    assert rows[3] == ("2026-07-02", "ALL STORES")
    assert rows[4] == ("TOTAL", "2026-07-01 to 2026-07-02")


def test_expanding_every_date_keeps_the_same_columns_throughout(two_days):
    by_date = ss.summarise_by_date(two_days)
    all_dates = list(two_days["date"].unique())
    grouped = ss.build_grouped_table(two_days, by_date, expanded_dates=all_dates)
    assert list(grouped.columns) == ss.FIELDS
    # Every original store-date row is present somewhere in the expansion.
    assert len(grouped) == len(by_date) - 1 + len(two_days) + 1


def test_grouped_table_on_empty_input_is_empty_not_a_crash():
    empty = pd.DataFrame(columns=ss.FIELDS)
    assert ss.build_grouped_table(empty, empty, []).empty


def test_every_consolidated_date_row_equals_the_sum_of_its_store_rows(two_days):
    by_date = ss.summarise_by_date(two_days)
    mismatches = ss.verify_date_rows_equal_store_sums(two_days, by_date)
    assert mismatches.empty


def test_a_date_row_disagreeing_with_its_stores_is_caught():
    """This is finer-grained than control (h), which only checks the single
    grand TOTAL row — a date could still disagree with its own stores while
    the whole-range total happens to be right."""
    sheet = pd.DataFrame([
        {**{f: 0.0 for f in ss.FIELDS}, "date": "2026-07-01", "store": "ABD",
         "cash_sale": 100.0},
        {**{f: 0.0 for f in ss.FIELDS}, "date": "2026-07-01", "store": "XYZ",
         "cash_sale": 50.0},
    ])[ss.FIELDS]
    # A tampered consolidated row that does not equal 100 + 50.
    by_date = pd.DataFrame([
        {**{f: 0.0 for f in ss.FIELDS}, "date": "2026-07-01", "store": "ALL STORES",
         "cash_sale": 999.0},
    ])[ss.FIELDS]
    mismatches = ss.verify_date_rows_equal_store_sums(sheet, by_date)
    assert not mismatches.empty
    assert "2026-07-01" in mismatches["date"].tolist()


# ---------------------------------------------------------------------------
# Non-COL type summary — the export's "Excluded types" tab (2026-09-11)
# ---------------------------------------------------------------------------

def test_other_type_summary_lists_ADJ_CR_ADJ_DB_and_ZPO():
    raw_cms = cms([("ADJ.CR.", "ABD", "2026-07-01", "2026-07-01", 47_400.0),
                   ("ZPO", None, None, None, 9_999.0)], adj=[103.0, 0.0])
    report = ss.other_type_summary(raw_cms)
    by_type = report.set_index("type")
    assert by_type.loc["ADJ.CR.", "value"] == 103.0
    assert bool(by_type.loc["ADJ.CR.", "feeds_J"]) is True
    assert by_type.loc["ADJ.DB.", "rows"] == 0
    assert by_type.loc["ZPO", "value"] == 9_999.0
    assert bool(by_type.loc["ZPO", "feeds_J"]) is False


def test_other_type_summary_never_includes_COL():
    raw_cms = cms([("COL", "ABD", "2026-07-01", "2026-07-01", 1_000.0)])
    report = ss.other_type_summary(raw_cms)
    assert "COL" not in set(report["type"])


def test_other_type_summary_on_empty_input_is_empty_not_a_crash():
    assert ss.other_type_summary(pd.DataFrame()).columns.tolist() == [
        "type", "rows", "value", "feeds_J"]
