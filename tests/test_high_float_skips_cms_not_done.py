"""A store-day that is already a CMS not done is not counted as high float (2026-10-27)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_tasks as stt  # noqa: E402


def _sf():
    # 2026-10-12 and 13 are a Monday and a Tuesday, so no weekend auto-remark; float stays above the 10,000 threshold on both
    return pd.DataFrame({"store": ["AAA", "AAA"], "business_date": ["2026-10-12", "2026-10-13"],
                         "float_amount": [50000.0, 60000.0], "opening_balance": [50000.0, 60000.0], "cash_deposit": [0.0, 0.0]})


def test_without_a_skip_both_days_are_high_float():
    g = stt.high_float_candidates(_sf(), {})
    assert [c.key for c in g.active] == ["AAA|2026-10-12", "AAA|2026-10-13"] and g.excluded_n == 0


def test_a_cms_not_done_day_is_left_out_and_reported_not_lost():
    g = stt.high_float_candidates(_sf(), {}, skip_days={"AAA|2026-10-12"})
    assert [c.key for c in g.active] == ["AAA|2026-10-13"]
    assert g.excluded_n == 1 and g.excluded_value == 50000.0
