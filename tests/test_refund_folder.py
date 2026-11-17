"""The Customer Refund folder (2026-10-30): the finance workbook is read by column NAME, judged by the ordinary refund rules,
and only passing lines are recorded - released with their process date, never one that is unpaid or whose amount differs."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import openpyxl
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import refunds as rf
from mop_reco.db import TransactionRefund
from test_refunds import CLEAN_POOL, seed_store_master, session  # noqa: F401  (fixtures)


def _workbook(folder: Path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Zoho ID", "Transaction No", "Name", "Store Code", "Tran Date", "Process Date", "Amt", "IFSC", "Account No"])
    for r in rows:
        ws.append(r)
    wb.save(folder / "Refunds.xlsx")
    (folder / "~$Refunds.xlsx").write_bytes(b"lock")


def test_reader_finds_columns_by_name_and_skips_bank_details_and_lock_files(tmp_path):
    _workbook(tmp_path, [["Z1", "TXN-1", "Ramesh", "abd", date(2026, 7, 20), date(2026, 7, 25), 500, "IFSC0", "123"],
                         ["Z2", "TXN-2", "Sita", "ABD", date(2026, 7, 20), None, 500, "IFSC0", "456"]])
    frame, files = rf.read_refund_folder(tmp_path)
    assert [f["file"] for f in files] == ["Refunds.xlsx"]
    assert list(frame["status"]) == ["released", "pending"]
    assert list(frame["store_code"]) == ["ABD", "ABD"]
    assert frame.loc[0, "process_date"] == "2026-07-25"
    assert not {"ifsc", "account_no"} & set(frame.columns)


def test_unpaid_and_amount_difference_are_held_and_a_clean_line_is_recorded(session, monkeypatch, tmp_path):  # noqa: F811
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)
    frame = pd.DataFrame([
        {"date": "2026-07-20", "store_code": "ABD", "transaction_no": "TXN-1", "zoho_no": "Z1", "customer_name": "Ramesh",
         "status": "released", "process_date": "2026-07-25", "amount_file": 500.0, "remark": "", "file": "f.xlsx", "file_row": 2},
    ], columns=rf.UPLOAD_COLUMNS + ["process_date", "amount_file", "remark", "file", "file_row"])

    ok = rf.preview_folder(session, frame)
    assert list(ok["verdict"]) == ["apply"]

    unpaid = frame.assign(process_date="", status="pending")
    assert rf.preview_folder(session, unpaid).loc[0, "verdict"] == "hold"
    differs = frame.assign(amount_file=499.0)
    j = rf.preview_folder(session, differs)
    assert j.loc[0, "verdict"] == "hold" and "differs" in j.loc[0, "why"]

    assert rf.apply_folder(session, ok, "tester") == 1
    row = session.query(TransactionRefund).one()
    assert (row.zoho_no, row.refund_status, row.uploaded_by) == ("Z1", "released", "tester")


def test_apply_folder_needs_an_operator_and_records_nothing_without_one(session, monkeypatch):  # noqa: F811
    seed_store_master(session, ["ABD"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: CLEAN_POOL)
    judged = pd.DataFrame([{"verdict": "apply"}])
    try:
        rf.apply_folder(session, judged, "")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert session.query(TransactionRefund).count() == 0


def test_refund_table_filters_by_date_and_store_and_carries_the_id(session):  # noqa: F811
    from datetime import datetime as dt
    for i, (d, s) in enumerate([("2026-07-01", "ABD"), ("2026-07-20", "ABD"), ("2026-07-20", "XYZ")]):
        session.add(TransactionRefund(gateway="paytm", transaction_ref=f"T{i}", business_date=d, store=s, amount=10.0 + i,
                                      zoho_no=f"Z{i}", customer_name="C", refund_date="2026-07-25", refund_status="released",
                                      uploaded_by="t", uploaded_at=dt(2026, 7, 26)))
    session.commit()
    assert len(rf.refund_table(session)) == 3
    one = rf.refund_table(session, "2026-07-10", "2026-07-31", "ABD")
    assert list(one["transaction_id"]) == ["T1"] and "id" in one.columns


# ---- 2026-10-30: shared Zoho tickets, one transaction named twice, store differences -------------------------------------
from mop_reco.db import Exception_  # noqa: E402


def _folder_frame(rows):
    cols = rf.UPLOAD_COLUMNS + ["process_date", "amount_file", "remark", "file", "file_row"]
    return pd.DataFrame([{"date": "2026-07-20", "store_code": s, "transaction_no": t, "zoho_no": z, "customer_name": c,
                          "status": "released", "process_date": pdate, "amount_file": a, "remark": "", "file": "f.xlsx",
                          "file_row": i + 2} for i, (s, t, z, c, pdate, a) in enumerate(rows)], columns=cols)


def _seed_exceptions(session, refs, store="ABD"):
    from mop_reco.db import Run
    run = Run(started_at=datetime(2026, 7, 20), finished_at=datetime(2026, 7, 20), business_dates="[]", status="completed",
              triggered_by="t")
    session.add(run)
    session.flush()
    for r in refs:
        session.add(Exception_(exception_key=f"paytm|{r}", reconciliation="R1", type="unmapped_gateway_transaction",
                               business_date="2026-07-20", store=store, amount=500.0, status="open",
                               first_seen_run_id=run.id, last_seen_run_id=run.id))
    session.commit()


POOL2 = pd.DataFrame([("paytm", t, "2026-07-20", "ABD", 500.0, "SUCCESS", "ACQUIRING", "UPI") for t in ("T1", "T2", "T3")],
                     columns=["gateway", "txn_ref", "business_date", "store", "amount", "status", "txn_type", "payment_mode"])


def test_one_zoho_ticket_covers_several_payments_of_the_same_customer_only(session, monkeypatch):  # noqa: F811
    seed_store_master(session, ["ABD"])
    _seed_exceptions(session, ["T1", "T2", "T3"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: POOL2)
    same = _folder_frame([("ABD", "T1", "Z1", "Ramesh", "2026-07-25", 500.0), ("ABD", "T2", "Z1", "RAMESH ", "2026-07-25", 500.0)])
    j = rf.preview_folder(session, same)
    assert list(j["verdict"]) == ["apply", "apply"]
    assert rf.apply_folder(session, j, "tester") == 2
    assert session.query(TransactionRefund).filter_by(zoho_no="Z1").count() == 2
    other = _folder_frame([("ABD", "T3", "Z1", "Someone Else", "2026-07-25", 500.0)])
    assert rf.preview_folder(session, other).loc[0, "verdict"] == "hold"      # a different customer cannot reuse the ticket


def test_a_transaction_named_by_two_tickets_is_refunded_once_and_the_second_is_noted(session, monkeypatch):  # noqa: F811
    from mop_reco.db import Remark
    seed_store_master(session, ["ABD"])
    _seed_exceptions(session, ["T1"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: POOL2)
    frame = _folder_frame([("ABD", "T1", "Z2", "Ramesh", "2026-07-30", 500.0), ("ABD", "T1", "Z1", "Ramesh", "2026-07-25", 500.0)])
    j = rf.preview_folder(session, frame)
    assert dict(zip(j["zoho_no"], j["verdict"])) == {"Z2": "hold", "Z1": "apply"}      # the earlier process date wins
    assert rf.apply_folder(session, j, "tester") == 1
    assert session.query(TransactionRefund).one().zoho_no == "Z1"
    remark = session.query(Remark).one().remark_text
    assert "Z2" in remark and "possible duplicate payout" in remark


def test_store_differences_are_recorded_with_both_stores_named(session, monkeypatch):  # noqa: F811
    from mop_reco.db import Remark
    seed_store_master(session, ["ABD", "XYZ"])
    _seed_exceptions(session, ["T1", "T2"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: POOL2)
    frame = _folder_frame([("XYZ", "T1", "Z1", "A", "2026-07-25", 500.0), ("NEW", "T2", "Z2", "B", "2026-07-25", 500.0)])
    j = rf.preview_folder(session, frame)
    assert list(j["verdict"]) == ["apply", "hold"]          # XYZ differs from ABD: recorded + noted; NEW is not the txn's store
    assert rf.apply_folder(session, j, "tester") == 1
    assert session.query(TransactionRefund).one().store == "ABD"
    assert "store in the refund file is XYZ, the transaction belongs to ABD" in session.query(Remark).one().remark_text


def test_a_line_without_a_transaction_number_is_never_guessed(session, monkeypatch):  # noqa: F811
    seed_store_master(session, ["ABD"])
    _seed_exceptions(session, ["T1"])
    monkeypatch.setattr(rf, "targeted_pool_many", lambda *a, **kw: POOL2)
    j = rf.preview_folder(session, _folder_frame([("ABD", "Not Found", "Z1", "A", "2026-07-25", 500.0)]))
    assert j.loc[0, "verdict"] == "hold" and "T1" in j.loc[0, "why"]       # the candidate is offered, not applied
    assert rf.apply_folder(session, j, "tester") == 0
