"""Audit D-007: the Cash Position card showed a NEGATIVE closing balance as a positive figure ("Rs 1.23 Cr") marked only by red.
Operation rows stay positive (the label says Add/Less); the RESULT row keeps its sign."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import re

from dashboard_common import stmt_row, stmt_table


def _result_cell(html):
    return re.search(r"mop-stmt-total.*?<td class='mop-stmt-figure[^']*'>([^<]*)</td>", html, re.S).group(1)


def test_a_negative_result_keeps_its_minus_sign_in_crores():
    html = stmt_table("Cash position", "p", "", "Closing balance at store", -12345678.9, result_bad=True, use_cr=True)
    assert _result_cell(html) == "-Rs 1.23 Cr"
    assert "mop-stmt-figure--bad" in html, "the red treatment stays, it is no longer the only signal"


def test_a_negative_result_keeps_its_minus_sign_in_rupees():
    assert _result_cell(stmt_table("t", "p", "", "Closing", -1500.5, result_bad=True)).startswith("-")


def test_a_positive_or_zero_result_has_no_sign():
    assert _result_cell(stmt_table("t", "p", "", "Closing", 12345678.9, use_cr=True)) == "Rs 1.23 Cr"
    assert _result_cell(stmt_table("t", "p", "", "Receivable", 0.0, use_cr=True)) == "Rs 0.00 Cr"


def test_operation_rows_are_still_shown_positive_whatever_their_sign():
    html = stmt_row("Less: Net cash expense", -497000000.0, use_cr=True)
    assert re.search(r"<td class='mop-stmt-figure'>Rs 49.70 Cr</td>", html), html
