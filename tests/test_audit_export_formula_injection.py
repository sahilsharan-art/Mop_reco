"""Audit S-4: a typed remark that starts with = + - or @ must not become a live formula in an exported workbook or CSV.

Excel and LibreOffice run a cell beginning with `=` as a formula (a `=HYPERLINK(...)` or a DDE call). Remarks, customer names, notes and uploaded
file names are typed by people, then exported and opened by others. Numeric columns (a genuine negative amount) must stay numbers."""
import io
import sys
from pathlib import Path

import openpyxl
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import export as ex  # noqa: E402

HOSTILE = ['=HYPERLINK("http://example.invalid/x","click")', "=1+1", "+SUM(1,1)", "-2+3", "@SUM(1,1)", "\t=1+1"]


def _frame():
    return pd.DataFrame({"store": ["AAA"] * len(HOSTILE), "remark": HOSTILE, "amount": [-1500.5] * len(HOSTILE),
                         "transaction_id": ["20260720111030000285964210320294741"] * len(HOSTILE)})


def _xlsx(df):
    res = ex.build_export(df, ex.ExportMeta(title="t", value_column="amount"))
    assert res.fmt == "xlsx"
    return openpyxl.load_workbook(io.BytesIO(res.data)).active


def _csv(df, monkeypatch):
    monkeypatch.setattr(ex, "XLSX_ROW_LIMIT", 1)
    res = ex.build_export(df, ex.ExportMeta(title="t", value_column="amount"))
    assert res.fmt == "csv"
    return res.data.decode("utf-8-sig")


def test_no_text_cell_in_the_xlsx_is_a_formula():
    ws = _xlsx(_frame())
    formulas = [(c.coordinate, c.value) for row in ws.iter_rows() for c in row if c.data_type == "f"]
    assert not formulas, f"live formulas in the export: {formulas}"


def test_the_remark_text_is_preserved_exactly_in_the_xlsx():
    ws = _xlsx(_frame())
    seen = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    for h in HOSTILE:
        assert h in seen, f"{h!r} was altered or lost"


def test_a_genuine_negative_amount_stays_a_number_in_the_xlsx():
    ws = _xlsx(_frame())
    nums = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, (int, float))]
    assert nums and all(n == -1500.5 for n in nums)


def test_the_long_transaction_id_still_survives_as_text():
    ws = _xlsx(_frame())
    assert "20260720111030000285964210320294741" in [c.value for row in ws.iter_rows() for c in row]


def test_urls_in_text_do_not_become_hyperlinks():
    ws = _xlsx(pd.DataFrame({"remark": ["see http://example.invalid/phish for details"], "amount": [1.0]}))
    assert not any(c.hyperlink for row in ws.iter_rows() for c in row)


def test_the_csv_path_neutralises_leading_formula_characters(monkeypatch):
    text = _csv(_frame(), monkeypatch)
    cells = [c.strip('"') for line in text.splitlines() for c in line.split('","')]
    bad = [c for c in cells if c.startswith(("=", "+", "@", "\t", "\r"))]
    assert not bad, f"unneutralised CSV cells: {bad}"
    assert "'=1+1" in text and "'-2+3" in text, "the original text is kept, behind an apostrophe"


def test_plain_numbers_and_ordinary_text_are_not_prefixed_in_csv(monkeypatch):
    df = pd.DataFrame({"remark": ["-1500", "+91", "ok", "- see note"], "amount": [-1.0, -2.0, 3.0, 4.0]})
    text = _csv(df, monkeypatch)
    assert '"-1500"' in text and '"+91"' in text and '"ok"' in text and "'- see note" in text
    assert ",-1.0" in text, "a numeric column's negative amount is untouched"


def test_every_workbook_writer_in_the_app_turns_off_string_formulas():
    """A new xlsxwriter writer that forgets the setting would reopen S-4. Every writer must pass strings_to_formulas=False."""
    import re
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"xlsxwriter\.Workbook\(|ExcelWriter\([^\n]*engine=\"xlsxwriter\"", text):
            # the whole call: up to the end of the line for Workbook(...), up to the " as writer" for a with-statement
            stop = text.index(" as ", m.start()) if m.group(0).startswith("ExcelWriter") else text.index("\n", m.start())
            if "strings_to_formulas" not in text[m.start():stop]:
                offenders.append(f"{path.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not offenders, f"writers without strings_to_formulas=False: {offenders}"
