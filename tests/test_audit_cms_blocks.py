"""Audit Phase 2: the CMS Reco blocks against an INDEPENDENT recomputation (CLAUDE.md 6.22).

The reference is worked in whole paise (integers) with plain loops: per store, per deposit date d,
    CLOSING(d) = OPENING(d) + deposits(d) - COL credited on d - COL credited on d+1,   OPENING(d+1) = CLOSING(d)
A credit dated d+2 or later, before d, or with no date is in NEITHER column and stays in CLOSING. Synthetic data only.
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import cms_blocks as cb  # noqa: E402

D0 = date(2026, 7, 1)


def iso(k):
    return (D0 + timedelta(days=k)).isoformat()


def slips_frame(rows):
    """rows: (store, dep_day, cr_day_or_None, amount_paise, row_type)"""
    recs = [{"row_type": t, "store": s, "dep_date": iso(dd), "cr_date": None if cd is None else iso(cd), "amount": p / 100.0,
             "adj": 0.0, "recovered": 0.0, "pickup_code": "AX1", "slip_no": str(i), "location": "X"}
            for i, (s, dd, cd, p, t) in enumerate(rows)]
    return pd.DataFrame(recs, columns=["row_type", "store", "dep_date", "cr_date", "amount", "adj", "recovered", "pickup_code", "slip_no", "location"])


def deposits_frame(rows):
    """rows: (store, dep_day, amount_paise)"""
    return pd.DataFrame([{"store": s, "dep_date": iso(d), "amount": p / 100.0} for s, d, p in rows], columns=["store", "dep_date", "amount"])


def reference(slips, deposits, n_days, openings_paise, stores):
    out = {}
    for s in stores:
        opening = openings_paise.get(s, 0)
        for d in range(n_days):
            dep = sum(p for st, dd, p in deposits if st == s and dd == d)
            c1 = sum(p for st, dd, cd, p, t in slips if t == "COL" and st == s and dd == d and cd == d)
            c2 = sum(p for st, dd, cd, p, t in slips if t == "COL" and st == s and dd == d and cd == d + 1)
            closing = opening + dep - c1 - c2
            out[(s, d)] = (opening, dep, c1, c2, closing)
            opening = closing
    return out


def check(slips, deposits, n_days, openings=None):
    openings = openings or {}
    m = cb.build_model(slips_frame(slips), deposits_frame(deposits), iso(0), iso(n_days - 1), {s: p / 100.0 for s, p in openings.items()})
    stores = [s for s in m.stores]
    want = reference(slips, deposits, n_days, openings, stores)
    for (s, d), (o, dep, c1, c2, cl) in want.items():
        si = m.stores.index(s)
        got = tuple(int(round(float(a[d, si]) * 100)) for a in (m.opening, m.deposit, m.cms1, m.cms2, m.closing))
        assert got == (o, dep, c1, c2, cl), f"{s} day {d}: model {got} vs reference {(o, dep, c1, c2, cl)}"
    for c in cb.block_controls(m, iso(0), iso(n_days - 1)):
        assert c.ok, f"{c.label}: {c.detail}"
    return m


# ---------------------------------------------------------------------------------------------- hand-asserted
def test_same_day_next_day_and_later_credits_by_hand():
    # Store AAA. Deposit Rs 1,000.00 on day 0. Credited: Rs 400.00 same day, Rs 350.00 next day, Rs 250.00 on day 2 (neither column).
    slips = [("AAA", 0, 0, 40000, "COL"), ("AAA", 0, 1, 35000, "COL"), ("AAA", 0, 2, 25000, "COL")]
    m = check(slips, [("AAA", 0, 100000)], 4)
    i = m.stores.index("AAA")
    assert int(round(m.closing[0, i] * 100)) == 100000 - 40000 - 35000 == 25000, "the day+2 credit is in neither column, so it stays in CLOSING"
    assert int(round(m.closing[3, i] * 100)) == 25000, "and it is carried forward unchanged"


def test_a_credit_before_the_deposit_date_or_with_no_date_is_in_neither_column_and_is_listed():
    slips = [("AAA", 3, 1, 10000, "COL"), ("AAA", 3, None, 20000, "COL")]
    m = check(slips, [("AAA", 3, 30000)], 5)
    assert int(round(m.closing[3, m.stores.index("AAA")] * 100)) == 30000
    late = cb.credited_later(slips_frame(slips), iso(0), iso(4))
    flags = sorted(late["flag"])
    assert flags == ["credited BEFORE deposit date", "no credit date"], flags


def test_adjustment_and_zpo_rows_are_not_in_the_grid():
    slips = [("AAA", 0, 0, 10000, "COL"), ("AAA", 0, 0, 99999, "ADJ.DB."), ("AAA", 0, 0, 77777, "ADJ.CR."), ("AAA", 0, 0, 55555, "ZPO")]
    m = check(slips[:1], [("AAA", 0, 10000)], 2)
    m2 = cb.build_model(slips_frame(slips), deposits_frame([("AAA", 0, 10000)]), iso(0), iso(1))
    assert np.array_equal(m.closing, m2.closing), "only COL rows move a block"
    ex = cb.excluded_types(slips_frame(slips), iso(0), iso(1)).set_index("Type")
    assert ex.loc["ADJ.DB.", "Rows"] == 1 and ex.loc["ADJ.CR.", "Rows"] == 1 and ex.loc["ZPO", "Rows"] == 1, "excluded rows are listed, not omitted"


def test_opening_comes_from_config_and_chains_and_a_store_with_no_activity_still_chains():
    m = check([], [], 3, openings={"AAA": 123456})
    assert [int(round(m.closing[d, 0] * 100)) for d in range(3)] == [123456] * 3, "nothing happens: the opening just carries forward"


def test_pseudo_stores_are_listed_after_real_stores_and_never_merged():
    slips = [("ZZZ", 0, 0, 100, "COL"), (cb.UNRESOLVED, 0, 0, 200, "COL"), ("BNK", 0, 0, 300, "COL"), ("AAA", 0, 0, 400, "COL")]
    m = cb.build_model(slips_frame(slips), deposits_frame([]), iso(0), iso(1))
    assert m.stores == ["AAA", "ZZZ", "BNK", cb.UNRESOLVED]
    tot = cb.pseudo_store_totals(slips_frame(slips), iso(0), iso(1))
    assert tot["BNK"] == (1, 3.0) and tot[cb.UNRESOLVED] == (1, 2.0)


def test_window_clipping_keeps_the_chains_own_opening_not_zero():
    m = check([("AAA", 0, 0, 5000, "COL")], [("AAA", 0, 20000), ("AAA", 2, 7000)], 5)
    s = cb.summary_frame(m, iso(2), iso(4)).set_index("store")
    assert round(s.loc["AAA", "opening"] * 100) == 15000, "the window opens at the chain's opening for day 2 (20,000 - 5,000), not at zero"
    assert round(s.loc["AAA", "diff"] * 100) == 15000 + 7000


def test_a_deposit_or_slip_outside_the_range_is_ignored_by_the_blocks_not_misplaced():
    m = cb.build_model(slips_frame([("AAA", 40, 40, 100, "COL")]), deposits_frame([("AAA", -5, 100)]), iso(0), iso(2))
    assert float(m.deposit.sum()) == 0.0 and float(m.cms1.sum()) == 0.0


def test_cent_rounding_never_drifts_over_a_long_chain():
    # 0.10 + 0.20 style values: thirty days of tiny deposits, each credited the next day. The chain must end at exactly zero.
    deposits = [("AAA", d, 10 + (d % 3) * 10) for d in range(29)]
    slips = [("AAA", d, d + 1, 10 + (d % 3) * 10, "COL") for d in range(29)]
    m = check(slips, deposits, 31)
    assert round(float(m.closing[-1, 0]) * 100) == 0


# --------------------------------------------------------------------------------------------- random, vs reference
@pytest.mark.parametrize("seed", range(80))
def test_model_matches_the_independent_paise_reference(seed):
    rng = random.Random(seed)
    stores = rng.sample(["AAA", "BBB", "CCC", "DDD", "BNK", cb.UNRESOLVED], rng.randint(1, 5))
    n_days = rng.randint(1, 12)
    deposits, slips = [], []
    for s in stores:
        for d in range(n_days):
            if rng.random() < 0.7:
                deposits.append((s, d, rng.randint(1, 900000)))
            if rng.random() < 0.6:
                cr = rng.choice([d, d, d + 1, d + 1, d + 2, d + 5, d - 1, None])
                slips.append((s, d, cr, rng.randint(1, 900000), rng.choice(["COL"] * 6 + ["ADJ.DB.", "ZPO"])))
    openings = {s: rng.randint(0, 500000) for s in stores if rng.random() < 0.5}
    check(slips, deposits, n_days, openings)
