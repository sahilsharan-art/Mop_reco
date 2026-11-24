"""Audit Phase 2: the daily store sheet (columns A-N) and the cash book (R3a) against an INDEPENDENT recomputation (CLAUDE.md 6.6, 6.9d).

The reference is whole paise (integers), plain loops, written from the column definitions:
  C = D + E    G = D - F    K = I - J    M = H + E - I + L    N = H - I     H(first) = opening file, H(d) = M(d-1)
Boundaries (blank bill numbers, the deposit head, a store that starts mid-range, signs) are asserted by hand. Synthetic data only.
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_sheet as ss  # noqa: E402

D0 = date(2026, 7, 1)


def iso(k):
    return (D0 + timedelta(days=k)).isoformat()


def paise(x):
    return int(round(float(x) * 100))


# ------------------------------------------------------------------------------------------ component builders by hand
def test_cash_sale_counts_only_cash_rows_with_a_bill_number():
    pos = pd.DataFrame([
        {"business_date": iso(0), "store": "AAA", "mop": "Cash", "bill_no": "B1", "amount": 100.50},
        {"business_date": iso(0), "store": "AAA", "mop": "Cash", "bill_no": "B2", "amount": 200.25},
        {"business_date": iso(0), "store": "AAA", "mop": "Cash", "bill_no": None, "amount": -999.0},      # a petty-cash movement, not a sale (6.13)
        {"business_date": iso(0), "store": "AAA", "mop": "Cash", "bill_no": "", "amount": -1.0},
        {"business_date": iso(0), "store": "AAA", "mop": "Cash", "bill_no": "nan", "amount": -2.0},
        {"business_date": iso(0), "store": "AAA", "mop": "PAYTM EDC", "bill_no": "B3", "amount": 777.0},   # digital, not cash
        {"business_date": iso(0), "store": "AAA", "mop": " Cash ", "bill_no": "B4", "amount": 0.25},         # padded name is still Cash
        {"business_date": iso(0), "store": "BBB", "mop": "Cash", "bill_no": "B5", "amount": -10.0},          # a negative cash bill is kept
    ])
    got = ss.cash_sale_by_store_date(pos).set_index("store")["cash_sale"]
    assert paise(got["AAA"]) == 10050 + 20025 + 25 and paise(got["BBB"]) == -1000


def test_ptc_deposit_is_stored_positive_and_other_petty_cash_excludes_the_deposit_head():
    ptc = pd.DataFrame([
        {"business_date": iso(0), "store": "AAA", "amount": -50000.0, "is_deposit": True},     # raw ledger sign: cash leaving the till
        {"business_date": iso(0), "store": "AAA", "amount": -1200.50, "is_deposit": False},    # an expense
        {"business_date": iso(0), "store": "AAA", "amount": 300.25, "is_deposit": False},      # an income
        {"business_date": iso(0), "store": "AAA", "amount": -2000.0, "is_deposit": True},
    ])
    row = ss.ptc_by_store_date(ptc).iloc[0]
    assert paise(row["cash_deposit"]) == 52000_00 // 1 // 1 // 1 and paise(row["cash_deposit"]) == 5200000, "deposits 50,000 + 2,000 stored POSITIVE"
    assert paise(row["cash_expense_income"]) == -120050 + 30025, "L is net of every NON-deposit row only"


def test_digital_received_is_the_matched_subset_of_digital_sale():
    bl = pd.DataFrame([
        {"business_date": iso(0), "store": "AAA", "amount": 100.10, "matched": True},
        {"business_date": iso(0), "store": "AAA", "amount": 50.05, "matched": False},
        {"business_date": iso(0), "store": "AAA", "amount": 25.00, "matched": None},       # unknown is NOT received
    ])
    r = ss.digital_by_store_date(bl).iloc[0]
    assert paise(r["digital_sale"]) == 17515 and paise(r["digital_received"]) == 10010


def test_cms_received_uses_each_types_own_column_signs_and_the_deposit_date():
    cms = pd.DataFrame([
        {"TYPE": "COL", "store": "AAA", "DEPDATE": iso(0), "SLIPAMOUNT": 1000.0, "ADJ": 0.0, "RECOVEREDAMOUNT": 0.0},
        {"TYPE": "ADJ.CR.", "store": "AAA", "DEPDATE": iso(0), "SLIPAMOUNT": 47400.0, "ADJ": 103.0, "RECOVEREDAMOUNT": 0.0},   # ADJ column, not the slip
        {"TYPE": "ADJ.DB.", "store": "AAA", "DEPDATE": iso(0), "SLIPAMOUNT": 5000.0, "ADJ": 0.0, "RECOVEREDAMOUNT": 400.0},     # minus RECOVEREDAMOUNT
        {"TYPE": "ZPO", "store": "AAA", "DEPDATE": None, "SLIPAMOUNT": 99999.0, "ADJ": 0.0, "RECOVEREDAMOUNT": 0.0},            # ignored
        {"TYPE": "COL", "store": None, "DEPDATE": iso(0), "SLIPAMOUNT": 555.0, "ADJ": 0.0, "RECOVEREDAMOUNT": 0.0},            # no store: not placed
        {"TYPE": "COL", "store": "AAA", "DEPDATE": None, "SLIPAMOUNT": 444.0, "ADJ": 0.0, "RECOVEREDAMOUNT": 0.0},              # no date: not placed
    ])
    got = ss.cms_received_by_deposit_date(cms)
    assert len(got) == 1 and paise(got["cash_received"].iloc[0]) == 100000 + 10300 - 40000


# ------------------------------------------------------------------------------------------- the sheet, by hand
def frames(opening=None, digital=None, cash=None, ptc=None, received=None):
    def f(rows, cols):
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    return (f(opening, ["store", "opening_cash"]), f(digital, ["date", "store", "digital_sale", "digital_received"]),
            f(cash, ["date", "store", "cash_sale"]), f(ptc, ["date", "store", "cash_deposit", "cash_expense_income"]),
            f(received, ["date", "store", "cash_received"]))


def test_a_known_row_worked_by_hand():
    # Opening 99,437. Cash sale 73,067. Deposit 87,937 (credited in full). Expense -320.  Digital sale 1,000, 900 received.
    sheet = ss.build_sheet(*frames(opening=[("ABD", 99437.0)], digital=[(iso(0), "ABD", 1000.0, 900.0)], cash=[(iso(0), "ABD", 73067.0)],
                                   ptc=[(iso(0), "ABD", 87937.0, -320.0)], received=[(iso(0), "ABD", 87937.0)]), D0, D0)
    r = sheet.iloc[0]
    assert [paise(r[c]) for c in ("total_sale", "digital_sale", "cash_sale", "digital_received", "digital_receivable", "opening_balance",
                                  "cash_deposit", "cash_received", "cash_receivable", "cash_expense_income", "closing_balance", "float_amount")] == [
        7406700, 100000, 7306700, 90000, 10000, 9943700, 8793700, 8793700, 0, -32000, 8424700, 1149999 + 1], \
        "C=D+E, G=D-F, K=I-J=0, M=99,437+73,067-87,937-320=84,247, N=H-I=11,500"


def test_the_carry_forward_chain_and_an_untouched_store_keeps_its_balance():
    sheet = ss.build_sheet(*frames(opening=[("AAA", 1000.0), ("ZZZ", 500.0)], cash=[(iso(0), "AAA", 200.0), (iso(2), "AAA", 50.0)],
                                   ptc=[(iso(1), "AAA", 100.0, -25.0)]), D0, D0 + timedelta(days=2))
    a = sheet[sheet["store"] == "AAA"].sort_values("date")
    assert [paise(x) for x in a["opening_balance"]] == [100000, 120000, 107500]
    assert [paise(x) for x in a["closing_balance"]] == [120000, 107500, 112500]
    z = sheet[sheet["store"] == "ZZZ"]
    assert len(z) == 3 and set(paise(x) for x in z["closing_balance"]) == {50000}, "a quiet store keeps a visible row and its balance"


def test_a_store_known_only_from_the_sale_data_starts_on_its_first_movement_and_never_earlier():
    sheet = ss.build_sheet(*frames(opening=[("AAA", 10.0)], cash=[(iso(3), "NEW", 70.0)]), D0, D0 + timedelta(days=5))
    new = sheet[sheet["store"] == "NEW"].sort_values("date")
    assert list(new["date"]) == [iso(3), iso(4), iso(5)] and paise(new["opening_balance"].iloc[0]) == 0
    assert len(sheet[sheet["store"] == "AAA"]) == 6, "a store the balance file knows is active from the first date"


def test_empty_inputs_give_an_empty_sheet_and_a_missing_opening_seeds_zero_visibly():
    assert ss.build_sheet(*frames(), D0, D0 + timedelta(days=1)).empty
    sheet = ss.build_sheet(*frames(cash=[(iso(0), "BGP", 20.0)]), D0, D0)
    assert paise(sheet["opening_balance"].iloc[0]) == 0 and paise(sheet["closing_balance"].iloc[0]) == 2000, "seeded at zero, stays visible (6.6)"


def test_the_estate_identity_holds_exactly_on_a_negative_closing_case():
    sheet = ss.build_sheet(*frames(opening=[("AAA", 100.0)], ptc=[(iso(0), "AAA", 500.0, 0.0)]), D0, D0)
    assert paise(sheet["closing_balance"].iloc[0]) == -40000, "a deposit larger than the cash is shown negative, never floored (it is an impossible book)"


# ---------------------------------------------------------------------------------------- random, vs paise reference
@pytest.mark.parametrize("seed", range(80))
def test_sheet_matches_an_independent_paise_recomputation(seed):
    rng = random.Random(seed)
    stores = rng.sample(["AAA", "BBB", "CCC", "DDD", "EEE"], rng.randint(1, 5))
    n = rng.randint(1, 15)
    opening_paise = {s: rng.randint(0, 500000) for s in stores if rng.random() < 0.7}
    digital, cash, ptc, received = [], [], [], []
    cell = {}
    for s in stores:
        for d in range(n):
            e = dict(ds=0, dr=0, cs=0, dep=0, exp=0, rec=0)
            if rng.random() < 0.6:
                e["ds"] = rng.randint(0, 900000)
                e["dr"] = rng.randint(0, e["ds"])
                digital.append((iso(d), s, e["ds"] / 100, e["dr"] / 100))
            if rng.random() < 0.6:
                e["cs"] = rng.randint(-5000, 900000)
                cash.append((iso(d), s, e["cs"] / 100))
            if rng.random() < 0.5:
                e["dep"], e["exp"] = rng.randint(0, 600000), rng.randint(-50000, 50000)
                ptc.append((iso(d), s, e["dep"] / 100, e["exp"] / 100))
            if rng.random() < 0.5:
                e["rec"] = rng.randint(-1000, 600000)
                received.append((iso(d), s, e["rec"] / 100))
            cell[(s, d)] = e
    sheet = ss.build_sheet(*frames(opening=[(s, p / 100) for s, p in opening_paise.items()], digital=digital, cash=cash, ptc=ptc, received=received),
                           D0, D0 + timedelta(days=n - 1))
    for s in stores:
        first_active = 0 if s in opening_paise else next((d for d in range(n) if any(cell[(s, d)][k] for k in cell[(s, d)])), None)
        if first_active is None:
            assert sheet[sheet["store"] == s].empty
            continue
        closing = opening_paise.get(s, 0)
        for d in range(first_active, n):
            e = cell[(s, d)]
            opening = closing
            closing = opening + e["cs"] - e["dep"] + e["exp"]
            row = sheet[(sheet["store"] == s) & (sheet["date"] == iso(d))]
            assert len(row) == 1, f"{s} {iso(d)}"
            r = row.iloc[0]
            got = {k: paise(r[k]) for k in ("total_sale", "digital_sale", "cash_sale", "digital_received", "digital_receivable", "opening_balance",
                                            "cash_deposit", "cash_received", "cash_receivable", "cash_expense_income", "closing_balance", "float_amount")}
            want = {"total_sale": e["ds"] + e["cs"], "digital_sale": e["ds"], "cash_sale": e["cs"], "digital_received": e["dr"],
                    "digital_receivable": e["ds"] - e["dr"], "opening_balance": opening, "cash_deposit": e["dep"], "cash_received": e["rec"],
                    "cash_receivable": e["dep"] - e["rec"], "cash_expense_income": e["exp"], "closing_balance": closing, "float_amount": opening - e["dep"]}
            assert got == want, f"{s} {iso(d)}: {got} vs {want}"
