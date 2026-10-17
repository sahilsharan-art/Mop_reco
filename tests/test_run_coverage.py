"""A period outside the run, or source files the run never read, is SAID - never an empty grid or zeros."""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import run_coverage as rc  # noqa: E402
from mop_reco.db import Run, RunFile, get_engine, get_session, init_db  # noqa: E402

FIRST, LAST = date(2026, 4, 1), date(2026, 8, 31)


def test_a_period_inside_the_run_says_nothing():
    assert rc.period_message(11, FIRST, LAST, date(2026, 4, 1), date(2026, 8, 31)) is None
    assert rc.period_message(11, FIRST, LAST, date(2026, 7, 1), date(2026, 7, 31)) is None


def test_september_outside_a_run_ending_in_august_is_named():
    msg = rc.period_message(11, FIRST, LAST, date(2026, 9, 1), date(2026, 9, 30))
    assert msg == "Run #11 covers 01/04 to 31/08. September is not reconciled - go to Upload and run it."
    assert rc.entirely_outside(FIRST, LAST, date(2026, 9, 1), date(2026, 9, 30))


def test_a_period_that_only_partly_overlaps_names_just_the_missing_months():
    msg = rc.period_message(11, FIRST, LAST, date(2026, 4, 1), date(2026, 10, 15))
    assert "September and October are not reconciled" in msg
    assert not rc.entirely_outside(FIRST, LAST, date(2026, 4, 1), date(2026, 10, 15))


def test_before_the_run_is_named_too():
    msg = rc.period_message(11, FIRST, LAST, date(2026, 3, 10), date(2026, 5, 1))
    assert "March is not reconciled" in msg


def test_unread_source_files_are_found_by_path_and_modified_time(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), business_dates="[]", status="completed"))
    read = tmp_path / "aug.xlsb"
    new = tmp_path / "sep.xlsb"
    read.write_bytes(b"x")
    new.write_bytes(b"y")
    s.add(RunFile(run_id=1, source="pos_bill_wise", file_path=str(read),
                  file_mtime=datetime.fromtimestamp(read.stat().st_mtime), row_count=1))
    s.commit()

    class Status:
        files = (read, new)

    class Config:
        def source_status(self, name):
            return Status() if name == "pos_bill_wise" else type("E", (), {"files": ()})()

    unread = rc.unread_source_files(s, Config(), 1)
    assert [(src, f.name) for src, f in unread] == [("pos_bill_wise", "sep.xlsb")]
    assert "has not read 1 source file(s)" in rc.source_message(1, unread) and "Upload" in rc.source_message(1, unread)
    assert rc.source_message(1, []) is None
