"""Exports. No Streamlit imports — the page supplies the data and the metadata.

Rules:
  - the export covers every row matching the current filters, never the page
  - .xlsx under the row threshold, .csv above it, and the caller is told which
  - reference ids are written as TEXT so a 35-digit transaction id is never
    truncated or turned into 2.02607E+34 by Excel
  - every export carries a header block: run, timestamp, date range, filters,
    row count and total value
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .formatting import count as fmt_count
from .formatting import indian_group

logger = logging.getLogger("mop_reco")

# Above this, .xlsx becomes slow and unwieldy, so we switch to .csv. Excel's
# hard sheet limit is 1,048,576 rows; this is well below it on purpose.
XLSX_ROW_LIMIT = 100_000

# Columns whose values must survive as text. Long numeric-looking ids are the
# whole reason: Excel silently mangles anything past 15 significant digits —
# a 35-digit Paytm reference shows as 2.02607E+34 and everything past the
# 15th digit is gone for good. `slip_no` and `pickup_code` were added
# 2026-09-20 after the same risk was found on the Data quality export (a
# comma-joined CMS slip number is exactly as numeric-looking as a
# transaction id); `mid`/`tid` are listed explicitly even though "id" alone
# already catches them, because the operator named them by hand and a future
# reader should not have to work out why they are covered.
ID_COLUMN_HINTS = ("ref", "_id", "id", "txn", "bill_no", "utr", "rrn", "key",
                   "transaction", "reference", "slip_no", "pickup_code",
                   "mid", "tid", "zoho_no",
                   # A 'HH:MM:SS' time string is exactly as exposed to
                   # Excel's own type-guessing as a numeric id — opened
                   # without an explicit text format, Excel reinterprets it
                   # as a time serial (2026-09-29, bill_time/txn_time).
                   "time")


@dataclass
class ExportMeta:
    run_id: int = None
    run_timestamp: datetime = None
    date_from: object = None
    date_to: object = None
    filters: dict = field(default_factory=dict)
    value_column: str = None
    title: str = "export"


@dataclass
class ExportResult:
    data: bytes
    filename: str
    fmt: str          # "xlsx" | "csv"
    rows: int
    total_value: float = None
    note: str = ""
    # 0-based row the column headers sit on in the xlsx, below the header
    # block. Reported rather than left for the reader to count, so adding a
    # provenance row never silently breaks whoever reads the file back.
    header_row: int = 0


def is_id_column(name: str) -> bool:
    lowered = str(name).lower()
    return any(hint in lowered for hint in ID_COLUMN_HINTS)


def _applied_filters(meta: ExportMeta) -> str:
    filters = meta.filters or {}
    shown = [f"{k}={v}" for k, v in filters.items()
             if v not in (None, "", "Any", "All stores")]
    return "; ".join(shown) if shown else "none"


def _total_value(df: pd.DataFrame, meta: ExportMeta):
    if not meta.value_column or meta.value_column not in df.columns:
        return None
    series = pd.to_numeric(df[meta.value_column], errors="coerce")
    return float(series.sum())


def _meta_rows(df: pd.DataFrame, meta: ExportMeta) -> list:
    total = _total_value(df, meta)
    rows = [
        # Named for what the system actually reconciles today: MOP only. R3a
        # and R3b (cash) are not running, so the name must not claim them.
        ("Application", "Citykart MOP Reconciliation"),
        ("Report", meta.title),
        ("Run", f"#{meta.run_id}" if meta.run_id is not None else "—"),
        ("Run timestamp", meta.run_timestamp.strftime("%Y-%m-%d %H:%M:%S")
         if meta.run_timestamp else "—"),
        ("Business date range", f"{meta.date_from} to {meta.date_to}"
         if meta.date_from else "all loaded dates"),
        ("Filters applied", _applied_filters(meta)),
        ("Rows in this export", fmt_count(len(df))),
        ("Total value (Rs)", indian_group(total) if total is not None else "—"),
        ("Exported at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Note", "Figures are as of the run above, not as of the export time."),
    ]
    return rows


def _stringify_ids(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for column in out.columns:
        if is_id_column(column):
            out[column] = out[column].map(
                lambda v: "" if pd.isna(v) else str(v))
    return out


def build_export(df: pd.DataFrame, meta: ExportMeta,
                 progress=None) -> ExportResult:
    """Returns the bytes to download, the filename, and which format was used.

    `progress` is an optional callable(fraction, message) so a large export can
    show movement instead of appearing frozen.
    """
    def tick(fraction, message):
        if progress:
            progress(fraction, message)

    rows = len(df)
    tick(0.1, f"Preparing {fmt_count(rows)} rows…")

    data = _stringify_ids(df)
    total = _total_value(data, meta)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = f"{meta.title.replace(' ', '_').lower()}_{stamp}"

    if rows > XLSX_ROW_LIMIT:
        tick(0.4, f"{fmt_count(rows)} rows is above the {fmt_count(XLSX_ROW_LIMIT)}-row "
                  "threshold — writing CSV…")
        buffer = io.StringIO()
        for key, value in _meta_rows(data, meta):
            buffer.write(f"# {key},{value}\n")
        buffer.write("\n")
        # QUOTE_NONNUMERIC: every text field (every id column, already
        # stringified above, plus every other text column) is wrapped in
        # quotes; only genuine numeric columns are left bare. A CSV has no
        # type information of its own — an unquoted numeric-looking id opens
        # in Excel as a number and is silently mangled past 15 significant
        # digits, exactly like the .xlsx case above. A quoted field opens as
        # text.
        data.to_csv(buffer, index=False, quoting=csv.QUOTE_NONNUMERIC)
        tick(1.0, "CSV ready")
        payload = buffer.getvalue().encode("utf-8-sig")
        logger.info(f"export: {rows} rows -> csv ({len(payload)/1_048_576:.1f} MB)")
        return ExportResult(payload, f"{base}.csv", "csv", rows, total,
                            note=f"CSV because {fmt_count(rows)} rows is above the "
                                 f"{fmt_count(XLSX_ROW_LIMIT)}-row xlsx threshold.")

    tick(0.4, "Writing Excel…")
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        sheet = "export"
        meta_rows = _meta_rows(data, meta)
        # Header block first, then a blank row, then the data.
        start_row = len(meta_rows) + 1
        data.to_excel(writer, index=False, sheet_name=sheet, startrow=start_row)

        book, ws = writer.book, writer.sheets[sheet]
        bold = book.add_format({"bold": True})
        text_fmt = book.add_format({"num_format": "@"})
        money_fmt = book.add_format({"num_format": "#,##0.00"})

        for i, (key, value) in enumerate(meta_rows):
            ws.write(i, 0, key, bold)
            ws.write(i, 1, str(value))

        tick(0.75, "Formatting columns…")
        first_data_row = start_row + 1
        for col_idx, column in enumerate(data.columns):
            # An all-null column gives a NaN max, and `NaN or 12` keeps the NaN
            # because bool(NaN) is True — so test for null explicitly.
            widest = data[column].astype(str).str.len().max()
            widest = 12 if pd.isna(widest) else int(widest)
            width = min(48, max(12, widest + 2))
            if is_id_column(column):
                # Text, explicitly — every cell is re-written with
                # worksheet.write_string(), never left to to_excel()'s own
                # write(), which will still coerce a numeric-looking string
                # to a number: a 35-digit Paytm reference has to survive
                # exactly, not as 2.02607E+34 with everything past the 15th
                # digit rounded away. The column's own number_format ('@',
                # text) is set too, so a later formula or paste into the
                # column keeps reading it as text. Wide enough that the full
                # id is visible, never just enough to hint at it.
                ws.set_column(col_idx, col_idx, width, text_fmt)
                for row_offset, value in enumerate(data[column]):
                    text = "" if pd.isna(value) else str(value)
                    ws.write_string(first_data_row + row_offset, col_idx,
                                    text, text_fmt)
            elif column == meta.value_column:
                ws.set_column(col_idx, col_idx, width, money_fmt)
            else:
                ws.set_column(col_idx, col_idx, width)
        ws.freeze_panes(start_row + 1, 0)

    tick(1.0, "Excel ready")
    payload = buffer.getvalue()
    logger.info(f"export: {rows} rows -> xlsx ({len(payload)/1_048_576:.1f} MB)")
    return ExportResult(payload, f"{base}.xlsx", "xlsx", rows, total,
                        note=f"Excel, {fmt_count(rows)} rows.",
                        header_row=start_row)
