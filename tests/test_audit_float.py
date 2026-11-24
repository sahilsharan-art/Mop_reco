"""Audit Phase 2: the Float grid against an INDEPENDENT recomputation (CLAUDE.md 6.15).

Independent means: Decimal money, hand-written streak logic, and a calendar rule worked out arithmetically (the 2nd Saturday of any month
always falls on day 8-14 and the 4th on day 22-28) instead of the engine's scan. Boundaries are asserted by hand.
Synthetic data only.
"""
import random
import sys
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import float_tracker as ft  # noqa: E402

CONFIG = {"float_tracker": {"float_threshold": 10000, "threshold_overrides": {"OVR": 50000}}}


def sheet(rows):
    """rows: (store, date, opening, deposit) -> a store-sheet frame with float_amount = opening - deposit (6.9d column N)."""
    recs = []
    for store, d, opening, deposit in rows:
        recs.append({"date": d.isoformat(), "store": store, "opening_balance": float(opening), "cash_sale": 0.0, "cash_deposit": float(deposit),
                     "float_amount": float(D(str(opening)) - D(str(deposit))), "cash_received": 0.0, "closing_balance": 0.0})
    return pd.DataFrame(recs)


def independent_auto_remark(d):
    if d.weekday() == 6:
        return "Weekend"
    if d.weekday() == 5 and (8 <= d.day <= 14 or 22 <= d.day <= 28):
        return "Weekend"
    return ""


# ------------------------------------------------------------------------------------------ hand-asserted boundaries
def test_float_exactly_at_threshold_is_not_high_one_paisa_over_is():
    g = ft.build_float_grid(sheet([("AAA", date(2026, 7, 1), "20000.00", "10000.00"), ("AAA", date(2026, 7, 2), "20000.01", "10000.00")]), CONFIG)
    assert list(g["high_float"]) == [False, True], "10,000.00 is not above 10,000; 10,000.01 is"
    assert list(g["days_held_high"]) == [0, 1]


def test_float_is_opening_less_deposit_never_closing():
    s = sheet([("AAA", date(2026, 7, 1), "50000", "30000")])
    s["closing_balance"] = 999999.0                       # a closing balance must have no influence
    g = ft.build_float_grid(s, CONFIG)
    assert float(g["float_amount"].iloc[0]) == 20000.0 and bool(g["high_float"].iloc[0])


def test_negative_float_is_kept_flagged_and_listed_never_floored():
    g = ft.build_float_grid(sheet([("AAA", date(2026, 7, 1), "1000", "5000"), ("BBB", date(2026, 7, 1), "9000", "1000")]), CONFIG)
    assert sorted(g["float_amount"]) == [-4000.0, 8000.0]
    neg = ft.negative_float_rows(g)
    assert list(neg["store"]) == ["AAA"] and float(neg["float_amount"].iloc[0]) == -4000.0
    assert not bool(g.loc[g["store"] == "AAA", "high_float"].iloc[0])


def test_streak_counts_consecutive_high_days_resets_and_never_leaks_between_stores():
    d = [date(2026, 7, k) for k in range(1, 7)]
    a = [("AAA", d[0], 30000, 0), ("AAA", d[1], 30000, 0), ("AAA", d[2], 5000, 0), ("AAA", d[3], 30000, 0), ("AAA", d[4], 30000, 0), ("AAA", d[5], 30000, 0)]
    b = [("BBB", d[k], 30000, 0) for k in range(6)]
    g = ft.build_float_grid(sheet(b[:3] + a + b[3:]), CONFIG)           # interleaved and out of order on purpose
    ga = g[g["store"] == "AAA"].sort_values("business_date")
    gb = g[g["store"] == "BBB"].sort_values("business_date")
    assert list(ga["days_held_high"]) == [1, 2, 0, 1, 2, 3]
    assert list(gb["days_held_high"]) == [1, 2, 3, 4, 5, 6]


