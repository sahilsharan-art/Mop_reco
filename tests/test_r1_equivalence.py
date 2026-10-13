"""The vectorized R1 matcher must be indistinguishable from the per-row engine.

`tests/reference_matcher.py` is a frozen copy of the `.at[]`-loop matcher that
produced Run #11 (docs/BASELINE_APR_AUG.md). These tests feed both the same
inputs - randomised, on purpose crowded with duplicates and collisions, since
the only place the two could differ is where several bills compete for one
transaction - and demand identical output on EVERY column plus the identical
consumed set. "Close" is not a pass.
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reference_matcher as ref  # noqa: E402
from mop_reco import r1_bill_txn as new  # noqa: E402

MOPS = ["PAYTM EDC", "PaytmDQR", "Bajajpay UPI", "EDC OTHER", "EDC OTHER AXIS"]
GATEWAYS = ["paytm", "axis", "bajaj"]


def _na(rng, value, p):
    return pd.NA if rng.random() < p else value


def make_inputs(seed: int, n_bills: int = 160, n_txns: int = 150):
    rng = random.Random(seed)
    refs = [f"R{i:03d}" for i in range(rng.choice([6, 12, 40]))]
    amounts = [100.0, 250.5, 99.99, 1132.0, 0.1 + 0.2, 2.675]   # 0.1+0.2 and 2.675 exercise rounding
    stores = ["AAA", "BBB", "CCC"]
    d0 = date(2026, 7, 1)
    dates = [d0 + timedelta(days=i) for i in range(4)]

    bills = pd.DataFrame({
        "business_date": [rng.choice(dates) for _ in range(n_bills)],
        "store": [rng.choice(stores + [None]) if rng.random() < 0.05 else rng.choice(stores)
                  for _ in range(n_bills)],
        "bill_no": [f"B{i}" for i in range(n_bills)],
        "mop": [rng.choice(MOPS) for _ in range(n_bills)],
        "amount": [rng.choice(amounts) for _ in range(n_bills)],
        "paytm_ref": pd.array([_na(rng, rng.choice(refs), .5) for _ in range(n_bills)], dtype="string"),
        "axis_ref": pd.array([_na(rng, rng.choice(refs), .7) for _ in range(n_bills)], dtype="string"),
        "bajaj_ref": pd.array([_na(rng, rng.choice(refs), .8) for _ in range(n_bills)], dtype="string"),
    })
    # EDC OTHER bills never carry a reference in real data.
    other = bills["mop"].isin(ref.EDC_OTHER_MOPS)
    for c in ("paytm_ref", "axis_ref", "bajaj_ref"):
        bills.loc[other, c] = pd.NA

    pool = pd.DataFrame({
        "gateway": [rng.choice(GATEWAYS) for _ in range(n_txns)],
        "txn_ref": pd.array([_na(rng, rng.choice(refs), .03) for _ in range(n_txns)], dtype="string"),
        "business_date": [rng.choice(dates) for _ in range(n_txns)],
        "amount": [rng.choice(amounts) for _ in range(n_txns)],
        "status": "SUCCESS", "txn_type": "ACQUIRING",
        "payment_mode": pd.array([rng.choice(["UPI", "DEBIT_CARD", None]) for _ in range(n_txns)],
                                 dtype="string"),
        "store": [rng.choice(stores + [None]) if rng.random() < 0.05 else rng.choice(stores)
                  for _ in range(n_txns)],
    })
    # A non-contiguous, non-zero-based index, like a filtered real pool.
    pool.index = pd.Index([i * 3 + 7 for i in range(n_txns)])
    return bills, pool


def run_both(bills, pool):
    a1, ca = ref.match_tier1(bills.copy(), pool)
    a2, ca = ref.match_tier2_edc_other(a1, pool, ca)
    b1, cb = new.match_tier1(bills.copy(), pool)
    b2, cb = new.match_tier2_edc_other(b1, pool, cb)
    return (a2, ca), (b2, cb)


def assert_identical(old, newr):
    (a, ca), (b, cb) = old, newr
    assert ca == cb, "consumed transaction sets differ"
    assert list(a.columns) == list(b.columns)
    assert a.index.equals(b.index)
    for col in a.columns:
        x, y = a[col], b[col]
        xn, yn = x.isna().to_numpy(), y.isna().to_numpy()
        xo, yo = x.astype(object).to_numpy(), y.astype(object).to_numpy()
        # Compare only where both are non-null (== on pd.NA raises); null-ness
        # itself must agree exactly.
        same = np.array([xn[i] == yn[i] and (xn[i] or bool(xo[i] == yo[i]))
                         for i in range(len(xo))], dtype=bool)
        bad = ~same
        assert not bad.any(), (
            f"column {col!r} differs on {int(bad.sum())} row(s), first: "
            f"{a.loc[bad, col].head(3).tolist()} vs {b.loc[bad, col].head(3).tolist()}")


@pytest.mark.parametrize("seed", range(250))
def test_vectorized_matcher_equals_per_row_engine(seed):
    bills, pool = make_inputs(seed)
    old, newr = run_both(bills, pool)
    assert_identical(old, newr)
    # The population must actually have exercised matching, or this proves nothing.
    assert old[0]["matched"].any()


def test_the_random_population_exercises_every_branch():
    seen = {"tier1": 0, "tier2": 0, "ambiguous": 0, "date_diff": 0, "second_bill_loses": 0}
    for seed in range(250):
        bills, pool = make_inputs(seed)
        (a, _), _ = run_both(bills, pool)
        seen["tier1"] += int((a["match_tier"] == "Tier 1").sum())
        seen["tier2"] += int((a["match_tier"] == "Matched (EDC OTHER)").sum())
        seen["ambiguous"] += int((a["match_tier"] == "Ambiguous").sum())
        seen["date_diff"] += int((a["date_diff_days"] != 0).sum())
        seen["second_bill_loses"] += int((~a["matched"] & (a["refs_tried"] > 0)).sum())
    assert all(v > 0 for v in seen.values()), seen


def test_one_transaction_never_closes_two_bills():
    bills, pool = make_inputs(1)
    _, (b, _) = run_both(bills, pool)
    used = b.loc[b["matched"], ["matched_gateway", "matched_txn_ref"]]
    tier1 = b.loc[b["match_tier"] == "Tier 1", ["matched_gateway", "matched_txn_ref", "amount"]]
    # (gateway, ref, amount) groups can repeat in the POOL; a bill count above
    # the pool's count for that key would be a double-consume.
    counts = pool.assign(ref=pool["txn_ref"].astype(str), amt=pool["amount"].round(2)) \
        .groupby(["gateway", "ref", "amt"]).size()
    for (gw, r, amt), n_bills in tier1.assign(amt=tier1["amount"].round(2)) \
            .groupby(["matched_gateway", "matched_txn_ref", "amt"]).size().items():
        assert n_bills <= counts.get((gw, r, amt), 0)
    assert len(used) == int(b["matched"].sum())


def test_empty_pool_and_no_eligible_bills():
    bills, pool = make_inputs(3)
    empty = pool.iloc[0:0]
    old, newr = run_both(bills, empty)
    assert_identical(old, newr)
    none_other = bills.copy()
    none_other["mop"] = "PAYTM EDC"
    old, newr = run_both(none_other, pool)
    assert_identical(old, newr)
