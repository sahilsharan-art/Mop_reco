"""Store-wise task summary: one row per store, (open tickets, volume in Rs) for open bills / high float / CMS not done,
filtered exactly as the Store Task page filters, laid out in the sheet shape the operator specified."""
import io
import sys
from datetime import date
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import store_task_summary as sts  # noqa: E402
from mop_reco import store_tasks as stt  # noqa: E402
from mop_reco.db import StoreTask, get_engine, get_session, init_db  # noqa: E402

TODAY = date(2026, 10, 21)


@pytest.fixture
def session(tmp_path):
    engine = get_engine(tmp_path / "t.sqlite")
    init_db(engine)
    s = get_session(engine)
    n = [0]

    def add(task_type, store, amount, status="open", day="2026-09-10"):
        n[0] += 1
        s.add(StoreTask(task_key=f"{task_type}:{n[0]}", generation=1, task_type=task_type, store=store,
                        business_date=day, bill_no=f"B{n[0]}", mop="PAYTM EDC", amount=amount, detail="{}",
                        raised_on="2026-10-14", due_by="2026-10-17", status=status, first_seen_run_id=1,
                        last_seen_run_id=1))
    add(stt.OPEN_BILL, "ABD", 500.0); add(stt.OPEN_BILL, "ABD", 250.5); add(stt.HIGH_FLOAT, "ABD", 20000.0)
    add(stt.CMS_NOT_DONE, "ABD", 75000.0); add(stt.OPEN_BILL, "XYZ", 100.0)
    add(stt.OPEN_BILL, "XYZ", 900.0, status="closed")
    s.commit()
    return s


def test_counts_tasks_and_sums_rupees_per_store_and_type(session):
    f = sts.summary_frame(session, TODAY, statuses=stt.LIVE_STATUSES)
    assert list(f["Store code"]) == ["ABD", "XYZ"]
    abd = f.set_index("Store code").loc["ABD"]
    assert (abd["Edc other tranx open ticket"], abd["Edc other tranx Volume"]) == (2, 750.5)
    assert (abd["High float open ticket"], abd["High float Volume"]) == (1, 20000.0)
    assert (abd["Cms not deposit open ticket"], abd["Cms not deposit Volume"]) == (1, 75000.0)
    xyz = f.set_index("Store code").loc["XYZ"]
    assert (xyz["Edc other tranx open ticket"], xyz["High float open ticket"]) == (1, 0)    # the closed one is not "open"


def test_the_page_filters_apply(session):
    only_float = sts.summary_frame(session, TODAY, statuses=stt.LIVE_STATUSES, types=[stt.HIGH_FLOAT])
    assert list(only_float["Store code"]) == ["ABD"] and only_float["Edc other tranx open ticket"].sum() == 0
    one_store = sts.summary_frame(session, TODAY, statuses=stt.LIVE_STATUSES, store="XYZ")
    assert list(one_store["Store code"]) == ["XYZ"]
    closed = sts.summary_frame(session, TODAY, statuses=[stt.CLOSED])
    assert closed["Edc other tranx Volume"].sum() == 900.0
    assert sts.summary_frame(session, TODAY, statuses=stt.LIVE_STATUSES, date_from=date(2026, 10, 1)).empty


def test_the_sheet_is_the_operators_own_template_with_the_stores_filled_in(session):
    f = sts.summary_frame(session, TODAY, statuses=stt.LIVE_STATUSES)
    ws = openpyxl.load_workbook(io.BytesIO(sts.workbook_bytes(f))).active
    ref = openpyxl.load_workbook(sts.TEMPLATE).active
    assert sorted(str(m) for m in ws.merged_cells.ranges) == ["A1:A2", "B1:C1", "D1:E1", "F1:G1"]
    assert [ws.cell(row=1, column=c).value for c in (1, 2, 4, 6)] == ["Store code", "Edc other tranx", "High float",
                                                                      "Cms not deposit"]
    assert [ws.cell(row=2, column=c).value for c in range(2, 8)] == ["open ticket", "Volume"] * 3
    assert {k: round(ws.column_dimensions[k].width, 2) for k in "ABCDEFG"} == sts.WIDTHS
    for coord in ("A1", "B1", "D1", "F1", "B2", "C2", "G2"):          # header cells identical to the supplied file
        a_, b_ = ws[coord], ref[coord]
        assert (a_.font.name, a_.font.size, a_.font.bold, a_.alignment.horizontal, a_.border.bottom.style) ==                (b_.font.name, b_.font.size, b_.font.bold, b_.alignment.horizontal, b_.border.bottom.style)
        assert a_.fill.fill_type is None
    assert ws["A3"].value == "ABD" and ws["B3"].value == 2 and ws["C3"].value == 750.5 and ws["A4"].value == "XYZ"
    assert ws.max_row == 4 and len(openpyxl.load_workbook(io.BytesIO(sts.workbook_bytes(f))).sheetnames) == 1


def test_the_page_table_has_the_same_two_row_header_with_rowspan_and_colspan(session):
    f = sts.summary_frame(session, TODAY, statuses=stt.LIVE_STATUSES)
    html = sts.html_table(f)
    assert "<th rowspan='2' class='mop-sum-left'>Store code</th>" in html
    for label in ("Edc other tranx", "High float", "Cms not deposit"):
        assert f"<th colspan='2'>{label}</th>" in html
    assert html.count("<th>open ticket</th><th>Volume</th>") == 3 and "stDataFrame" not in html
    assert "<td class='mop-sum-left'>ABD</td><td>2</td><td>751</td><td>1</td><td>20,000</td><td>1</td><td>75,000</td>" in html
    assert "<tr class='mop-static-total'><td class='mop-sum-left'>Total</td><td>3</td><td>851</td>" in html
