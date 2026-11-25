"""The ERP readers (mop_reco/erp_source.py): same normalised frames as the Excel readers, strict about the schema, careful about the queries.

No real ERP is touched: the connection is a fake that answers the catalogue lookup and the two views.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from mop_reco import erp, erp_source, ingest
from mop_reco.config import load_config

CONFIG = load_config()

POS_FULL = ["bill_no", "site_ref", "store_code", "store_name", "business_date", "bill_time", "mop", "mop_type", "mop_short_code",
            "amount", "tender", "balance", "edc_response", "tp_bill_ref", "created_by", "terminal", "bill_ts", "loaded_at"]
POS_INTERIM = ["bill_no", "site_ref", "store_code", "store_name", "business_date", "bill_time", "mop", "mop_type", "mop_short_code",
               "amount", "bill_loaded_at"]
PTC_COLS = ["store", "store_code", "business_date", "voucher_no", "mode", "head", "ledger", "sub_ledger", "amount", "posted_amount",
            "remarks"]

PAYTM = json.dumps({"head": {}, "body": {"acquirementId": "20260720111030000285964210320294741"}})
AXIS = json.dumps({"head": {}, "body": {"retrievalReferenceNo": "973933049280", "issuerMaskCardNo": "XXXX1234"}})
BAJAJ = json.dumps({"rrn": "BJ1234567", "tid": "T9"})

D = datetime(2026, 7, 20)
# (bill_no, site_ref, store, mop, amount, edc_response, created_by, bill_time)
BILLS = [
    ("SB58/024800/0726", "SSRKPHB-145937", "PHB", "PAYTM EDC", 2254.0, PAYTM, "T1879", time(15, 30, 20)),
    ("SB58/024801/0726", "SSRKPHB-145938", "PHB", "EDC OTHER AXIS", 500.0, AXIS, "T1879", time(15, 31, 2)),
    ("SB58/024802/0726", "SSRKPHB-145939", "PHB", "Bajajpay UPI", 700.0, BAJAJ, "T1880", time(16, 0, 0)),
    ("SB58/024803/0726", "SSRKPHB-145940", "PHB", "Cash", 1000.0, None, "T1880", time(16, 5, 9)),
    ("SB58/024803/0726", "SSRKPHB-145940", "PHB", "PaytmDQR", 250.0, "not json {", "T1880", time(16, 5, 9)),
    ("SA01/000001/0726", "SSRKABD-000001", "ABD", "Credit Note Received", 398.0, None, "C1", time(10, 0, 1)),
]


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)

    def fetchmany(self, n):
        return list(self.rows[:n])

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeErp:
    """Answers the catalogue lookup and the two views; records every statement and its parameters."""

    def __init__(self, pos_columns, pos_rows=(), ptc_columns=None, ptc_rows=()):
        self.pos_columns, self.pos_rows = list(pos_columns), list(pos_rows)
        self.ptc_columns, self.ptc_rows = ptc_columns, list(ptc_rows)
        self.executed = []
        self.read_only = True

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split()).lower()
        if "pg_attribute" in s:
            rel = params[0]
            cols = self.pos_columns if rel == erp_source.POS_VIEW else (self.ptc_columns or [])
            return _Cur([(c,) for c in cols])
        if f"from {erp_source.POS_VIEW}" in s:
            return _Cur(self._select(self.pos_rows, self.pos_columns, sql, params))
        if f"from {erp_source.PTC_VIEW}" in s:
            return _Cur(self._select(self.ptc_rows, self.ptc_columns, sql, params))
        raise AssertionError("unexpected statement: " + sql)

    @staticmethod
    def _select(rows, columns, sql, params):
        lo, hi = params
        wanted = [c.strip() for c in sql.lower().split(" from ")[0].replace("select", "").split(",")]
        out = []
        for r in rows:
            rec = dict(zip(columns, r))
            if lo <= rec["business_date"] < hi:
                out.append(tuple(rec[c] for c in wanted))
        return out

    def rollback(self):
        pass


def _pos_row(bill, columns):
    bill_no, site_ref, store, mop, amount, edc, by, t = bill
    rec = {"bill_no": bill_no, "site_ref": site_ref, "store_code": 1, "store_name": store, "business_date": D, "bill_time": t,
           "mop": mop, "mop_type": "x", "mop_short_code": "y", "amount": amount, "tender": amount, "balance": 0, "edc_response": edc,
           "tp_bill_ref": None, "created_by": by, "terminal": "T", "bill_ts": D, "loaded_at": D, "bill_loaded_at": D}
    return tuple(rec[c] for c in columns)


@pytest.fixture
def fake():
    return FakeErp(POS_FULL, [_pos_row(b, POS_FULL) for b in BILLS])


# --- schema -------------------------------------------------------------------------------------------------------

def test_the_interim_view_without_the_terminal_reply_is_refused_with_a_clear_message():
    conn = FakeErp(POS_INTERIM)
    with pytest.raises(erp_source.ErpSchemaError) as exc:
        list(erp_source.iter_pos_raw(conn, date(2026, 7, 20), date(2026, 7, 20)))
    msg = str(exc.value)
    assert "edc_response" in msg and "night load" in msg
    assert not any("business_date >=" in s for s, _ in conn.executed)       # no data was read


def test_a_missing_or_unreadable_view_is_named():
    with pytest.raises(erp_source.ErpSchemaError, match="does not exist"):
        erp_source.check_pos_schema(FakeErp([]))
    with pytest.raises(erp_source.ErpSchemaError, match="ptc_vouchers"):
        erp_source.check_ptc_schema(FakeErp(POS_FULL, ptc_columns=None))
    with pytest.raises(erp_source.ErpSchemaError, match="remarks"):
        erp_source.check_ptc_schema(FakeErp(POS_FULL, ptc_columns=[c for c in PTC_COLS if c != "remarks"]))


# --- the query contract (the DBA's rules) -------------------------------------------------------------------------

def test_queries_use_a_half_open_range_with_no_function_on_the_date_column(fake):
    list(erp_source.iter_pos_raw(fake, date(2026, 7, 20), date(2026, 7, 21), chunk_days=1))
    data_queries = [(s, p) for s, p in fake.executed if "from gold_mop.pos_bill_lines" in s]
    assert len(data_queries) == 2                                              # one query per day
    for sql, params in data_queries:
        flat = " ".join(sql.split()).lower()
        assert "where business_date >= %s and business_date < %s" in flat
        assert "date(business_date" not in flat and "::date" not in flat and "(business_date)" not in flat
        assert "order by bill_no, mop, amount, bill_time" in flat
        assert params[1] - params[0] == pd.Timedelta(days=1).to_pytimedelta()
        assert params[0].time() == time(0, 0)
    assert data_queries[0][1][0] == datetime(2026, 7, 20) and data_queries[1][1][0] == datetime(2026, 7, 21)


def test_chunking_covers_every_date_once():
    ch = list(erp_source._chunks(date(2026, 7, 1), date(2026, 7, 10), 4))
    assert ch == [(date(2026, 7, 1), date(2026, 7, 4)), (date(2026, 7, 5), date(2026, 7, 8)), (date(2026, 7, 9), date(2026, 7, 10))]
    assert list(erp_source._chunks(date(2026, 7, 5), date(2026, 7, 5), 1)) == [(date(2026, 7, 5), date(2026, 7, 5))]
    with pytest.raises(ValueError):
        list(erp_source._chunks(date(2026, 7, 5), date(2026, 7, 4), 1))


def test_every_statement_the_reader_sends_passes_the_read_only_guard(fake):
    list(erp_source.iter_pos_raw(fake, date(2026, 7, 20), date(2026, 7, 20)))
    for sql, _ in fake.executed:
        erp.check_select_only(sql)


# --- the same normalised frame as the Excel path ------------------------------------------------------------------

def _excel_shaped():
    """The same bills as the Excel export would hand the shared normaliser."""
    rows = [{"BILLNO": b[0], "PSITE_POSBILL_CODE": b[1], "STORE_NAME": b[2], "BILLDATE": D, "MOPDESC": b[3], "BASEAMT": b[4],
             "TPEDCRESPONSE": b[5], "CREATEBY": b[6], "BILL_TIME": b[7]} for b in BILLS]
    return pd.DataFrame(rows)


def test_erp_and_excel_paths_give_an_identical_normalised_frame(fake):
    via_erp = erp_source.read_pos(CONFIG, fake, date(2026, 7, 20), date(2026, 7, 20))
    via_excel, _ = ingest._normalise_pos_frame(CONFIG, _excel_shaped(), "excel")
    key = ["bill_no", "mop"]
    a = via_erp.sort_values(key).reset_index(drop=True)
    b = via_excel.sort_values(key).reset_index(drop=True)
    assert list(a.columns) == list(b.columns)
    for col in ["bill_no", "site_ref", "store", "mop", "amount", "business_date", "bill_time", "created_by", "in_sale_universe",
                "paytm_ref", "axis_ref", "bajaj_ref", "tpedc_parse_failed", "mop_class"]:
        assert a[col].tolist() == b[col].tolist(), col


def test_references_come_out_of_the_terminal_reply_and_the_reply_itself_is_dropped(fake):
    df = erp_source.read_pos(CONFIG, fake, date(2026, 7, 20), date(2026, 7, 20))
    assert "TPEDCRESPONSE" not in df.columns and "edc_response" not in df.columns
    by_bill = df.set_index(["bill_no", "mop"])
    assert by_bill.loc[("SB58/024800/0726", "PAYTM EDC"), "paytm_ref"] == "20260720111030000285964210320294741"
    assert by_bill.loc[("SB58/024801/0726", "EDC OTHER AXIS"), "axis_ref"] == "973933049280"
    assert by_bill.loc[("SB58/024802/0726", "Bajajpay UPI"), "bajaj_ref"] == "BJ1234567"
    assert pd.isna(by_bill.loc[("SB58/024803/0726", "Cash"), "paytm_ref"])
    assert bool(by_bill.loc[("SB58/024803/0726", "PaytmDQR"), "tpedc_parse_failed"]) is True      # reported, never silent
    assert not bool(by_bill.loc[("SB58/024803/0726", "Cash"), "tpedc_parse_failed"])


def test_bill_time_amount_dates_and_sequence(fake):
    df = erp_source.read_pos(CONFIG, fake, date(2026, 7, 20), date(2026, 7, 20))
    row = df[(df.bill_no == "SB58/024800/0726")].iloc[0]
    assert row["bill_time"] == "15:30:20" and row["amount"] == 2254.0 and row["business_date"] == date(2026, 7, 20)
    two_lines = df[df.bill_no == "SB58/024803/0726"].sort_values("line_seq")
    assert two_lines["line_seq"].tolist() == [1, 2] and df["in_sale_universe"].all()


def test_an_unknown_mop_is_tagged_other_not_fatal(fake):
    conn = FakeErp(POS_FULL, [_pos_row(("SB1/1/0726", "S", "PHB", "NEPALI RUPPES", 12.0, None, "T", time(9, 0, 0)), POS_FULL)])
    df = erp_source.read_pos(CONFIG, conn, date(2026, 7, 20), date(2026, 7, 20))
    assert df["mop_class"].tolist() == ["other"]


def test_a_day_with_no_rows_gives_an_empty_frame_with_the_right_columns():
    conn = FakeErp(POS_FULL, [])
    raws = list(erp_source.iter_pos_raw(conn, date(2026, 7, 21), date(2026, 7, 21)))
    assert len(raws) == 1 and raws[0].empty and "BILLNO" in raws[0].columns


# --- one line per bill x reference-less MOP (the Excel export's shape; 2026-11-25) ----------------------------------

def _split_conn(extra=()):
    rows = [("SB39/001712/0426", "S1", "PHB", "EDC OTHER", 99.0, None, "T1", time(10, 0, 0)),
            ("SB39/001712/0426", "S1", "PHB", "EDC OTHER", 527.0, None, "T1", time(10, 0, 5)),
            ("SB39/001712/0426", "S1", "PHB", "Cash", 100.0, None, "T1", time(10, 0, 9))] + list(extra)
    return FakeErp(POS_FULL, [_pos_row(r, POS_FULL) for r in rows])


def test_reference_less_lines_of_one_bill_and_mop_become_one_line_with_the_exact_total():
    df = erp_source.read_pos(CONFIG, _split_conn(), date(2026, 7, 20), date(2026, 7, 20))
    other = df[df.mop == "EDC OTHER"]
    assert len(other) == 1 and other["amount"].iloc[0] == 626.0                       # 99 + 527, to the paisa
    assert other["bill_time"].iloc[0] == "10:00:00"                                   # the first line keeps its own time
    assert round(float(df["amount"].sum()), 2) == 726.0                               # no money added or lost
    assert sorted(df[df.bill_no == "SB39/001712/0426"]["line_seq"]) == [1, 2]         # renumbered, no gap


def test_a_line_that_carries_a_reference_is_never_merged():
    paytm1 = json.dumps({"head": {}, "body": {"acquirementId": "A1"}})
    paytm2 = json.dumps({"head": {}, "body": {"acquirementId": "A2"}})
    conn = FakeErp(POS_FULL, [_pos_row(("SB1/1/0726", "S", "PHB", "PAYTM EDC", 100.0, paytm1, "T", time(9, 0, 0)), POS_FULL),
                              _pos_row(("SB1/1/0726", "S", "PHB", "PAYTM EDC", 200.0, paytm2, "T", time(9, 0, 1)), POS_FULL),
                              _pos_row(("SB1/1/0726", "S", "PHB", "EDC OTHER AXIS", 50.0, AXIS, "T", time(9, 0, 2)), POS_FULL),
                              _pos_row(("SB1/1/0726", "S", "PHB", "EDC OTHER AXIS", 70.0, None, "T", time(9, 0, 3)), POS_FULL)])
    df = erp_source.read_pos(CONFIG, conn, date(2026, 7, 20), date(2026, 7, 20))
    assert len(df) == 4                                                              # two payments with their own references; a mixed group is left alone


def test_the_merge_can_be_switched_off_in_config(monkeypatch):
    class Cfg:
        raw = {"erp": {"merge_reference_less_lines": False}}
        mop_classification = CONFIG.mop_classification
    df = erp_source.read_pos(Cfg(), _split_conn(), date(2026, 7, 20), date(2026, 7, 20))
    assert len(df[df.mop == "EDC OTHER"]) == 2


# --- petty-cash vouchers ------------------------------------------------------------------------------------------

def _ptc_row(store, d, no, mode, head, ledger, amount):
    return (store, 1, d, no, mode, head, ledger, "", amount, amount, "remark")


def test_ptc_goes_through_the_shared_normaliser_and_flags_deposits():
    rows = [_ptc_row("ABD", D, "V1", "PAYMENT", "Cash/CMS Deposit", "CMS-RECD.", -87937.0),
            _ptc_row("ABD", D, "V2", "PAYMENT", "Tea and snacks", "EXPENSE", -320.0),
            _ptc_row("ABD", D, "V3", "RECEIVE", "Misc income", "INCOME", 150.0),
            _ptc_row("AGT", D, "V4", "PAYMENT", "Cash/CMS Deposit", "OTHER", -100.0)]       # head only: still a deposit, flagged elsewhere
    conn = FakeErp(POS_FULL, ptc_columns=PTC_COLS, ptc_rows=rows)
    df = erp_source.read_ptc(conn, date(2026, 7, 20), date(2026, 7, 20))
    assert len(df) == 4 and set(["store", "business_date", "voucher_no", "mode", "head", "ledger", "amount", "remarks"]) <= set(df.columns)
    dep = df[df["is_deposit"]]
    assert sorted(dep["voucher_no"]) == ["V1", "V4"] and dep["deposit_amount"].tolist() == [87937.0, 100.0]
    assert df.loc[df.voucher_no == "V2", "deposit_amount"].iloc[0] == 0.0
    assert df["business_date"].eq(date(2026, 7, 20)).all()
    assert round(df["amount"].sum(), 2) == round(-87937.0 - 320.0 + 150.0 - 100.0, 2)


def test_ptc_query_uses_the_same_half_open_range():
    conn = FakeErp(POS_FULL, ptc_columns=PTC_COLS, ptc_rows=[])
    list(erp_source.iter_ptc_raw(conn, date(2026, 7, 1), date(2026, 7, 31)))
    sql, params = [(s, p) for s, p in conn.executed if f"from {erp_source.PTC_VIEW}" in s][0]
    assert "where business_date >= %s and business_date < %s" in " ".join(sql.split()).lower()
    assert params == [datetime(2026, 7, 1), datetime(2026, 8, 1)]
