"""The Exceptions screen and export share ONE column definition (2026-10-15).

Regression: the screen gained candidate-transaction columns, the export did not, and the export
selected the screen's column list against a frame without them - a raw KeyError at the write step.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import exception_pairing as ep
from mop_reco import exception_tables as et
from mop_reco import queries as q
from mop_reco.db import Base, Exception_, R1BillLine, Run, get_session
from mop_reco.export import ExportMeta, build_export


@pytest.fixture
def session():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    s = get_session(e)
    s.add(Run(id=1, started_at=datetime(2026, 9, 1), status="completed", business_dates="[]"))
    s.add(R1BillLine(run_id=1, business_date="2026-08-03", store="ABD", bill_no="B1", line_seq=1, mop="PAYTM EDC",
                     mop_class="digital", amount=500.0, matched=False, status="x", bill_time="12:00:00",
                     created_by="CASH2"))
    s.add(Exception_(exception_key="2026-08-03|ABD|B1|PAYTM EDC", reconciliation="R1", type="bill_not_matched",
                     business_date="2026-08-03", store="ABD", amount=500.0, status="open",
                     detail=json.dumps({"bill_no": "B1", "mop": "PAYTM EDC"}), first_seen_run_id=1, last_seen_run_id=1))
    tid = "20260803111030000285964210320294741"
    for t, hh in ((tid, "12:05:00"), ("T2", "12:30:00"), ("T3", "12:40:00")):
        s.add(Exception_(exception_key=f"paytm|{t}", reconciliation="R1", type="unmapped_gateway_transaction",
                         business_date="2026-08-03", store="ABD", amount=500.0, status="open", transaction_time=hh,
                         detail=json.dumps({"gateway": "paytm", "transaction_id": t}), first_seen_run_id=1,
                         last_seen_run_id=1))
    s.commit()
    return s


def shaped(session):
    assignment = ep.assign(ep.open_population(session, 1), ep.unmapped_pool(session), 10, 2)
    return q.exception_export(session, None, None, None, q.NOT_RECEIVED_TYPES, ("open",),
                              transform=lambda f: et.add_pairing(et.bill_frame(session, 1, f), assignment)[0])


def test_export_has_the_candidate_columns_and_no_ui_columns(session):
    out = et.export_view(shaped(session), et.bill_columns(pairing=True, screen=False), "bills")
    assert list(out.columns) == ["date", "time", "store", "bill_no", "mop", "amount", "created_by", "status",
                                 "Txn 1", "Amount 1", "Txn 2", "Amount 2", "remark", "remark_by", "remark_at"]
    assert "More" not in out.columns and "Match" not in out.columns
    row = out.iloc[0]
    assert row["Txn 1"] == "20260803111030000285964210320294741" and row["Amount 1"] == 500.0
    assert row["Txn 2"] == ""          # T2 has the bill's own amount - a duplicate, never "Txn 2"
    assert row["date"] == "03/08/2026" and row["time"] == "12:00:00" and row["created_by"] == "CASH2"


def test_the_screen_and_the_export_columns_differ_only_by_the_two_ui_affordances():
    screen = et.bill_columns(pairing=True, screen=True)
    export = et.bill_columns(pairing=True, screen=False)
    assert [c for c in screen if c not in export] == ["More", "Match"]


def test_ui_only_columns_are_refused_by_name(session):
    with pytest.raises(et.ExportSchemaError, match="More"):
        et.export_view(shaped(session), ["date", "More"], "bills")


def test_a_missing_declared_column_is_a_named_error_not_a_keyerror(session):
    plain = et.bill_frame(session, 1, q.exception_export(session, None, None, None, q.NOT_RECEIVED_TYPES, ("open",)))
    with pytest.raises(et.ExportSchemaError, match="Txn 1"):
        et.export_view(plain, et.bill_columns(pairing=True, screen=False), "bills")


def test_the_file_keeps_a_35_digit_id_as_text(session, tmp_path):
    out = et.export_view(shaped(session), et.bill_columns(pairing=True, screen=False), "bills")
    res = build_export(out, ExportMeta(run_id=1, run_timestamp=datetime(2026, 9, 1), value_column="amount",
                                       title="Exception bills"))
    import io
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(res.data)).active
    cells = [c for row in ws.iter_rows() for c in row if c.value == "20260803111030000285964210320294741"]
    assert cells and cells[0].data_type == "s"


def test_transactions_export_uses_its_own_declared_schema(session):
    types = q.RECEIVED_NOT_BILLED_TYPES
    frame = q.exception_export(session, None, None, None, types, ("open",), transform=lambda f: et.txn_frame(session, f))
    out = et.export_view(frame, et.TXN_COLUMNS, "txns")
    assert len(out) == 3 and out["transaction_id"].iloc[0].startswith("2026080311")


def test_closed_bills_have_no_candidate_columns():
    assert et.bill_columns(pairing=False, screen=False) == et.BILL_BASE_COLUMNS
