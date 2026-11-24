"""Audit defect D-002 (2026-11-24): stored money carried float noise (74017.9999999999, 86044.0600000001). Rounding to the whole paisa when
the aggregates are written must give exact stored values, keep every identity exact, and change no value by more than the noise."""
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_sheet_source as src  # noqa: E402


def _row(**kw):
    base = dict(date="2026-10-02", store="SNP", total_sale=0.0, digital_sale=0.0, cash_sale=0.0, opening_balance=0.0, cash_deposit=0.0,
                cash_received=0.0, cash_receivable=0.0, cash_expense_income=0.0, closing_balance=0.0, float_amount=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _decimals(x) -> int:
    return -Decimal(str(x)).as_tuple().exponent


def test_noise_seen_in_production_is_removed_and_nothing_else_changes():
    noisy = {"opening_balance": 74017.9999999999, "closing_balance": 86044.0600000001, "float_amount": 7500.59999999998,
             "digital_sale": 54163022.620000005, "cash_sale": 100.0}
    rec = src.store_day_record(2, _row(**noisy), False)
    assert rec["opening_balance"] == 74018.0 and rec["closing_balance"] == 86044.06 and rec["float_amount"] == 7500.6
    assert rec["digital_sale"] == 54163022.62 and rec["cash_sale"] == 100.0
    assert all(_decimals(rec[c]) <= 2 for c in src._SHEET_MONEY)
    for c, v in noisy.items():
        assert abs(rec[c] - v) < 1e-6, "rounding may only remove the noise, never move a real amount"


def test_a_real_amount_on_the_paise_grid_is_untouched_and_negatives_survive():
    rec = src.store_day_record(2, _row(opening_balance=1234.56, closing_balance=-0.01, cash_deposit=-87937.0), True)
    assert rec["opening_balance"] == 1234.56 and rec["closing_balance"] == -0.01 and rec["cash_deposit"] == -87937.0
    assert rec["defect_b"] is True and rec["run_id"] == 2 and rec["store"] == "SNP"


def test_identities_stay_exact_after_rounding_the_columns_independently():
    # true values (whole paise): H 1000.10, E 500.20, I 300.30, L -20.05 -> M 1179.95 ; N = H - I = 699.80 ; C = D + E with D 250.25
    noisy = _row(opening_balance=1000.1000000001, cash_sale=500.19999999998, cash_deposit=300.3000000002, cash_expense_income=-20.0500000001,
                 closing_balance=1179.9500000003, float_amount=699.8000000001, digital_sale=250.2500000002, total_sale=750.4500000004)
    r = src.store_day_record(1, noisy, False)
    paise = lambda x: int((Decimal(str(x)) * 100).to_integral_value())  # noqa: E731
    assert paise(r["closing_balance"]) == paise(r["opening_balance"]) + paise(r["cash_sale"]) - paise(r["cash_deposit"]) + paise(r["cash_expense_income"])
    assert paise(r["float_amount"]) == paise(r["opening_balance"]) - paise(r["cash_deposit"])
    assert paise(r["total_sale"]) == paise(r["digital_sale"]) + paise(r["cash_sale"])
    assert r["closing_balance"] == 1179.95 and r["float_amount"] == 699.8 and r["total_sale"] == 750.45


def test_the_date_total_record_is_rounded_the_same_way():
    rec = src.date_total_record(5, _row(date="2026-10-02", closing_balance=86044.0600000001))
    assert rec["closing_balance"] == 86044.06 and rec["run_id"] == 5 and "store" not in rec
