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

Report layout, 2026-09-30 — Store/Date/Digital block/spacer/Cash block
------------------------------------------------------------------------
This module's own report layout (`COLUMN_ORDER` below) is deliberately
different from `store_sheet.FIELDS`, the engine's internal A..N column
order that the on-screen table and every other consumer still use,
untouched. `_write_row`/`_write_cross_sheet_total_row` already read a
record by FIELD NAME, never by position, so reordering the columns THIS
MODULE writes has zero effect on the engine, the dashboard page, or any
other test of `store_sheet.py` itself.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

import pandas as pd

from .formatting import display_date
from .store_sheet import verify_date_rows_equal_store_sums as _verify_dates

logger = logging.getLogger("mop_reco")

# Groups by two after the first three — Indian lakh/crore. Column I is
# carried positive (2026-09-17); a negative here is a genuine negative
# figure (e.g. a store-day where credits exceed deposits, K < 0), not a
# sign convention, and still needs its leading minus.
INDIAN = "#,##,##0.00"

DETAIL_SHEET = "By store and date"

# ---------------------------------------------------------------------------
# The export's own column order — Store first, Date second, the digital
# block grouped together (Total sale/Digital sale/Digital received/Digital
# receivable), a blank spacer, then the cash block with Cash sale moved to
# sit right after Opening balance. `None` marks the spacer: no field, no
# header text, just a fill running the height of the sheet. Float amount is
# last and detail-sheet-only (2c) — dropped from `_columns(False)`.
# ---------------------------------------------------------------------------
COLUMN_ORDER = [
    "store", "date", "total_sale", "digital_sale", "digital_received",
    "digital_receivable", None, "opening_balance", "cash_sale",
    "cash_deposit", "cash_received", "cash_receivable",
    "cash_expense_income", "closing_balance", "float_amount",
]

LABELS = {
    "store": "Store", "date": "Date", "total_sale": "Total sale",
    "digital_sale": "Digital sale", "digital_received": "Digital received",
    "digital_receivable": "Digital receivable",
    "opening_balance": "Opening balance", "cash_sale": "Cash sale",
    "cash_deposit": "Cash deposit", "cash_received": "Cash received",
    "cash_receivable": "Cash receivable",
    "cash_expense_income": "Cash expense/income",
    "closing_balance": "Closing balance", "float_amount": "Float amount",
}

# Letters as they land AFTER the reorder — computed from POSITION, never
# hand-typed, so a future reorder can't leave a formula pointing at a stale
# letter (exactly the failure mode the operator flagged: "this is where a
# reorder usually breaks a workbook"). The spacer still consumes a letter
# slot (G) even though it never gets a dict entry, so H onward land right.
LETTER = {name: chr(ord("A") + i) for i, name in enumerate(COLUMN_ORDER) if name}

# The two group-header spans (2e) — "Digital Reco" over the digital block,
# "Cash Reco" over the cash block. Float amount sits after Closing balance
# on the detail sheet, deliberately OUTSIDE both groups — it's a Float-page
# concept riding along on the detail tab, not part of either reconciliation.
DIGITAL_GROUP = ("total_sale", "digital_sale", "digital_received", "digital_receivable")
CASH_GROUP = ("opening_balance", "cash_sale", "cash_deposit", "cash_received",
             "cash_receivable", "cash_expense_income", "closing_balance")

# DERIVED formulas, written in terms of the OTHER field's own letter via
# `LETTER` — never a hand-typed literal letter — so this is safe against a
# future reorder by construction, not by careful proofreading.
FORMULAS = {
    "total_sale": lambda r: f"={LETTER['digital_sale']}{r}+{LETTER['cash_sale']}{r}",
    "digital_receivable": lambda r: (
        f"={LETTER['digital_sale']}{r}-{LETTER['digital_received']}{r}"),
    "cash_receivable": lambda r: (
        f"={LETTER['cash_deposit']}{r}-{LETTER['cash_received']}{r}"),
    "closing_balance": lambda r: (
        f"={LETTER['opening_balance']}{r}+{LETTER['cash_sale']}{r}"
        f"-{LETTER['cash_deposit']}{r}+{LETTER['cash_expense_income']}{r}"),
    "float_amount": lambda r: (
        f"={LETTER['opening_balance']}{r}-{LETTER['cash_deposit']}{r}"),
}

# Which columns carry their own permanent data-row fill (2f) — everything
# else has none. Digital sale and Cash sale share the identical hex value,
# so they share one format family ("dsale") internally; Total sale is its
# own, darker tint ("tsale").
TINT_FAMILY = {"total_sale": "tsale", "digital_sale": "dsale", "cash_sale": "dsale"}
TINT_HEX = {"tsale": "#D9D9D9", "dsale": "#E7E6E6"}


