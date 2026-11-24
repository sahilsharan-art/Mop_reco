"""UAT U01-U03 (audit Phase 7): the Upload page refuses a file for a plain reason, before anything is stored.

A wrong file type, a file whose dates are not in the chosen month, and a file missing a required column must each be refused with a message that
names the problem. Small real workbooks are built here and passed to the same `validate_upload()` the page calls."""
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import uploads as up  # noqa: E402
from mop_reco.config import load_config  # noqa: E402

SPEC = next(s for s in up.SOURCE_REGISTRY if s.key == "axis_edc")
COLS = list(SPEC.required_columns)


def _xlsx(tmp_path, columns=None, dates=(datetime(2026, 7, 20), datetime(2026, 7, 21)), name="axis.xlsx"):
    cols = columns or COLS
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(cols)
    for i, d in enumerate(dates):
        ws.append([d if c == "TRAN_DATE" else (f"R{i}" if c == "RRN" else (1.0 if "AMT" in c else "x")) for c in cols])
    p = tmp_path / name
    wb.save(p)
    return p


def _check(path, month="2026-07"):
    return up.validate_upload(SPEC, path, path.name, month, load_config())


def test_a_correct_file_for_the_right_month_is_accepted(tmp_path):
    prev = _check(_xlsx(tmp_path))
    assert prev.ok, prev.errors
    assert prev.row_count == 2


def test_a_wrong_file_type_is_refused_naming_what_is_accepted(tmp_path):
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.4 not a spreadsheet")
    prev = _check(p)
    assert not prev.ok and ".xlsx" in " ".join(prev.errors) and ".pdf" in " ".join(prev.errors)


def test_a_file_for_the_wrong_month_is_refused(tmp_path):
    prev = _check(_xlsx(tmp_path), month="2026-08")
    assert not prev.ok, "a July file must not be accepted as August"
    text = " ".join(prev.errors).lower()
    assert "2026-07" in text or "july" in text or "month" in text, prev.errors


@pytest.mark.parametrize("missing", ["MID", "RRN", "GROSS_AMT"])
def test_a_missing_column_is_named(tmp_path, missing):
    prev = _check(_xlsx(tmp_path, columns=[c for c in COLS if c != missing]))
    assert not prev.ok
    assert missing in " ".join(prev.errors), prev.errors


def test_the_page_never_stores_a_refused_file(tmp_path):
    """validate_upload only reads: after a refusal the original is untouched and nothing new appears beside it."""
    p = _xlsx(tmp_path, columns=[c for c in COLS if c != "MID"])
    before = sorted(x.name for x in tmp_path.iterdir())
    _check(p)
    assert sorted(x.name for x in tmp_path.iterdir()) == before and p.exists()
