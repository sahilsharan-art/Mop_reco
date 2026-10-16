"""store_sheet_source._read through Polars must equal the per-file pandas read."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_sheet_source as sss


def _pandas_read(files, columns=None):
    frame = pd.concat([pd.read_parquet(p, columns=columns) for p in files], ignore_index=True)
    if "business_date" in frame.columns:
        frame["business_date"] = frame["business_date"].astype(str)
    return frame


def _write_days(root: Path, nulls_first=False):
    import datetime as dt
    files = []
    for i in range(1, 5):
        day = dt.date(2026, 7, i)
        df = pd.DataFrame({
            "business_date": [day] * 3, "store": ["A", "B", None], "bill_no": ["x1", "x2", "x3"],
            "mop": ["Cash", "PAYTM EDC", "Cash"], "amount": [10.5, 20.25, 0.1 + 0.2],
            # an all-null column on the first day, text afterwards: pandas copes, Polars may not
            "extra": [None, None, None] if (i == 1 and nulls_first) else ["a", None, "c"],
        })
        p = root / f"2026-07-0{i}.parquet"
        df.to_parquet(p, index=False)
        files.append(p)
    return files


def test_polars_read_equals_pandas_read(tmp_path):
    files = _write_days(tmp_path)
    for columns in (None, ["business_date", "store", "amount"], sss.POS_COLUMNS):
        cols = None if columns is None else [c for c in columns if c in ("business_date", "store", "bill_no", "mop", "amount", "extra")]
        pd.testing.assert_frame_equal(sss._read_polars(files, cols), _pandas_read(files, cols))


def test_a_type_mismatch_between_files_falls_back_to_pandas_not_to_an_error(tmp_path, monkeypatch):
    files = _write_days(tmp_path, nulls_first=True)
    monkeypatch.setattr(sss, "WAREHOUSE", tmp_path.parent)
    folder = tmp_path.parent / "src_mismatch"
    folder.mkdir(exist_ok=True)
    for p in files:
        (folder / p.name).write_bytes(p.read_bytes())
    got = sss._read("src_mismatch")                      # Polars cannot unify it; the read still works
    pd.testing.assert_frame_equal(got, _pandas_read(sorted(folder.glob("*.parquet"))))


@pytest.mark.parametrize("source", ["ptc", "axis_cms"])
def test_real_warehouse_sources_read_identically(source):
    folder = sss.WAREHOUSE / source
    files = [p for p in sorted(folder.glob("*.parquet")) if not p.stem.startswith("_")] if folder.exists() else []
    if not files:
        pytest.skip(f"no {source} warehouse on this machine")
    pd.testing.assert_frame_equal(sss._read_polars(files), _pandas_read(files))


def test_real_pos_range_reads_identically():
    folder = sss.WAREHOUSE / "pos_bill_wise"
    files = [p for p in sorted(folder.glob("2026-07-*.parquet"))] if folder.exists() else []
    if not files:
        pytest.skip("no POS warehouse on this machine")
    pd.testing.assert_frame_equal(sss._read_polars(files, sss.POS_COLUMNS), _pandas_read(files, sss.POS_COLUMNS))