def _formula(name: str, row: int) -> str:
    return FORMULAS[name](row)


def _columns(include_float: bool) -> list:
    return COLUMN_ORDER if include_float else COLUMN_ORDER[:-1]


def _formats(book):
    HEADER_FILL = "#EAEDED"
    SPACER_FILL = "#EDEDED"
    TOTAL_FILL = "#D6EAF8"

    def money(fill=None, total=False):
        opts = {"num_format": INDIAN}
        if fill:
            opts["bg_color"] = fill
        if total:
            opts.update(bold=True, top=2, bottom=2)
        return book.add_format(opts)

    def text(fill=None, total=False):
        opts = {}
        if fill:
            opts["bg_color"] = fill
        if total:
            opts.update(bold=True, top=2, bottom=2)
        return book.add_format(opts)

    fmt = {
        "title": book.add_format({"bold": True, "font_size": 13}),
        "label": book.add_format({"font_color": "#5d6d7e"}),
        "note": book.add_format({"font_color": "#5d6d7e", "italic": True}),
        "green": book.add_format({"bold": True, "font_color": "#1e8449"}),
        "red": book.add_format({"bold": True, "font_color": "#c0392b"}),
        # Group headers (2e) — bold, no fill, left-aligned over their block.
        "group": book.add_format({"bold": True, "align": "left"}),
        # The plain column-header row (2f) — every cell #EAEDED, except the
        # spacer's own #EDEDED (2d).
        "head": book.add_format({"bold": True, "bg_color": HEADER_FILL,
                                 "border": 1, "align": "center",
                                 "text_wrap": True, "valign": "vcenter"}),
        "head_spacer": book.add_format({"bold": True, "bg_color": SPACER_FILL,
                                        "border": 1}),
        # The spacer's own data-row fill (2d) — no number format, it never
        # carries a value.
        "spacer": book.add_format({"bg_color": SPACER_FILL}),
        # Section headings on the Excluded types tab — unrelated to this
        # rework, kept as it always was.
        "block": book.add_format({"bold": True, "font_size": 11,
                                  "font_color": "#1c2833"}),
        "money": money(),
        "money_total": money(fill=TOTAL_FILL, total=True),
        "text": text(),
        "text_total": text(fill=TOTAL_FILL, total=True),
    }
    for family, hexval in TINT_HEX.items():
        fmt[f"money_{family}"] = money(fill=hexval)
        fmt[f"money_{family}_total"] = money(fill=hexval, total=True)
    return fmt


def _money_format(fmt, name: str, is_total: bool):
    family = TINT_FAMILY.get(name)
    if family is None:
        return fmt["money_total"] if is_total else fmt["money"]
    return fmt[f"money_{family}_total"] if is_total else fmt[f"money_{family}"]


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


def _span(names) -> tuple:
    cols = COLUMN_ORDER  # group spans are defined against the full order —
                        # both groups sit before Float amount, so this is
                        # identical whether or not the sheet carries it.
    return cols.index(names[0]), cols.index(names[-1])


def _write_headers(ws, row: int, formats, include_float: bool) -> int:
    """Two header rows (2e): group headers, then the plain column-header
    row (2a — no letter prefixes). Returns the first data row."""
    cols = _columns(include_float)
    group_row, head_row = row, row + 1

    d_first, d_last = _span(DIGITAL_GROUP)
    c_first, c_last = _span(CASH_GROUP)
    ws.merge_range(group_row, d_first, group_row, d_last,
                   "Digital Reco", formats["group"])
    ws.merge_range(group_row, c_first, group_row, c_last,
                   "Cash Reco", formats["group"])

    for index, name in enumerate(cols):
        if name is None:
            ws.write_blank(head_row, index, None, formats["head_spacer"])
        else:
            ws.write(head_row, index, LABELS[name], formats["head"])
    return head_row + 1


def _column_widths(ws, include_float: bool):
    for index, name in enumerate(_columns(include_float)):
        if name is None:
            ws.set_column(index, index, 1.63)
        elif name == "store":
            ws.set_column(index, index, 16)
        elif name == "date":
            ws.set_column(index, index, 14)
        else:
            ws.set_column(index, index, 17)


def _cell_text(record, name: str) -> str:
    return display_date(record[name]) if name == "date" else str(record[name])


