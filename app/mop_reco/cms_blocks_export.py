"""The CMS Reco workbook - the operator's own wide layout (2026-10-16).

Sheet "CMS Reco":
  row 1  DEP.DATE over each block      row 2  Cr. Date (the credit date each CMS column collects)
  row 3  labels                        rows 4..  one row per store (UNRESOLVED / BNK last), totals at the foot
  per deposit-date block: Opening | Cash/CMS Deposit | CMS Report (1) | CMS Report (2) | Closing, then a spacer
  to the right, per month: Opening | Cash/CMS Deposit | CMS Report | Diff | Remarks

Live formulas where the workbook has them: Closing = Opening + Deposit - CMS(1) - CMS(2); the next block's
Opening = this Closing; the monthly Opening/Deposit/CMS/Diff; every total. Only the first block's Opening, the
Deposit and the CMS amounts are values. Pure of Streamlit.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pandas as pd
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name, xl_rowcol_to_cell

from . import cms_blocks as cb

NUM = "#,##,##0.00"
HEAD_ROWS = 3


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


def build_workbook(m: cb.Model, date_from: str, date_to: str, remarks: dict, later: pd.DataFrame,
                   unresolved: pd.DataFrame, excluded: pd.DataFrame, controls: list, diffs: pd.DataFrame,
                   meta: dict, overrides: pd.DataFrame = None) -> bytes:
    a, b = m.window(date_from, date_to)
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    f_num = wb.add_format({"num_format": NUM})
    f_head = wb.add_format({"bold": True, "align": "center", "bg_color": "#EEF2F6", "border": 1})
    f_date = wb.add_format({"bold": True, "align": "center", "num_format": "dd/mm/yyyy", "bg_color": "#EEF2F6", "border": 1})
    f_tot = wb.add_format({"bold": True, "num_format": NUM, "top": 1})
    f_totl = wb.add_format({"bold": True, "top": 1})
    f_bold = wb.add_format({"bold": True})
    f_text = wb.add_format({"num_format": "@"})
    f_over = wb.add_format({"num_format": NUM, "bg_color": "#FDE9C8", "border": 1, "border_color": "#E8962E"})
    over_note = {}
    if overrides is not None and len(overrides):
        for o in overrides[overrides["status"] == "active"].itertuples(index=False):
            over_note[(o.store, o.dep_date, o.col)] = (f"Bank reported {o.computed:,.2f}, overridden to "
                                                       f"{o.override:,.2f} by {o.entered_by}: {o.reason}")

    ws = wb.add_worksheet("CMS Reco")
    ws.write(2, 0, "STORE NAME", f_head)
    ws.freeze_panes(HEAD_ROWS, 1)
    ws.set_column(0, 0, 14)
    n = len(m.stores)
    r0, r_tot = HEAD_ROWS, HEAD_ROWS + n
    for i, s in enumerate(m.stores):
        ws.write(r0 + i, 0, s)
    ws.write(r_tot, 0, "TOTAL", f_totl)

    # block columns
    col_of = {}                       # block index -> first column
    c = 1
    for k in range(a, b):
        col_of[k] = c
        c += 6                        # five columns and a spacer
    month_start_col = c + 1

    for k in range(a, b):
        c0 = col_of[k]
        dep = _d(m.dates[k])
        ws.merge_range(0, c0, 0, c0 + 4, datetime.combine(dep, datetime.min.time()), f_date)
        ws.write(1, c0 + 2, datetime.combine(dep, datetime.min.time()), f_date)
        ws.write(1, c0 + 3, datetime.combine(dep + timedelta(days=1), datetime.min.time()), f_date)
        for j, lab in enumerate(["Opening", "Cash/CMS Deposit", "CMS Report (1)", "CMS Report (2)", "Closing"]):
            ws.write(2, c0 + j, lab, f_head)
        ws.set_column(c0, c0 + 4, 14)
        ws.set_column(c0 + 5, c0 + 5, 2)
        for i in range(n):
            r = r0 + i
            if k == a:
                ws.write_number(r, c0, float(m.opening[k][i]), f_num)
            else:
                ws.write_formula(r, c0, "=" + xl_rowcol_to_cell(r, col_of[k - 1] + 4), f_num,
                                 float(m.opening[k][i]))
            ws.write_number(r, c0 + 1, float(m.deposit[k][i]), f_num)
            for off, name, arr in ((2, 'same_day', m.cms1), (3, 'next_day', m.cms2)):
                note = over_note.get((m.stores[i], m.dates[k], name))
                if note:
                    ws.write_number(r, c0 + off, float(arr[k][i]), f_over)
                    ws.write_comment(r, c0 + off, note, {'x_scale': 3})
                else:
                    ws.write_number(r, c0 + off, float(arr[k][i]), f_num)
            o, d_, c1, c2 = (xl_rowcol_to_cell(r, c0 + j) for j in range(4))
            ws.write_formula(r, c0 + 4, f"={o}+{d_}-{c1}-{c2}", f_num, float(m.closing[k][i]))
        for j in range(5):
            col = xl_col_to_name(c0 + j)
            if j == 0 and k != a:
                ws.write_formula(r_tot, c0, "=" + xl_rowcol_to_cell(r_tot, col_of[k - 1] + 4), f_tot)
            else:
                ws.write_formula(r_tot, c0 + j, f"=SUM({col}{r0 + 1}:{col}{r0 + n})", f_tot)

    # monthly summaries to the right
    months = []
    for k in range(a, b):
        key = m.dates[k][:7]
        if not months or months[-1][0] != key:
            months.append([key, k, k])
        else:
            months[-1][2] = k
    cc = month_start_col
    for key, k0, k1 in months:
        lo, hi = m.dates[k0], m.dates[k1]
        ws.merge_range(0, cc, 0, cc + 4, f"{_d(lo).strftime('%B %Y')} ({_d(lo).strftime('%d/%m')} - {_d(hi).strftime('%d/%m')})", f_head)
        for j, lab in enumerate(["Opening", "Cash/CMS Deposit", "CMS Report", "Diff", "Remarks"]):
            ws.write(2, cc + j, lab, f_head)
        ws.set_column(cc, cc + 3, 15)
        ws.set_column(cc + 4, cc + 4, 40)
        rm = remarks.get((lo, hi), {})
        for i, s in enumerate(m.stores):
            r = r0 + i
            deps = ",".join(xl_rowcol_to_cell(r, col_of[k] + 1) for k in range(k0, k1 + 1))
            cmss = ",".join(f"{xl_rowcol_to_cell(r, col_of[k] + 2)},{xl_rowcol_to_cell(r, col_of[k] + 3)}"
                            for k in range(k0, k1 + 1))
            dep_v = float(m.deposit[k0:k1 + 1, i].sum())
            cms_v = float((m.cms1[k0:k1 + 1, i] + m.cms2[k0:k1 + 1, i]).sum())
            open_v = float(m.opening[k0][i])
            ws.write_formula(r, cc, "=" + xl_rowcol_to_cell(r, col_of[k0]), f_num, open_v)
            ws.write_formula(r, cc + 1, f"=SUM({deps})", f_num, dep_v)
            ws.write_formula(r, cc + 2, f"=SUM({cmss})", f_num, cms_v)
            ws.write_formula(r, cc + 3, "={}+{}-{}".format(*(xl_rowcol_to_cell(r, cc + j) for j in range(3))),
                             f_num, open_v + dep_v - cms_v)
            if s in rm:
                ws.write(r, cc + 4, rm[s])
        for j in range(4):
            col = xl_col_to_name(cc + j)
            ws.write_formula(r_tot, cc + j, f"=SUM({col}{r0 + 1}:{col}{r0 + n})", f_tot)
        cc += 6

    # supporting sheets
    ro = wb.add_worksheet("Read me")
    lines = [("CMS Reco", ""), ("Run", meta.get("run_id")), ("Run timestamp", str(meta.get("run_ts"))),
             ("Period", f"{_d(date_from).strftime('%d/%m/%Y')} to {_d(date_to).strftime('%d/%m/%Y')}"),
             ("Exported", datetime.now().strftime("%d/%m/%Y %H:%M")),
             ("Opening", meta.get("opening_note", "")),
             ("CMS Report (1) / (2)", "COL slips of the deposit date credited on the same day / the next day (CRDATE)."),
             ("Credited later", "A slip credited day+2 or later is in neither column; it stays in Closing and is listed on 'Credited later'."),
             ("", ""), ("Controls", "")]
    for i, (k, v) in enumerate(lines):
        ro.write(i, 0, k, f_bold)
        ro.write(i, 1, v)
    base = len(lines)
    for i, ctl in enumerate(controls):
        ro.write(base + i, 0, "OK" if ctl.ok else "MISMATCH", f_bold)
        ro.write(base + i, 1, ctl.label + (f" - {ctl.detail}" if ctl.detail else ""))
        ro.write_number(base + i, 2, ctl.amount, f_num)
    ro.set_column(0, 0, 24)
    ro.set_column(1, 1, 110)
    ro.set_column(2, 2, 18)

    def sheet(name, df, id_cols=(), widths=None):
        w = wb.add_worksheet(name)
        for j, col in enumerate(df.columns):
            w.write(0, j, col, f_head)
            w.set_column(j, j, (widths or {}).get(col, 16))
        for i, row in enumerate(df.itertuples(index=False), start=1):
            for j, v in enumerate(row):
                if pd.isna(v) if not isinstance(v, str) else False:
                    continue
                if df.columns[j] in id_cols:
                    w.write_string(i, j, str(v), f_text)
                elif isinstance(v, (int, float)):
                    w.write_number(i, j, float(v), f_num if isinstance(v, float) else None)
                else:
                    w.write(i, j, str(v))
        w.freeze_panes(1, 0)

    sheet("Credited later", later, id_cols=("pickup_code", "slip_no"))
    sheet("Unresolved stores", unresolved, id_cols=("pickup_code",))
    sheet("Excluded types", excluded, widths={"Note": 70})
    sheet("Deposit vs CMS by store", diffs)
    if overrides is not None:
        ov = overrides.copy()
        if len(ov):
            ov["entered_at"] = ov["entered_at"].astype(str)
            ov["reverted_at"] = ov["reverted_at"].astype(str).replace({"NaT": ""})
        sheet("CMS overrides", ov.rename(columns={"dep_date": "deposit_date", "col": "column", "computed": "bank (computed)",
                                                   "override": "override"}), id_cols=(), widths={"reason": 60})
    wb.close()
    return buf.getvalue()
