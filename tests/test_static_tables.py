"""Presentation-only blocks are plain HTML, never an interactive data grid (2026-10-21): the Particulars matrix and the
Bills / Transactions tables on Summary. No column menu, hover toolbar, index or resize handles - and no figure moves."""
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import match_strip  # noqa: E402


def _ov():
    bills = {k: {"count": n, "value": v} for k, (n, v) in
             {"open": (5122, 5026246.0), "auto": (4360377, 3869846442.67), "manual": (2, 2000.0),
              "total": (4365501, 3874874688.67)}.items()}
    txns = SimpleNamespace(auto_n=3699262, auto_v=3.7e9, manual_n=2, manual_v=1.0, refunded_n=0, refunded_v=0.0,
                           unmapped_n=6728, unmapped_v=4.5e6, total_n=3705992, total_v=3.8e9)
    return SimpleNamespace(bills=bills, txns=txns)


def test_bills_and_transactions_rows_carry_the_same_figures_as_before():
    b = match_strip.bills_rows(_ov())
    assert b[0] == ("Open (unmatched)", "5,122", "Rs 0.50 Cr") and b[-1][0] == "Total" and b[-1][1] == "4,365,501"
    t = match_strip.txn_rows(_ov())
    assert [r[0] for r in t] == ["Mapped to bill - auto", "Mapped to bill - manual", "Refunded to customer",
                                 "Unmapped (open)", "Total"]
    assert t[3][1] == "6,728" and t[-1][1] == "3,705,992"


def test_the_static_table_is_plain_html_with_no_grid_machinery():
    html = match_strip.static_table_html(match_strip.bills_rows(_ov()))
    assert html.startswith("<div class='mop-static-wrap'><table class='mop-static'>") and html.count("<tr") == 5
    assert "mop-static-total" in html and "stDataFrame" not in html and "glide" not in html


def test_the_presentation_blocks_do_not_use_a_data_grid():
    assert "st.dataframe" not in inspect.getsource(match_strip.render_table)
    assert "st.data_editor" not in inspect.getsource(match_strip.render_table)
    summary = (Path(__file__).resolve().parents[1] / "app" / "Summary.py").read_text(encoding="utf-8")
    # 2026-10-26: the particulars matrix is a clickable grid (every Sale / Collected figure opens its store-wise table), so it
    # is built from Streamlit columns and buttons - still never a data grid - and spans the page width.
    block = summary[summary.index("_MX_COLS = "):summary.index("# --- the drill")]
    assert "st.dataframe" not in block and "st.data_editor" not in block and "st.button(text, key=f\"mxb_" in block
    css = (Path(__file__).resolve().parents[1] / "app" / "assets" / "theme.css").read_text(encoding="utf-8")
    assert ".st-key-matrix-card { width: 100% !important;" in css
