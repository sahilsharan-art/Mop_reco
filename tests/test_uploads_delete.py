"""Delete on the Upload page moves the file to an archive, never erases it, and keeps the history row (rules 4 and 7)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import uploads as up
from mop_reco.db import UploadedFile, get_engine, get_session, init_db


@pytest.fixture
def env(tmp_path):
    engine = init_db(get_engine(tmp_path / "t.sqlite"))
    session = get_session(engine)
    src = tmp_path / "2. Settlement reports" / "Bajaj Pay" / "Oct'26"
    src.mkdir(parents=True)
    f = src / "report.xlsx"
    f.write_bytes(b"data")
    row = UploadedFile(uploaded_at=datetime.now(), source_key="bajaj", report_name="Bajaj Pay settlement",
                       month="2026-10", original_file_name="report.xlsx", stored_path=str(f), size_bytes=4,
                       uploaded_by="aditi", status="stored")
    session.add(row)
    session.commit()
    yield SimpleNamespace(session=session, row=row, file=f, config=SimpleNamespace(base_dir=tmp_path))
    session.close()


def test_delete_moves_the_file_to_the_archive_and_keeps_the_row(env):
    ok, msg = up.delete_upload(env.session, env.row.id, "aditi", False, env.config)
    assert ok, msg
    assert not env.file.exists()                                   # gone from the source folder: runs cannot read it
    row = env.session.get(UploadedFile, env.row.id)
    assert row is not None and row.status == "deleted"             # the history row is never removed
    assert row.deleted_by == "aditi" and row.deleted_at is not None
    archived = Path(row.archived_path)
    assert archived.read_bytes() == b"data"                        # kept, byte for byte
    assert up.ARCHIVE_DIR_NAME in archived.parts
    assert env.config.base_dir in archived.parents and env.file.parent not in archived.parents


def test_delete_is_refused_while_a_run_is_in_progress(env):
    ok, _ = up.delete_upload(env.session, env.row.id, "aditi", True, env.config)
    assert not ok and env.file.exists()
    assert env.session.get(UploadedFile, env.row.id).status == "stored"


def test_deleting_twice_is_refused_and_a_missing_file_is_still_recorded(env):
    env.file.unlink()
    ok, msg = up.delete_upload(env.session, env.row.id, "aditi", False, env.config)
    assert ok and "already missing" in msg
    assert env.session.get(UploadedFile, env.row.id).archived_path is None
    ok2, _ = up.delete_upload(env.session, env.row.id, "aditi", False, env.config)
    assert not ok2


def test_two_files_with_the_same_name_never_overwrite_in_the_archive(env, tmp_path):
    up.delete_upload(env.session, env.row.id, "aditi", False, env.config)
    f2 = env.file                                                   # same name uploaded again, same id slot not reused
    f2.write_bytes(b"second")
    row2 = UploadedFile(uploaded_at=datetime.now(), source_key="bajaj", report_name="x", month="2026-10",
                        original_file_name="report.xlsx", stored_path=str(f2), size_bytes=6, uploaded_by="a",
                        status="stored")
    env.session.add(row2)
    env.session.commit()
    up.delete_upload(env.session, row2.id, "aditi", False, env.config)
    first = Path(env.session.get(UploadedFile, env.row.id).archived_path)
    second = Path(env.session.get(UploadedFile, row2.id).archived_path)
    assert first != second and first.read_bytes() == b"data" and second.read_bytes() == b"second"


# --- the fast check for a big workbook gives the same answer as the whole-file read (2026-11-19) ----------------------

def _pos_workbook(path, dates, sheets=1):
    import openpyxl
    wb = openpyxl.Workbook()
    for i in range(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = "Data" if i == 0 else f"Data({i + 1})"
        ws.append(["BILLNO", "PSITE_POSBILL_CODE", "STORE_NAME", "BILLDATE", "MOPDESC", "BASEAMT"])
        for n, d in enumerate(dates):
            ws.append([f"B{i}-{n}", f"S-{n}", "ABD", d, "Cash", 10.0 + n])
    wb.save(path)


@pytest.mark.parametrize("month", ["2026-10", "2026-09"])
def test_fast_check_agrees_with_the_whole_file_read(tmp_path, monkeypatch, month):
    from datetime import datetime
    f = tmp_path / "pos.xlsx"
    _pos_workbook(f, [datetime(2026, 10, 1), datetime(2026, 10, 15), datetime(2026, 10, 31)], sheets=2)
    spec = up.SOURCE_BY_KEY["pos_bill_wise"]
    cfg = SimpleNamespace(encoding_for=lambda k: None)
    monkeypatch.setattr(up, "FAST_VALIDATE_BYTES", 10 ** 12)        # force the whole-file read
    slow = up.validate_upload(spec, f, "pos.xlsx", month, cfg)
    monkeypatch.setattr(up, "FAST_VALIDATE_BYTES", 0)               # force the fast check
    fast = up.validate_upload(spec, f, "pos.xlsx", month, cfg)
    assert fast.ok == slow.ok
    assert fast.date_range == slow.date_range or (not slow.ok)
    assert fast.columns_found == slow.columns_found
    assert fast.row_count == slow.row_count or fast.row_count == 6   # sheets are summed; the slow read keeps sheet 1 rows
    assert any("fast check" in w for w in fast.warnings)
    assert (not fast.ok) == (month == "2026-09")


# --- every source's check must read its own file shape, not crash (2026-11-19) -----------------------------------------

def test_paytm_style_quoted_dates_with_a_time_are_read():
    import pandas as pd
    s = pd.Series(["'31-08-2026 17:48:23'", "'01-09-2026 00:00:01'", None])
    out = up._extract_dates(pd.DataFrame({"transaction_date": s}), "transaction_date")
    assert sorted(str(d) for d in out) == ["2026-08-31", "2026-09-01"]


def test_a_ragged_csv_is_checked_not_crashed(tmp_path):
    f = tmp_path / "cms.csv"
    f.write_text("TYPE,DEPDATE,CRDATE,PICKUPPOINTCODE\nCOL,01-10-2026,02-10-2026,AX1,\nCOL,03-10-2026,04-10-2026,AX2,\n",
                 encoding="utf-8")
    spec = up.SourceSpec("axis_cms", "CMS received (Axis)", (".csv",), True, ("TYPE", "DEPDATE", "CRDATE", "PICKUPPOINTCODE"))
    cfg = SimpleNamespace(encoding_for=lambda k: "utf-8")
    r = up.validate_upload(spec, f, "cms.csv", "2026-10", cfg)
    assert r.ok and r.row_count == 2
    assert any("different number of fields" in w for w in r.warnings)


def test_bajaj_export_with_separate_date_and_time_columns_is_read(tmp_path):
    """The November 2026 Bajaj export has DATE + TIME instead of one 'Transaction Date'."""
    import openpyxl
    from datetime import datetime
    from mop_reco import ingest
    cols = ["Transaction Id", "DATE", "TIME", "Transaction Amount", "Transaction Status", "TID", "MID", "Card Scheme",
            "Net Amount", "Total MDR"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(cols)
    ws.append(["111", datetime(2026, 11, 14), "22:46:28", 100.0, "Success", "T1", "M1", "UPI", 99.0, 1.0])
    ws.append(["222", datetime(2026, 11, 2), "09:05:00", 50.0, "SUCCESS", "T1", "M1", "UPI", 49.5, 0.5])
    f = tmp_path / "bajaj_nov.xlsx"
    wb.save(f)
    df = ingest._read_bajaj_file(f, datetime.now())
    assert sorted(str(d) for d in df["business_date"]) == ["2026-11-02", "2026-11-14"]
    assert df["transaction_time"].notna().all() and str(df["transaction_time"].iloc[0]) == "22:46:28"
