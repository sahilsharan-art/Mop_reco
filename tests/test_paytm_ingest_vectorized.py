"""The vectorized Paytm ingest steps must equal the per-cell / per-row code they replaced."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import ingest as ing

AWKWARD = ["'SUCCESS'", "SUCCESS", "'", "''", "' '", "'a'b'", "'a", "a'", "'\n'", "'a\nb'", "x'y'",
           "'20260720111030000285964210320294741'", "", None, "'é'", "''''", "'ab", "'0'"]


def test_strip_quotes_on_columns_equals_the_per_cell_function():
    rng = np.random.default_rng(0)
    frame = pd.DataFrame({f"c{i}": [AWKWARD[j] for j in rng.integers(0, len(AWKWARD), 500)] for i in range(4)})
    expected = frame.copy()
    for c in expected.columns:
        expected[c] = expected[c].map(ing._strip_quotes)
    got = ing._strip_quotes_columns(frame)
    assert list(got.columns) == list(expected.columns) and got.index.equals(expected.index)
    for c in frame.columns:
        a = expected[c].astype(object).where(expected[c].notna(), None).tolist()
        b = got[c].tolist()
        assert a == b, c
        assert all(v is None or isinstance(v, str) for v in b)


def _old_resolve_store_column(resolve, session, source, identifiers, amounts, business_dates):
    stores, rows_with_store, rows_without_date, unmapped_agg = [], 0, 0, {}
    for identifier, amount, business_date in zip(identifiers, amounts, business_dates):
        if pd.isna(business_date):
            stores.append(None)
            rows_without_date += 1
            continue
        result = resolve(session, source, identifier, business_date)
        stores.append(result.store)
        if result.status == "resolved":
            rows_with_store += 1
        else:
            ing._accumulate_unmapped(unmapped_agg, source, identifier, amount, result.status)
    return stores, rows_with_store, rows_without_date, unmapped_agg


def test_memoised_store_resolution_equals_the_per_row_loop(monkeypatch):
    calls = []

    def fake_resolve(session, source, identifier, business_date):
        calls.append((identifier, business_date))
        known = {"M1": "AAA", "M2": "BBB"}
        if identifier in known and business_date >= date(2026, 7, 2):
            return SimpleNamespace(store=known[identifier], status="resolved")
        return SimpleNamespace(store=None, status="unmapped")

    rng = np.random.default_rng(1)
    n = 3000
    ids = rng.choice(["M1", "M2", "M3", None], n)
    amounts = rng.random(n) * 1000
    days = [None if rng.random() < .02 else date(2026, 7, int(rng.integers(1, 6))) for _ in range(n)]
    dates = pd.Series(days, dtype=object)

    monkeypatch.setattr(ing, "resolve_store", fake_resolve)
    old = _old_resolve_store_column(fake_resolve, None, "paytm_mid", ids, amounts, dates)
    calls_old = len(calls)
    calls.clear()
    new = ing._resolve_store_column(None, "paytm_mid", ids, amounts, dates)
    assert new == old                                   # stores, counts and the unmapped aggregate, exactly
    assert len(calls) < calls_old / 20                  # asked once per distinct pair, not once per row


FLOATS = ["12.5", "'12.5'", " 7 ", "1e3", "-0", "-3.25", "+4", "0", "0.00", "", "  ", "nan", "NaN", "'nan'", "'", "''",
          None, "1_000", "infinity", "1.7976931348623157e308", "0.1", "123456789012.34", "'  8.5  '", "1,000"]


def test_to_float_series_equals_the_per_cell_function():
    import pytest as _pytest
    good = [v for v in FLOATS if v != "1,000"]            # "1,000" is not a number to float() either
    rng = np.random.default_rng(3)
    series = pd.Series([good[j] for j in rng.integers(0, len(good), 2000)], dtype=object)
    expected = series.map(ing._to_float)
    got = ing._to_float_series(series)
    assert expected.dtype == got.dtype == np.float64
    assert np.array_equal(expected.to_numpy(), got.to_numpy(), equal_nan=True)
    assert got.index.equals(series.index)
    # a genuinely non-numeric cell raises, exactly as before
    with _pytest.raises(ValueError):
        ing._to_float_series(pd.Series(["1.5", "1,000"], dtype=object))
    with _pytest.raises(ValueError):
        pd.Series(["1.5", "1,000"], dtype=object).map(ing._to_float)
