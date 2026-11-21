"""The Polars/calamine POS ingest must produce what the pyxlsb/pandas ingest produced.

Run #11's inputs were read by pyxlsb and pandas. The `.xlsb` reader is now calamine
(fastexcel) and the duplicate-candidate group-by is Polars; these tests hold both
against the originals, which stay in the code as the specification.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import ingest as ing
from mop_reco import io_
from mop_reco.config import load_config


# ---------------------------------------------------------------------------
# duplicate-candidate detection: Polars group-by == the pandas specification
# ---------------------------------------------------------------------------

def frame(seed, n=3000):
    rng = np.random.default_rng(seed)
    stores = [f"S{i:02d}" for i in range(25)] + [None]
    d0 = date(2026, 7, 1)
    return pd.DataFrame({
        "store": rng.choice(stores, n),
        "business_date": [None if rng.random() < .01 else d0 + timedelta(days=int(rng.integers(0, 6)))
                          for _ in range(n)],
        "amount": np.where(rng.random(n) < .01, np.nan, rng.choice([100.0, 250.5, 99.99, 1132.0, 0.1 + 0.2], n)),
        "bill_no": [f"B{i:05d}" for i in range(n)],
        "mop_class": rng.choice(["digital", "digital_noref", "cash", "non_settling"], n),
    })


@pytest.mark.parametrize("seed", range(6))
def test_duplicate_candidates_polars_equals_pandas(seed):
    df = frame(seed)
    old = ing.detect_duplicate_candidates_pandas(df)
    new = ing.detect_duplicate_candidates(df)
    assert len(old) > 0 and len(old) == len(new)
    for a, b in zip(old, new):
        assert a["store"] == b["store"] and a["business_date"] == b["business_date"]
        assert a["amount"] == b["amount"] and list(a["bill_nos"]) == list(b["bill_nos"])


# ---------------------------------------------------------------------------
# per-sheet reference extraction == the one authoritative row parser
# ---------------------------------------------------------------------------

SAMPLES = [
    None, "", "0", "nan", "not json at all", "{broken",
    json.dumps({"head": {}, "body": {"acquirementId": "20260720111030000285964210320294741"}}),
    json.dumps({"head": {}, "body": {"txnId": "20260720111030000285964210320294999", "x": 1}}),
    json.dumps({"head": {}, "body": {"retrievalReferenceNo": "123456789012", "issuerMaskCardNo": "4111XXXX1111"}}),
    json.dumps({"head": {}, "body": {"retrievalReferenceNo": "123456789012"}}),       # no card -> no axis ref
    json.dumps({"head": {}, "body": {"acquirementId": "'20260720111030000285964210320294741.0"}}),
    json.dumps({"rrn": "9988776655", "tid": "T1"}),                                       # flat Bajaj payload
    json.dumps({"head": {}, "body": {"acquirementId": "0x2a"}}),
]


def test_per_sheet_reference_extraction_equals_the_row_parser():
    import polars as pl
    frame_ = pl.DataFrame({"TPEDCRESPONSE": SAMPLES * 40, "BILLNO": [str(i) for i in range(len(SAMPLES) * 40)]},
                          schema={"TPEDCRESPONSE": pl.String, "BILLNO": pl.String})
    out = ing._pos_extract_refs_polars(frame_)
    assert "TPEDCRESPONSE" not in out.columns
    expected = [ing._extract_pos_refs(v) for v in SAMPLES * 40]
    assert out["paytm_ref"].to_list() == [e[0] for e in expected]
    assert out["axis_ref"].to_list() == [e[1] for e in expected]
    assert out["bajaj_ref"].to_list() == [e[2] for e in expected]
    assert out["tpedc_parse_failed"].to_list() == [e[3] for e in expected]
    assert any(e[3] for e in expected) and any(e[0] for e in expected)      # the samples exercise both


# ---------------------------------------------------------------------------
# calamine vs pyxlsb on the real July workbook (first rows of its first sheet)
# ---------------------------------------------------------------------------

def _july_xlsb():
    config = load_config()
    hits = [p for p in config.source_status("pos_bill_wise").files if p.name == "2026-07.xlsb"]
    return hits[0] if hits else None


@pytest.mark.skipif(_july_xlsb() is None, reason="the July POS workbook is not on this machine")
def test_calamine_cells_equal_pyxlsb_cells_on_the_real_workbook():
    path = _july_xlsb()
    n = 6000
    text, floats = ing._POS_TEXT_COLUMNS, ing._POS_FLOAT_COLUMNS
    new, per_sheet = io_.read_xlsb_table(path, text, floats, ing._POS_DATE_COLUMNS,
                                         ing._POS_TIME_COLUMNS, max_rows=n)
    assert len(new) == n

    ref = io_.read_any(path, max_rows=n + 1)            # the original pyxlsb path
    header = [str(c).strip() for c in ref.grid[0]]
    old = pd.DataFrame(ref.grid[1:], columns=header)[list(new.columns)]

    for c in text:
        o = old[c].astype(object).where(old[c].notna(), None).tolist()
        assert o == new[c].astype(object).where(new[c].notna(), None).tolist(), c
    assert (old["BASEAMT"].astype(float).to_numpy() == new["BASEAMT"].to_numpy()).all()
    # dates: identical serials, hence identical business dates
    assert (old["BILLDATE"].astype(float).to_numpy() == new["BILLDATE"].to_numpy()).all()
    assert io_.parse_business_date_series(old["BILLDATE"]) == io_.parse_business_date_series(new["BILLDATE"])
    # times: the raw serial differs by one unit in the last place (calamine hands back a
    # datetime), but what is stored and used - the HH:MM:SS text - is identical
    assert np.abs(old["BILL_TIME"].astype(float).to_numpy() - new["BILL_TIME"].to_numpy()).max() < 1e-12
    assert io_.parse_bill_time_series(old["BILL_TIME"]) == io_.parse_bill_time_series(new["BILL_TIME"])


def test_the_reader_refuses_a_cell_that_is_not_the_declared_type(tmp_path):
    """Strict coercion: a surprise cell raises instead of being quietly changed."""
    import openpyxl
    p = tmp_path / "t.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["BILLNO", "BASEAMT"])
    ws.append(["B1", 10.5])
    ws.append(["B2", "ten"])                          # text where a number is declared
    wb.save(p)
    import fastexcel
    with pytest.raises(Exception) as e:
        io_.read_xlsb_table(p, ["BILLNO"], ["BASEAMT"], [], [])
    assert "strict" in str(e.value).lower() or "coerc" in str(e.value).lower() or "type" in str(e.value).lower()


# ---------------------------------------------------------------------------
# an .xlsx POS export is split across sheets at Excel's row limit - EVERY sheet is read (2026-11-20)
# ---------------------------------------------------------------------------

def _two_sheet_pos_xlsx(path, time_as_text=True):
    import openpyxl
    from datetime import datetime, time as dtime
    wb = openpyxl.Workbook()
    cols = ["PSITE_POSBILL_CODE", "ADMSITE_CODE", "MOPDESC", "MOPSTLMDESC", "TPEDCRESPONSE", "TPBILLREFNUMBER",
            "BILLDATE", "BILL_TIME", "STORE_NAME", "BASETENDER", "BASEBALANCE", "BASEAMT", "BILLNO", "CREATEBY"]
    rows = {"Data": [datetime(2026, 10, 1), datetime(2026, 10, 15)], "Data(2)": [datetime(2026, 10, 16), datetime(2026, 10, 31)]}
    for i, (name, dates) in enumerate(rows.items()):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = name
        ws.append(cols)
        for n, d in enumerate(dates):
            ws.append([f"S-{i}{n}", 1, "Cash", "x", None, None, d, "17:23:26" if time_as_text else dtime(17, 23, 26),
                       "ABD", 100.0, 0.0, 100.0, f"B{i}{n}/1026", "CASH1"])
    wb.save(path)


@pytest.mark.parametrize("time_as_text", [True, False])
def test_every_sheet_of_an_xlsx_pos_export_is_read(tmp_path, time_as_text):
    f = tmp_path / "pos.xlsx"
    _two_sheet_pos_xlsx(f, time_as_text)
    df, per_sheet = ing._read_pos_raw(f)
    assert per_sheet == {"Data": 2, "Data(2)": 2}, "a lost sheet must show up as a number"
    assert len(df) == 4
    assert sorted(df["BILLNO"]) == ["B00/1026", "B01/1026", "B10/1026", "B11/1026"]


def test_the_xlsx_fallback_reader_also_reads_every_sheet(tmp_path, monkeypatch):
    f = tmp_path / "pos.xlsx"
    _two_sheet_pos_xlsx(f)
    monkeypatch.setattr(io_, "read_xlsb_table", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("force fallback")))
    df, per_sheet = ing._read_pos_raw(f)
    assert per_sheet == {"Data": 2, "Data(2)": 2} and len(df) == 4


def test_a_file_with_the_time_as_text_in_one_sheet_and_a_time_in_the_next_reads_fast_and_saves_to_parquet(tmp_path):
    """The October 2026 export: BILL_TIME is text ('15:42:34') in one sheet and a real time in the other. It must
    read through the fast reader (one numeric type for every sheet) and the result must be writable to Parquet."""
    import openpyxl
    from datetime import datetime, time as dtime
    cols = ["PSITE_POSBILL_CODE", "ADMSITE_CODE", "MOPDESC", "MOPSTLMDESC", "TPEDCRESPONSE", "TPBILLREFNUMBER",
            "BILLDATE", "BILL_TIME", "STORE_NAME", "BASETENDER", "BASEBALANCE", "BASEAMT", "BILLNO", "CREATEBY"]
    wb = openpyxl.Workbook()
    for i, t in enumerate(["15:42:34", dtime(17, 23, 26)]):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = f"Data{i}"
        ws.append(cols)
        ws.append([f"S-{i}", 1, "Cash", "x", None, None, datetime(2026, 10, 1 + i), t, "ABD", 10.0, 0.0, 10.0,
                   f"B{i}/1026", "C1"])
    f = tmp_path / "mixed.xlsx"
    wb.save(f)
    df, per_sheet = ing._read_pos_raw(f)
    assert per_sheet == {"Data0": 1, "Data1": 1}
    assert str(df["BILL_TIME"].dtype) == "float64"
    assert io_.parse_bill_time_series(df["BILL_TIME"]) == ["15:42:34", "17:23:26"]
    df["bill_time"] = io_.parse_bill_time_series(df["BILL_TIME"])
    df.drop(columns=["BILL_TIME"]).to_parquet(tmp_path / "out.parquet", index=False)      # must not raise
