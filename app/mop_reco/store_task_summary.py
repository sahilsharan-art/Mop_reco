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
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font
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


TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "task_summary_template.xlsx"


def workbook_bytes(frame: pd.DataFrame, provenance: list = None) -> bytes:
    """The operator's own sheet (`assets/task_summary_template.xlsx`, supplied 2026-10-21) with the stores filled in below
    its two header rows: merged and centred headers, Aptos Narrow 11, no fill, its borders and column widths all come from
    the template itself, so the file cannot drift from the format asked for. Nothing else is added to the sheet."""
    wb = load_workbook(TEMPLATE)
    ws = wb.active
    body = Font(name=FONT, size=11)
    r = 3
    for rec in frame.itertuples(index=False):
        ws.cell(row=r, column=1, value=rec[0]).font = body
        for col in range(2, 8):
            cell = ws.cell(row=r, column=col, value=rec[col - 1])
            cell.font = body
            if col % 2 == 1:        # Volume: whole rupees, Indian grouping; shrinks to fit the narrow column, never ####
                cell.number_format = "#,##,##0"
                cell.alignment = Alignment(horizontal="right", shrink_to_fit=True)
        r += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def html_table(frame: pd.DataFrame) -> str:
    """The same summary drawn on the page as a static HTML table (no data grid): Store code spans both header rows
    (rowspan 2), each group spans its two columns (colspan 2), and a Total row closes it - so the screen shows the layout
    of the downloaded sheet. Volumes are whole rupees with Indian grouping."""
    from .formatting import count as fmt_count
    from .formatting import indian_group

    def rupees(v) -> str:
        whole = int(abs(float(v)) + 0.5) * (1 if float(v) >= 0 else -1)      # half-up, not banker's rounding
        return indian_group(whole).removesuffix(".00") if v else "0"

    head1 = "<tr><th rowspan='2' class='mop-sum-left'>Store code</th>" + "".join(
        f"<th colspan='2'>{label}</th>" for _t, label in GROUPS) + "</tr>"
    head2 = "<tr>" + "<th>open ticket</th><th>Volume</th>" * len(GROUPS) + "</tr>"
    body = []
    for rec in frame.itertuples(index=False):
        cells = "".join(f"<td>{fmt_count(rec[i]) if i % 2 == 1 else rupees(rec[i])}</td>" for i in range(1, 7))
        body.append(f"<tr><td class='mop-sum-left'>{rec[0]}</td>{cells}</tr>")
    if len(frame):
        tot = [frame[c].sum() for c in COLUMNS[1:]]
        cells = "".join(f"<td>{fmt_count(int(v)) if i % 2 == 0 else rupees(v)}</td>" for i, v in enumerate(tot))
        body.append(f"<tr class='mop-static-total'><td class='mop-sum-left'>Total</td>{cells}</tr>")
    return ("<div class='mop-static-wrap mop-sum-scroll'><table class='mop-static mop-sum'><thead>"
            f"{head1}{head2}</thead><tbody>{''.join(body)}</tbody></table></div>")
