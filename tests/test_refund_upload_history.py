"""A Customer Refund workbook leaves an audit line in the Upload history - who, when, how many lines, what became of them - and no file
(it carries the customers' bank details, which are never stored)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import uploads as up
from mop_reco.db import UploadedFile, get_engine, get_session, init_db


@pytest.fixture
def session(tmp_path):
    s = get_session(init_db(get_engine(tmp_path / "t.sqlite")))
    yield s
    s.close()


def _frame(n=3):
    return pd.DataFrame({"date": ["2026-07-01", "2026-07-15", "2026-06-20"][:n], "transaction_no": list("abc")[:n]})


def _judged(recorded=0, apply=0, hold=0):
    return pd.DataFrame({"verdict": ["recorded"] * recorded + ["apply"] * apply + ["hold"] * hold})


def test_a_check_creates_one_history_line_with_no_file(session):
    row = up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(recorded=2, apply=1), "Aditi Agarwal")
    assert row.source_key == up.REFUND_SOURCE_KEY and row.report_name == "Customer refunds"
    assert row.status == "checked" and row.stored_path == ""          # nothing kept: bank details are never stored
    assert row.row_count == 3 and (row.date_range_start, row.date_range_end) == ("2026-06-20", "2026-07-15")
    assert row.uploaded_by == "Aditi Agarwal" and row.month is None
    assert "2 already recorded" in row.note and "1 ready to record" in row.note and "0 held" in row.note


def test_checking_the_same_workbook_again_updates_the_line_not_adds_one(session):
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(apply=3), "Aditi Agarwal")
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(recorded=3), "Aditi Agarwal")
    rows = session.query(UploadedFile).filter(UploadedFile.source_key == up.REFUND_SOURCE_KEY).all()
    assert len(rows) == 1 and "3 already recorded" in rows[0].note


def test_a_different_workbook_is_its_own_line(session):
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(apply=3), "A")
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 44000, _frame(), _judged(apply=3), "A")
    assert session.query(UploadedFile).count() == 2


def test_recording_marks_the_line_and_a_later_check_never_overwrites_it(session):
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(apply=3), "Aditi Agarwal")
    row = up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(apply=3), "Aditi Agarwal",
                                  recorded=3, recorded_value=4061.5)
    assert row.status == "recorded" and "3 customer refund(s) recorded (Rs 4,061.50) by Aditi Agarwal" in row.note
    after = up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(recorded=3), "Someone")
    assert after.status == "recorded" and "3 customer refund(s) recorded" in after.note       # what was recorded is not rewritten
    assert session.query(UploadedFile).count() == 1


def test_history_lists_the_refund_line_with_the_other_uploads(session):
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(recorded=3), "Aditi Agarwal")
    assert [r.report_name for r in up.history(session)] == ["Customer refunds"]


def test_a_refund_line_does_not_count_as_a_stored_source_file(session):
    """Coverage, the month grid and the comparison read status == 'stored' rows only; a refund line must never be one."""
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(apply=3), "A")
    up.note_refund_workbook(session, "Customer Refunds.xlsx", 43000, _frame(), _judged(apply=3), "A", recorded=3)
    assert session.query(UploadedFile).filter(UploadedFile.status == "stored").count() == 0
