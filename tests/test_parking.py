"""Digital lines awaiting a gateway file that has not arrived are parked, not opened (CLAUDE.md 6.28)."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import parking  # noqa: E402

CFG = {"awaiting_source": {"axis": {"only_mops": ["EDC OTHER AXIS"]}}}


def _frame(rows):
    cols = ["business_date", "mop", "matched", "match_tier", "paytm_ref", "axis_ref", "bajaj_ref"]
    return pd.DataFrame(rows, columns=cols)


def test_only_unmatched_axis_only_lines_after_the_last_axis_date_are_parked():
    day = _frame([
        ["2026-10-05", "PAYTM EDC", False, None, None, "R1", None],        # axis ref only  -> parked
        ["2026-10-05", "PAYTM EDC", False, None, "T1", "R1", None],        # card on a Paytm EDC: Paytm ref + Axis ref -> parked
        ["2026-10-05", "PAYTM EDC", False, None, "T1", None, None],        # Paytm ref only, no card indication -> genuine
        ["2026-10-05", "PAYTM EDC", False, None, None, None, None],        # no reference at all -> genuine
        ["2026-10-05", "EDC OTHER AXIS", False, None, None, None, None],   # MOP only Axis can serve -> parked
        ["2026-10-05", "EDC OTHER", False, None, None, None, None],        # may be any gateway -> genuine
        ["2026-10-05", "PAYTM EDC", True, "Tier 1", None, "R1", None],     # matched -> never parked
        ["2026-10-05", "EDC OTHER AXIS", False, "Ambiguous", None, None, None],  # has candidates -> not parked
        ["2026-09-30", "PAYTM EDC", False, None, None, "R1", None],        # Axis covers the date -> genuine
    ])
    kept, parked = parking.split_parked(day, "2026-09-30", CFG)
    assert list(parked.index) == [0, 1, 4]
    assert len(kept) + len(parked) == len(day)          # nothing vanishes


def test_no_axis_data_at_all_parks_every_axis_only_line():
    day = _frame([["2026-07-01", "PAYTM EDC", False, None, None, "R1", None]])
    assert parking.parked_mask(day, None, CFG).all()
