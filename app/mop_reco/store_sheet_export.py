"""The store-wise sheet as Excel — five tabs, one cut of the data each,
real formulas throughout.

Separate from `export.py` because this sheet has a fixed, operator-supplied
layout rather than being a generic dump of a filtered table.

    Tab 1  "By date"            one row per business date, all stores
                                 consolidated, plus a TOTAL row
    Tab 2  "By store and date"  one row per store per date — the full detail
    Tab 3  "By store"           one row per store for the whole range, plus
                                 a grand total row
    Tab 4  "Excluded types"     ADJ.CR., ADJ.DB., ZPO — counts and values,
                                 plus the full J-contribution breakdown
    Tab 5  "Controls"           the 8 controls, and whether each holds

Four things are deliberate:

- **C, G, K, M and N are written as Excel FORMULAS, not values**, on every
  tab. The operator asked to be able to trace any cell, and a pasted number
  cannot be traced. The workbook re-proves its own arithmetic when opened:
  if C did not equal D+E, Excel itself would say so.
- **Every TOTAL / grand-total row's SUM formulas point at the DETAIL rows on
  the "By store and date" tab directly**, via a cross-sheet reference —
  never at another summary tab — so tracing a total never depends on
  trusting a block in between, and the by-date total and the by-store grand
  total can never quietly drift apart: they are the same SUM over the same
  range.
- **Indian number grouping** via the `#,##,##0.00` format, which groups by
  two after the first three digits, so 489898591 reads 48,98,98,591.00
  rather than 489,898,591.00.
- **Each tab carries its own compact provenance line** (run, timestamp,
  range) so a tab pulled out on its own is still auditable — the operator
  said "so I can use whichever I need", which means any one of the five may
  travel alone.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

import pandas as pd

from .store_sheet import COLUMNS, FIELDS
from .store_sheet import verify_date_rows_equal_store_sums as _verify_dates

logger = logging.getLogger("mop_reco")

# Groups by two after the first three — Indian lakh/crore. Column I is
# carried positive (2026-09-17); a negative here is a genuine negative
# figure (e.g. a store-day where credits exceed deposits, K < 0), not a
# sign convention, and still needs its leading minus.
INDIAN = "#,##,##0.00"

DETAIL_SHEET = "By store and date"

# field name -> its Excel column letter. A..N is the operator's contract, and
# the fields are written in exactly that order, so position gives the letter.
LETTER = {name: chr(ord("A") + i) for i, name in enumerate(FIELDS)}

# The derived columns, as Excel formulas. The column letters are written
# literally because A..N IS the format — a formula reading "=D5+E5" is what
# the operator expects to see when they click the cell, and indirection
# through field names would only obscure it.
FORMULAS = {
    "total_sale": "=D{r}+E{r}",
    "digital_receivable": "=D{r}-F{r}",
    "cash_receivable": "=I{r}-J{r}",
    "closing_balance": "=H{r}+E{r}-I{r}+L{r}",
    "float_amount": "=H{r}-I{r}",
}

def _formula(name: str, row: int) -> str:
    return FORMULAS[name].format(r=row)


def _formats(book):
    return {
        "title": book.add_format({"bold": True, "font_size": 13}),
        "label": book.add_format({"font_color": "#5d6d7e"}),
        "head": book.add_format({"bold": True, "bg_color": "#eaeded",
                                 "border": 1, "align": "center",
                                 "text_wrap": True, "valign": "vcenter"}),
        "block": book.add_format({"bold": True, "font_size": 11,
                                  "font_color": "#1c2833"}),
        "money": book.add_format({"num_format": INDIAN}),
        "money_total": book.add_format({"num_format": INDIAN, "bold": True,
                                        "bg_color": "#d6eaf8", "top": 2,
                                        "bottom": 2}),
        "text": book.add_format({}),
        "text_total": book.add_format({"bold": True, "bg_color": "#d6eaf8",
                                       "top": 2, "bottom": 2}),
        "note": book.add_format({"font_color": "#5d6d7e", "italic": True}),
        "green": book.add_format({"bold": True, "font_color": "#1e8449"}),
        "red": book.add_format({"bold": True, "font_color": "#c0392b"}),
    }


def _provenance(ws, formats, tab_purpose: str, run_id, run_timestamp,
               date_from, date_to) -> int:
    """A compact header — run, timestamp, range — so a tab pulled out on its
    own is still auditable. Returns the next free row."""
    row = 0
    ws.write(row, 0, "Citykart MOP Reconciliation — daily store-wise sale "
                     "and cash", formats["title"])
    row += 1
    ws.write(row, 0, tab_purpose, formats["note"])
    row += 1
    for key, value in (
        ("Run", f"#{run_id}" if run_id is not None else "—"),
        ("Run timestamp", run_timestamp.strftime("%Y-%m-%d %H:%M:%S")
         if run_timestamp else "—"),
        ("Business date range", f"{date_from} to {date_to}"),
        ("Exported at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ):
        ws.write(row, 0, key, formats["label"])
        ws.write(row, 1, str(value))
        row += 1
    return row + 1


def _header_row(ws, row, formats) -> int:
    for index, (letter, _name, text_label) in enumerate(COLUMNS):
        ws.write(row, index, f"{letter}  {text_label}", formats["head"])
    return row + 1


def _column_widths(ws):
    ws.set_column(0, 0, 24)
    ws.set_column(1, 1, 16)
    ws.set_column(2, len(FIELDS) - 1, 17)


def _write_row(ws, row: int, record, money_format, text_format):
    """One data row. Derived columns go in as formulas, with the computed
    value cached so a reader that does not evaluate formulas still sees the
    number."""
    excel_row = row + 1
    ws.write(row, 0, str(record["date"]), text_format)
    ws.write(row, 1, str(record["store"]), text_format)
    for index, name in enumerate(FIELDS):
        if index < 2:
            continue
        value = float(record[name])
        if name in FORMULAS:
            ws.write_formula(row, index, _formula(name, excel_row),
                             money_format, value)
        else:
            ws.write_number(row, index, value, money_format)


def _write_cross_sheet_total_row(ws, row: int, record, detail_first: int,
                                 detail_last: int, money_format, text_format):
    """A TOTAL / grand-total row whose SUM formulas point at the DETAIL rows
    on the "By store and date" tab — never at another summary tab — so
    tracing it never depends on trusting a block in between."""
    excel_row = row + 1
    ws.write(row, 0, str(record["date"]), text_format)
    ws.write(row, 1, str(record["store"]), text_format)
    for index, name in enumerate(FIELDS):
        if index < 2:
            continue
        value = float(record[name])
        letter = LETTER[name]
        if name in FORMULAS:
            ws.write_formula(row, index, _formula(name, excel_row),
                             money_format, value)
        elif name == "opening_balance":
            # The whole range's opening is the FIRST detail row's opening —
            # never a sum, which would double-count the carry-forward across
            # every day in the range.
            ws.write_formula(
                row, index, f"='{DETAIL_SHEET}'!{letter}{detail_first}",
                money_format, value)
        else:
            ws.write_formula(
                row, index,
                f"=SUM('{DETAIL_SHEET}'!{letter}{detail_first}:"
                f"{letter}{detail_last})",
                money_format, value)


def build_workbook(sheet: pd.DataFrame, summary: pd.DataFrame,
                   excluded: pd.DataFrame, run_id=None, run_timestamp=None,
                   date_from=None, date_to=None, checks=None,
                   summary_by_store: pd.DataFrame = None,
                   grand_total: pd.DataFrame = None,
                   contribution: pd.DataFrame = None,
                   other_types: pd.DataFrame = None) -> bytes:
    """The whole deliverable as one .xlsx, five tabs.

    `summary_by_store`, `grand_total` and `other_types` are optional for
    backward compatibility with a caller that only has the by-date summary;
    when omitted, they are derived here so a tab is never missing silently.
    """
    from .store_sheet import grand_total_row as _grand_total_row
    from .store_sheet import other_type_summary as _other_type_summary
    from .store_sheet import summarise_by_store as _summarise_by_store

    if summary_by_store is None:
        summary_by_store = _summarise_by_store(sheet)
    if grand_total is None:
        grand_total = _grand_total_row(sheet)
    if other_types is None:
        other_types = pd.DataFrame(columns=["type", "rows", "value", "feeds_J"])

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        book = writer.book
        fmt = _formats(book)

        # Sheets are added in the order they should appear as tabs, but the
        # DETAIL tab's content is written FIRST so the other tabs' grand
        # totals can reference its row range the moment they are written.
        ws_by_date = book.add_worksheet("By date")
        ws_detail = book.add_worksheet(DETAIL_SHEET)
        ws_by_store = book.add_worksheet("By store")
        ws_excluded = book.add_worksheet("Excluded types")
        ws_controls = book.add_worksheet("Controls")
        for name, ws in (("By date", ws_by_date), (DETAIL_SHEET, ws_detail),
                         ("By store", ws_by_store),
                         ("Excluded types", ws_excluded),
                         ("Controls", ws_controls)):
            writer.sheets[name] = ws

        # =====================================================================
        # Tab 2 — By store and date (written first: the others reference it)
        # =====================================================================
        row = _provenance(ws_detail, fmt, "One row per store per business "
                                          "date — the full detail.",
                          run_id, run_timestamp, date_from, date_to)
        row = _header_row(ws_detail, row, fmt)
        detail_header = row - 1
        detail_first = row + 1
        for _, record in sheet.iterrows():
            _write_row(ws_detail, row, record, fmt["money"], fmt["text"])
            row += 1
        detail_last = row
        ws_detail.freeze_panes(detail_header + 1, 2)
        ws_detail.autofilter(detail_header, 0, max(detail_last - 1, detail_header),
                             len(FIELDS) - 1)
        _column_widths(ws_detail)

        # =====================================================================
        # Tab 1 — By date, all stores consolidated, plus TOTAL
        # =====================================================================
        row = _provenance(ws_by_date, fmt, "One row per business date, all "
                                           "stores consolidated, plus a "
                                           "TOTAL row.",
                          run_id, run_timestamp, date_from, date_to)
        row = _header_row(ws_by_date, row, fmt)
        by_date_header = row - 1
        per_date = summary[summary["date"] != "TOTAL"]
        for _, record in per_date.iterrows():
            _write_row(ws_by_date, row, record, fmt["money"], fmt["text"])
            row += 1
        by_date_last = row
        total_row = summary[summary["date"] == "TOTAL"]
        if not total_row.empty:
            _write_cross_sheet_total_row(
                ws_by_date, row, total_row.iloc[0], detail_first, detail_last,
                fmt["money_total"], fmt["text_total"])
            row += 1
        ws_by_date.freeze_panes(by_date_header + 1, 2)
        ws_by_date.autofilter(by_date_header, 0, max(by_date_last - 1, by_date_header),
                              len(FIELDS) - 1)
        _column_widths(ws_by_date)

        # A consistency check finer than any lettered control: every
        # consolidated date row must equal the sum of that date's own store
        # rows, not just the grand total against the whole detail.
        mismatches = _verify_dates(sheet, summary)
        row += 1
        ws_by_date.write(row, 0, "Consistency — each date equals its "
                                 "stores' sum", fmt["block"])
        row += 1
        if mismatches.empty:
            ws_by_date.write(row, 0, "HOLDS — every date's consolidated row "
                                     "equals the sum of that date's store "
                                     "rows.", fmt["green"])
        else:
            ws_by_date.write(row, 0, f"FAILS — {len(mismatches)} date/column "
                                     "combination(s) disagree. See the "
                                     "Controls tab.", fmt["red"])

        # =====================================================================
        # Tab 3 — By store, whole range, plus grand total
        # =====================================================================
        row = _provenance(ws_by_store, fmt, "One row per store for the whole "
                                            "range, plus a grand total.",
                          run_id, run_timestamp, date_from, date_to)
        row = _header_row(ws_by_store, row, fmt)
        by_store_header = row - 1
        for _, record in summary_by_store.iterrows():
            _write_row(ws_by_store, row, record, fmt["money"], fmt["text"])
            row += 1
        by_store_last = row
        if not grand_total.empty:
            _write_cross_sheet_total_row(
                ws_by_store, row, grand_total.iloc[0], detail_first, detail_last,
                fmt["money_total"], fmt["text_total"])
            row += 1
        ws_by_store.write(
            row + 1, 0, "Opening is that store's FIRST date's opening and "
                        "closing its LAST date's closing — not a sum of "
                        "balances, which would double-count the "
                        "carry-forward.", fmt["note"])
        ws_by_store.freeze_panes(by_store_header + 1, 2)
        ws_by_store.autofilter(by_store_header, 0,
                               max(by_store_last - 1, by_store_header),
                               len(FIELDS) - 1)
        _column_widths(ws_by_store)

        # =====================================================================
        # Tab 4 — Excluded types
        # =====================================================================
        row = _provenance(ws_excluded, fmt, "ADJ.CR., ADJ.DB. and ZPO — "
                                            "counts and values.",
                          run_id, run_timestamp, date_from, date_to)
        ws_excluded.write(row, 0, "Non-COL CMS types", fmt["block"])
        row += 1
        ws_excluded.write(
            row, 0, "ADJ.CR. and ADJ.DB. DO feed column J on the sheet, each "
                    "via its own source column (ADJ.CR. from the ADJ column, "
                    "ADJ.DB. from RECOVEREDAMOUNT, subtracted) — not from "
                    "SLIPAMOUNT. TYPE = ZPO never reaches J at all, on "
                    "instruction, and also carries no deposit date.",
            fmt["note"])
        row += 2
        if other_types is None or other_types.empty:
            ws_excluded.write(row, 0, "None present in this range.", fmt["text"])
            row += 1
        else:
            for index, column in enumerate(["Type", "Rows", "Value (Rs)",
                                            "Feeds column J?"]):
                ws_excluded.write(row, index, column, fmt["head"])
            row += 1
            for _, record in other_types.iterrows():
                ws_excluded.write(row, 0, str(record["type"]), fmt["text"])
                ws_excluded.write_number(row, 1, int(record["rows"]), fmt["text"])
                ws_excluded.write_number(row, 2, float(record["value"]), fmt["money"])
                ws_excluded.write(row, 3, "Yes" if record["feeds_J"] else "No — ignored",
                                  fmt["text"])
                row += 1
        row += 2

        ws_excluded.write(row, 0, "Column J — full contribution by CMS type",
                          fmt["block"])
        row += 1
        ws_excluded.write(
            row, 0, "Every type that feeds J, including COL, so the whole "
                    "figure is visible in one table — never one blended "
                    "SLIPAMOUNT total.", fmt["note"])
        row += 2
        if contribution is None or contribution.empty:
            ws_excluded.write(row, 0, "No CMS data in this range.", fmt["text"])
            row += 1
        else:
            for index, column in enumerate(["Type", "Source column", "Rows",
                                            "Contribution to J (Rs)"]):
                ws_excluded.write(row, index, column, fmt["head"])
            row += 1
            for _, record in contribution.iterrows():
                ws_excluded.write(row, 0, str(record["type"]), fmt["text"])
                ws_excluded.write(row, 1, str(record["source_column"]), fmt["text"])
                ws_excluded.write_number(row, 2, int(record["rows"]), fmt["text"])
                ws_excluded.write_number(row, 3, float(record["value"]), fmt["money"])
                row += 1
            row += 1
            ws_excluded.write(row, 0, "Total contribution to J", fmt["label"])
            ws_excluded.write_number(row, 3, float(contribution["value"].sum()),
                                     fmt["money"])
            row += 1

        row += 2
        ws_excluded.write(row, 0, "TYPE = ZPO — detail by date and store",
                          fmt["block"])
        row += 1
        if excluded is None or excluded.empty:
            ws_excluded.write(row, 0, "None present in this range.", fmt["text"])
        else:
            for index, column in enumerate(["Type", "Date", "Store", "Rows",
                                            "Value"]):
                ws_excluded.write(row, index, column, fmt["head"])
            row += 1
            for _, record in excluded.iterrows():
                ws_excluded.write(row, 0, str(record["type"]), fmt["text"])
                ws_excluded.write(row, 1, str(record["date"]), fmt["text"])
                ws_excluded.write(row, 2, str(record["store"]), fmt["text"])
                ws_excluded.write_number(row, 3, int(record["rows"]), fmt["text"])
                ws_excluded.write_number(row, 4, float(record["value"]), fmt["money"])
                row += 1
        ws_excluded.set_column(0, 0, 26)
        ws_excluded.set_column(1, 3, 16)

        # =====================================================================
        # Tab 5 — Controls
        # =====================================================================
        row = _provenance(ws_controls, fmt, "The 8 controls, and whether "
                                            "each holds.",
                          run_id, run_timestamp, date_from, date_to)
        if checks is not None:
            for control in checks.controls:
                ws_controls.write(row, 0, f"({control.key})", fmt["label"])
                ws_controls.write(row, 1, "HOLDS" if control.holds else "FAILS",
                                  fmt["green"] if control.holds else fmt["red"])
                ws_controls.write(row, 2, control.what, fmt["text"])
                ws_controls.write(row, 6, control.detail, fmt["note"])
                row += 1
        row += 1
        ws_controls.write(row, 0, "(date=Σstores)", fmt["label"])
        ws_controls.write(row, 1, "HOLDS" if mismatches.empty else "FAILS",
                          fmt["green"] if mismatches.empty else fmt["red"])
        ws_controls.write(row, 2, "each consolidated date row equals the "
                                  "sum of that date's store rows", fmt["text"])
        ws_controls.write(
            row, 6,
            "every additive column agrees" if mismatches.empty
            else f"{len(mismatches)} date/column combination(s) disagree",
            fmt["note"])
        ws_controls.set_column(0, 0, 16)
        ws_controls.set_column(2, 2, 55)
        ws_controls.set_column(6, 6, 55)

    payload = buffer.getvalue()
    logger.info(f"store sheet export: {len(sheet)} detail rows, 5 tabs, "
                f"{len(payload)/1_048_576:.1f} MB")
    return payload