def _write_row(ws, row: int, record, formats, include_float: bool):
    """One data row. Derived columns go in as formulas, with the computed
    value cached so a reader that does not evaluate formulas still sees the
    number. The spacer column gets its own fill and nothing else (2d)."""
    excel_row = row + 1
    for index, name in enumerate(_columns(include_float)):
        if name is None:
            ws.write_blank(row, index, None, formats["spacer"])
            continue
        if name in ("store", "date"):
            ws.write(row, index, _cell_text(record, name), formats["text"])
            continue
        value = float(record[name])
        fmt = _money_format(formats, name, is_total=False)
        if name in FORMULAS:
            ws.write_formula(row, index, _formula(name, excel_row), fmt, value)
        else:
            ws.write_number(row, index, value, fmt)


def _write_cross_sheet_total_row(ws, row: int, record, detail_first: int,
                                 detail_last: int, formats, include_float: bool):
    """A TOTAL / grand-total row whose SUM formulas point at the DETAIL rows
    on the "By store and date" tab — never at another summary tab — so
    tracing it never depends on trusting a block in between. Columns with
    their own permanent tint (2f) keep that tint here too, bold and
    bordered, rather than the plain blue total-row fill."""
    excel_row = row + 1
    for index, name in enumerate(_columns(include_float)):
        if name is None:
            ws.write_blank(row, index, None, formats["spacer"])
            continue
        if name in ("store", "date"):
            ws.write(row, index, _cell_text(record, name), formats["text_total"])
            continue
        value = float(record[name])
        letter = LETTER[name]
        fmt = _money_format(formats, name, is_total=True)
        if name in FORMULAS:
            ws.write_formula(row, index, _formula(name, excel_row), fmt, value)
        elif name == "opening_balance":
            # The whole range's opening is the FIRST detail row's opening —
            # never a sum, which would double-count the carry-forward across
            # every day in the range.
            ws.write_formula(
                row, index, f"='{DETAIL_SHEET}'!{letter}{detail_first}",
                fmt, value)
        else:
            ws.write_formula(
                row, index,
                f"=SUM('{DETAIL_SHEET}'!{letter}{detail_first}:"
                f"{letter}{detail_last})",
                fmt, value)


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

    n_cols_full = len(_columns(True))
    n_cols_plain = len(_columns(False))

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
        row = _write_headers(ws_detail, row, fmt, include_float=True)
        detail_header = row - 1
        detail_first = row + 1
        for _, record in sheet.iterrows():
            _write_row(ws_detail, row, record, fmt, include_float=True)
            row += 1
        detail_last = row
        ws_detail.freeze_panes(detail_header + 1, 2)
        ws_detail.autofilter(detail_header, 0, max(detail_last - 1, detail_header),
                             n_cols_full - 1)
        _column_widths(ws_detail, include_float=True)

        # =====================================================================
        # Tab 1 — By date, all stores consolidated, plus TOTAL
        # =====================================================================
        row = _provenance(ws_by_date, fmt, "One row per business date, all "
                                           "stores consolidated, plus a "
                                           "TOTAL row.",
                          run_id, run_timestamp, date_from, date_to)
        row = _write_headers(ws_by_date, row, fmt, include_float=False)
        by_date_header = row - 1
        per_date = summary[summary["date"] != "TOTAL"]
        for _, record in per_date.iterrows():
            _write_row(ws_by_date, row, record, fmt, include_float=False)
            row += 1
        by_date_last = row
        total_row = summary[summary["date"] == "TOTAL"]
        if not total_row.empty:
            _write_cross_sheet_total_row(
                ws_by_date, row, total_row.iloc[0], detail_first, detail_last,
                fmt, include_float=False)
            row += 1
        ws_by_date.freeze_panes(by_date_header + 1, 2)
        ws_by_date.autofilter(by_date_header, 0, max(by_date_last - 1, by_date_header),
                              n_cols_plain - 1)
        _column_widths(ws_by_date, include_float=False)

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
        row = _write_headers(ws_by_store, row, fmt, include_float=False)
        by_store_header = row - 1
        for _, record in summary_by_store.iterrows():
            _write_row(ws_by_store, row, record, fmt, include_float=False)
            row += 1
        by_store_last = row
        if not grand_total.empty:
            _write_cross_sheet_total_row(
                ws_by_store, row, grand_total.iloc[0], detail_first, detail_last,
                fmt, include_float=False)
            row += 1
        ws_by_store.write(
            row + 1, 0, "Opening is that store's FIRST date's opening and "
                        "closing its LAST date's closing — not a sum of "
                        "balances, which would double-count the "
                        "carry-forward.", fmt["note"])
        ws_by_store.freeze_panes(by_store_header + 1, 2)
        ws_by_store.autofilter(by_store_header, 0,
                               max(by_store_last - 1, by_store_header),
                               n_cols_plain - 1)
        _column_widths(ws_by_store, include_float=False)

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