def test_per_store_override_replaces_the_default_only_for_that_store():
    g = ft.build_float_grid(sheet([("OVR", date(2026, 7, 1), "40000", "0"), ("AAA", date(2026, 7, 1), "40000", "0")]), CONFIG)
    assert bool(g.loc[g["store"] == "OVR", "high_float"].iloc[0]) is False and bool(g.loc[g["store"] == "AAA", "high_float"].iloc[0]) is True
    assert float(g.loc[g["store"] == "OVR", "float_threshold"].iloc[0]) == 50000.0


def test_empty_and_single_row_inputs():
    assert ft.build_float_grid(pd.DataFrame(), CONFIG).empty
    one = ft.build_float_grid(sheet([("AAA", date(2026, 7, 1), "0", "0")]), CONFIG)
    assert len(one) == 1 and list(one["days_held_high"]) == [0] and not bool(one["high_float"].iloc[0])


@pytest.mark.parametrize("d,expected", [
    (date(2026, 8, 1), ""),            # a month that STARTS on a Saturday: the 1st is the 1st Saturday, not a weekend remark
    (date(2026, 8, 8), "Weekend"),     # 2nd Saturday
    (date(2026, 8, 15), ""),           # 3rd
    (date(2026, 8, 22), "Weekend"),    # 4th
    (date(2026, 8, 29), ""),           # 5th
    (date(2026, 7, 11), "Weekend"),    # 2nd Saturday of July 2026
    (date(2026, 7, 25), "Weekend"),    # 4th
    (date(2026, 7, 4), ""),            # 1st
    (date(2026, 7, 5), "Weekend"),     # any Sunday
    (date(2026, 7, 6), ""),            # Monday
    (date(2028, 2, 26), "Weekend"),    # 4th Saturday of a leap February
    (date(2026, 12, 31), ""),          # year end (a Thursday)
    (date(2027, 1, 1), ""),            # year start (a Friday)
])
def test_auto_remark_calendar_boundaries(d, expected):
    assert ft.auto_remark_for(d) == expected


def test_auto_remark_agrees_with_the_independent_calendar_rule_for_four_years():
    d = date(2025, 1, 1)
    while d <= date(2028, 12, 31):
        assert ft.auto_remark_for(d) == independent_auto_remark(d), d
        d += timedelta(days=1)


def test_a_holiday_gets_the_holiday_remark_only_when_a_calendar_lists_it(monkeypatch):
    weekday = date(2026, 7, 7)
    assert ft.auto_remark_for(weekday) == ""
    monkeypatch.setattr(ft.masters, "_HOLIDAY_SET", {weekday})
    assert ft.auto_remark_for(weekday) == "Holiday"
    sunday = date(2026, 7, 5)
    monkeypatch.setattr(ft.masters, "_HOLIDAY_SET", {sunday})
    assert ft.auto_remark_for(sunday) == "Weekend", "a Sunday that is also a holiday is still explained"


# ------------------------------------------------------------------------------------------- random, vs recomputation
@pytest.mark.parametrize("seed", range(60))
def test_grid_matches_an_independent_recomputation(seed):
    rng = random.Random(seed)
    stores = rng.sample(["AAA", "BBB", "CCC", "OVR", "DDD"], rng.randint(1, 5))
    start = date(2026, 6, 25) + timedelta(days=rng.randint(0, 40))
    days = [start + timedelta(days=k) for k in range(rng.randint(1, 14))]
    rows = [(s, d, D(rng.randint(-5000, 90000)) + D(rng.choice(["0", "0.50", "0.01", "0.99"])), D(rng.randint(0, 60000))) for s in stores for d in days]
    rng.shuffle(rows)
    g = ft.build_float_grid(sheet(rows), CONFIG)
    assert len(g) == len(rows)
    for store in stores:
        streak = 0
        for d in days:
            opening, deposit = next((o, p) for s, dd, o, p in rows if s == store and dd == d)
            want_float = opening - deposit
            thr = D(50000 if store == "OVR" else 10000)
            high = want_float > thr
            streak = streak + 1 if high else 0
            row = g[(g["store"] == store) & (g["business_date"] == d.isoformat())].iloc[0]
            assert D(str(row["float_amount"])) == want_float
            assert bool(row["high_float"]) is high and int(row["days_held_high"]) == streak
            assert bool(row["negative_float"]) is (want_float < 0)
            assert row["day_name"] == d.strftime("%A")
