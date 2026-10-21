"""Store-wise summary of tasks: one row per store, three groups of (open tickets, volume in Rs) (2026-10-21).

    Store code | Edc other tranx   | High float        | Cms not deposit
               | open ticket | Vol | open ticket | Vol | open ticket | Vol

* "Edc other tranx" counts the OPEN-BILL tasks (a bill raised and no payment found) and sums their rupee amount.
* "High float" counts the high-float tasks and sums the float above threshold.
* "Cms not deposit" counts the CMS-not-done tasks (no deposit, or deposited and not credited) and sums their amount.
* "open ticket" is a COUNT OF TASKS; "Volume" is the sum of the tasks' rupee amounts.

It reads the SAME filter the Store Task page applies (store, search, type, status, business-date range), through the same
`store_tasks._filtered`, so the file always agrees with the screen. No Streamlit imports (CLAUDE.md section 8).
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from sqlalchemy import func

from . import store_tasks as stt
from .db import StoreTask

GROUPS = [(stt.OPEN_BILL, "Edc other tranx"), (stt.HIGH_FLOAT, "High float"), (stt.CMS_NOT_DONE, "Cms not deposit")]
COLUMNS = ["Store code"] + [f"{label} {kind}" for _t, label in GROUPS for kind in ("open ticket", "Volume")]
WIDTHS = {"A": 9.45, "B": 9.82, "C": 6.91, "D": 9.82, "E": 6.91, "F": 9.82, "G": 6.91}
FONT = "Aptos Narrow"


def summary_frame(session, today: date, **filters) -> pd.DataFrame:
    """One row per store, in store order; COLUMNS as defined above. Counts are tasks, volumes are rupees."""
    q = stt._filtered(session, today=today, **filters).with_entities(
        StoreTask.store, StoreTask.task_type, func.count(StoreTask.id), func.sum(StoreTask.amount)
    ).group_by(StoreTask.store, StoreTask.task_type)
    stores: dict = {}
    for store, task_type, n, value in q.all():
        row = stores.setdefault(store, {c: 0 for c in COLUMNS[1:]})
        label = dict(GROUPS).get(task_type)
        if label is None:
            continue
        row[f"{label} open ticket"] = int(n or 0)
        row[f"{label} Volume"] = round(float(value or 0), 2)
    rows = [{"Store code": s, **stores[s]} for s in sorted(stores)]
    return pd.DataFrame(rows, columns=COLUMNS)


def workbook_bytes(frame: pd.DataFrame, provenance: list = None) -> bytes:
    """The sheet exactly as laid out: two header rows, merged and centred, Aptos Narrow 11, no fill, the given column
    widths; a Total row last. A second sheet records which run and filters the figures came from."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Store summary"
    plain, bold = Font(name=FONT, size=11), Font(name=FONT, size=11, bold=True)
    centre = Alignment(horizontal="center", vertical="center")

    ws["A1"] = "Store code"
    ws.merge_cells("A1:A2")
    for i, (_t, label) in enumerate(GROUPS):
        c0 = 2 + 2 * i
        ws.cell(row=1, column=c0, value=label)
        ws.merge_cells(start_row=1, start_column=c0, end_row=1, end_column=c0 + 1)
        ws.cell(row=2, column=c0, value="open ticket")
        ws.cell(row=2, column=c0 + 1, value="Volume")
    for row in (1, 2):
        for col in range(1, 8):
            cell = ws.cell(row=row, column=col)
            cell.font, cell.alignment = bold, centre
    for letter, width in WIDTHS.items():
        ws.column_dimensions[letter].width = width

    r = 3
    for rec in frame.itertuples(index=False):
        ws.cell(row=r, column=1, value=rec[0]).font = plain
        for col in range(2, 8):
            cell = ws.cell(row=r, column=col, value=rec[col - 1])
            cell.font = plain
            if col % 2 == 1:        # Volume: whole rupees, Indian grouping; shrinks to fit the narrow column, never ####
                cell.number_format = "#,##,##0"
                cell.alignment = Alignment(horizontal="right", shrink_to_fit=True)
        r += 1
    if len(frame):
        ws.cell(row=r, column=1, value="Total").font = bold
        for col in range(2, 8):
            letter = get_column_letter(col)
            cell = ws.cell(row=r, column=col, value=f"=SUM({letter}3:{letter}{r - 1})")
            cell.font = bold
            if col % 2 == 1:
                cell.number_format = "#,##,##0"
                cell.alignment = Alignment(horizontal="right", shrink_to_fit=True)
    ws.freeze_panes = "B3"

    about = wb.create_sheet("About")
    lines = [("Generated", datetime.now().strftime("%d/%m/%Y %H:%M")), ("Stores", len(frame))]
    lines += list(provenance or [])
    for i, (k, v) in enumerate(lines, start=1):
        about.cell(row=i, column=1, value=k).font = bold
        about.cell(row=i, column=2, value=str(v)).font = plain
    about.column_dimensions["A"].width = 22
    about.column_dimensions["B"].width = 60

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
