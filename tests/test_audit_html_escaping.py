"""Audit S-3: text from the database or from an uploaded file must not reach raw HTML unescaped.

A file name is chosen by whoever uploads it. The Upload history put it straight into `st.markdown(..., unsafe_allow_html=True)`: an
`<img>` in a file name rendered for everyone (Streamlit strips event handlers, so no script ran, but arbitrary markup - a tracking image, a
fake link - did). Tiles, the status pill and the Store Task tables also held database text."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>&"\''


class _Box:
    def __init__(self):
        self.html = ""

    def markdown(self, s, **kw):
        self.html += s


def test_tile_escapes_label_value_note_and_tooltip():
    import dashboard_common as dc
    box = _Box()
    dc.tile(box, HOSTILE, HOSTILE, note=HOSTILE, tooltip=HOSTILE)
    assert "<img" not in box.html and "<script" not in box.html, box.html
    assert "&lt;img" in box.html and "&quot;" in box.html


def test_tile_still_renders_ordinary_figures_unchanged():
    import dashboard_common as dc
    box = _Box()
    dc.tile(box, "TOTAL SALE", "Rs 1.23 Cr", note="99.97% - 659,945 of 660,150 lines")
    assert "Rs 1.23 Cr" in box.html and "99.97% - 659,945 of 660,150 lines" in box.html and "&amp;" not in box.html


def test_store_task_tables_escape_store_codes():
    import pandas as pd
    from mop_reco import store_task_summary as sts
    assert sts._esc(HOSTILE) == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&lt;script&gt;alert(2)&lt;/script&gt;&amp;&quot;&#x27;"
    # the two builders must call it
    src = Path(sts.__file__).read_text(encoding="utf-8")
    assert "_esc(rec[0])" in src and "_esc(r[0])" in src


def test_upload_history_escapes_file_name_uploader_and_report():
    src = (Path(__file__).resolve().parents[1] / "app" / "pages" / "6_Upload.py").read_text(encoding="utf-8")
    for field in ("original_file_name", "uploaded_by", "report_name"):
        assert re.search(r"html\.escape\(str\(r\." + field + r"\)\)", src), f"{field} is not escaped in the Upload history"
    assert not re.search(r"\{r\.(original_file_name|uploaded_by|report_name)\}", src), "a raw {r.<field>} is still interpolated into HTML"


def test_no_other_page_interpolates_an_upload_or_remark_field_into_html():
    """Fields that people type or upload must never appear raw inside an unsafe_allow_html string."""
    risky = re.compile(r"\{[^{}]*\b(remark|reply|customer_name|original_file_name|uploaded_by|zoho_no|note|override_reason)\b[^{}]*\}")
    offenders = []
    for path in sorted((Path(__file__).resolve().parents[1] / "app").rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            window = " ".join(lines[max(0, i - 1):i + 2])
            if "unsafe_allow_html=True" in window and risky.search(line) and "html.escape" not in line and "_esc(" not in line:
                offenders.append(f"{path.name}:{i + 1}")
    assert not offenders, offenders


def test_dates_on_the_masters_page_use_the_shared_formatter():
    """UAT Z02: the Masters page showed '2026-11-14' (ISO) while every other page reads DD/MM/YYYY."""
    src = (Path(__file__).resolve().parents[1] / "app" / "pages" / "5_Masters.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if ("st.error(" in line or "st.success(" in line or "st.info(" in line) and "{business_date}" in line:
            raise AssertionError(f"raw business_date in a message: {line.strip()}")
    assert src.count("display_date(business_date)") >= 3 and "display_date(sample.date)" in src
