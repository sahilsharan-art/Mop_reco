"""Audit defect D-001 (2026-11-24): the CMS overlap key left out the row TYPE, so a slip's collection (COL) in one monthly file and the
adjustment (ADJ.DB. / ADJ.CR.) in a later file looked like one re-exported row and only the newer file's row survived. Cash received
(column J) then lost the collection or the reversal.

Every expected figure below is worked out BY HAND from the CMS rule (COL = SLIPAMOUNT, ADJ.CR. = ADJ column, ADJ.DB. = minus
RECOVEREDAMOUNT, all on the DEPOSIT date), not produced by calling the application's own sheet logic on the same rows twice.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import ingest, store_sheet  # noqa: E402


class _Cfg:
    def __init__(self, files):
        self._files = files

    def source_status(self, source):
        return SimpleNamespace(files=self._files, configured_path="test")


def _slip(row_type, slip, amount, adj=0.0, recovered=0.0, dep="2026-08-14", pickup="AX31939", store="JMI"):
    return {"TYPE": row_type, "PICKUPPOINTCODE": pickup, "DEPDATE": dep, "SLIPNO": slip, "SLIPAMOUNT": amount, "ADJ": adj,
            "RECOVEREDAMOUNT": recovered, "store": store}


def _load(tmp_path, older_rows, newer_rows):
    older, newer = tmp_path / "older.dat", tmp_path / "newer.dat"
    older.write_text("x")
    newer.write_text("x")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_100_000, 1_700_100_000))

    def read_one(path, mtime):
        return pd.DataFrame(older_rows if path.name == "older.dat" else newer_rows)

    return ingest.read_source_files(_Cfg([older, newer]), "axis_cms", None, read_one, "_cms_key", key_label="pickup_key",
                                    make_key=ingest._cms_overlap_key)


def _j(df, date, store):
    got = store_sheet.cms_received_by_deposit_date(df)
    row = got[(got["date"] == date) & (got["store"] == store)]
    return float(row["cash_received"].iloc[0]) if len(row) else 0.0


def test_a_collection_and_its_reversal_in_different_files_both_survive(tmp_path):
    # Hand calculation for store JMI, deposit date 2026-08-14:
    #   COL 61,720 (slip 1) + COL 87,500 (slip 2) - ADJ.DB. 87,500 (slip 2 reversed) = 61,720
    older = [_slip("COL", "88675593", 61720.0), _slip("COL", "88489019", 87500.0)]
    newer = [_slip("ADJ.DB.", "88489019", 87500.0, recovered=87500.0)]
    df, files, report = _load(tmp_path, older, newer)
    assert len(df) == 3, "the collection and its reversal are different records and must both be kept"
    assert report == [] and sorted(df["TYPE"]) == ["ADJ.DB.", "COL", "COL"]
    assert _j(df, "2026-08-14", "JMI") == 61720.0


def test_a_collection_and_its_adjustment_credit_in_different_files_both_survive(tmp_path):
    # COL 47,400 + ADJ.CR. (the ADJ column, 103 - NOT the 47,400 slip amount) = 47,503
    older = [_slip("COL", "5001", 47400.0)]
    newer = [_slip("ADJ.CR.", "5001", 47400.0, adj=103.0)]
    df, files, report = _load(tmp_path, older, newer)
    assert len(df) == 2 and report == []
    assert _j(df, "2026-08-14", "JMI") == 47503.0


def test_a_true_reexport_of_the_same_row_still_supersedes_and_is_reported(tmp_path):
    # The SAME type, slip, pickup and deposit date in both files is a re-export: the newer file wins, one row is reported dropped.
    older = [_slip("COL", "7001", 1000.0)]
    newer = [_slip("COL", "7001", 1100.0)]
    df, files, report = _load(tmp_path, older, newer)
    assert len(df) == 1 and float(df["SLIPAMOUNT"].iloc[0]) == 1100.0
    assert sum(r["rows_dropped"] for r in report) == 1
    assert _j(df, "2026-08-14", "JMI") == 1100.0


def test_rows_with_no_deposit_date_are_never_compared(tmp_path):
    zpo = {"TYPE": "ZPO", "PICKUPPOINTCODE": "AX38737", "DEPDATE": None, "SLIPNO": "9", "SLIPAMOUNT": 500.0, "ADJ": 0.0,
           "RECOVEREDAMOUNT": 0.0, "store": None}
    df, files, report = _load(tmp_path, [dict(zpo)], [dict(zpo)])
    assert len(df) == 2 and report == []


def test_the_key_names_pickup_date_slip_and_type():
    c = pd.DataFrame([_slip("COL", "12", 1.0), _slip("ADJ.DB.", "12", 1.0, recovered=1.0), _slip(" col ", "12", 1.0, pickup=" AX31939 ")])
    ingest._cms_overlap_key(c)
    assert c["_cms_key"].tolist() == ["AX31939|2026-08-14|12|COL", "AX31939|2026-08-14|12|ADJ.DB.", "AX31939|2026-08-14|12|COL"]


def test_the_old_key_would_have_dropped_the_collection(tmp_path):
    """Documents the defect: the same scenario with the ORIGINAL key (no TYPE). Kept as evidence of what was wrong."""
    def old_key(c):
        c["_cms_key"] = (c["PICKUPPOINTCODE"].astype(str).str.strip() + "|" + c["DEPDATE"].astype(str) + "|" + c["SLIPNO"].astype(str).str.strip())

    older, newer = tmp_path / "older.dat", tmp_path / "newer.dat"
    older.write_text("x")
    newer.write_text("x")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_100_000, 1_700_100_000))
    rows = {"older.dat": [_slip("COL", "88675593", 61720.0), _slip("COL", "88489019", 87500.0)],
            "newer.dat": [_slip("ADJ.DB.", "88489019", 87500.0, recovered=87500.0)]}
    df, _, report = ingest.read_source_files(_Cfg([older, newer]), "axis_cms", None, lambda p, m: pd.DataFrame(rows[p.name]), "_cms_key",
                                             key_label="pickup_key", make_key=old_key)
    assert len(df) == 2 and sorted(df["TYPE"]) == ["ADJ.DB.", "COL"]            # the 87,500 collection was dropped...
    assert _j(df, "2026-08-14", "JMI") == 61720.0 - 87500.0                      # ...so the sheet showed -25,780 instead of 61,720
