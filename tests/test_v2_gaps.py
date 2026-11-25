"""The three V2 gaps closed on 25 Nov 2026 (Float, Summary tables, Exceptions pairing) - synthetic data and a temporary SQLite database only."""
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api_v2 import setup, login, ORIGIN  # noqa: E402,F401
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco.db import Exception_, ManualMatch, StoreDayAgg  # noqa: E402


def _high_float_store(factory, store='DEMO-B', opening=90000, deposit=20000):
    with factory() as s:
        s.add(StoreDayAgg(run_id=1, business_date='2026-01-01', store=store, total_sale=0, digital_sale=0, cash_sale=0, opening_balance=opening,
                          cash_deposit=deposit, cash_received=0, cash_receivable=deposit, cash_expense_income=0, closing_balance=opening - deposit,
                          float_amount=opening - deposit))
        s.commit()


# ------------------------------------------------------------------------------------------------ Float

def test_float_default_view_is_the_work_queue_with_tiles_and_quality(setup):
    c, f = setup
    _high_float_store(f)
    login(c)
    body = c.get('/api/mop/float').json()
    assert [r['store'] for r in body['rows']] == ['DEMO-B']                       # high float, no remark: the work queue (DEMO-A is at the threshold)
    assert body['tiles']['stores_above'] == 1 and body['tiles']['float_above'] == 70000.0 and body['tiles']['high_no_remark'] == 1
    assert body['quality']['negative_n'] == 0
    assert c.get('/api/mop/float?view=all').json()['total'] == 2


def test_a_float_remark_takes_a_store_day_out_of_the_work_queue_and_keeps_history(setup):
    c, f = setup
    _high_float_store(f)
    h = login(c)
    assert c.post('/api/mop/float/remark', headers=h, json={'store': 'DEMO-B', 'business_date': '2026-01-01', 'text': 'Pickup missed'}).status_code == 200
    assert c.get('/api/mop/float').json()['rows'] == []                            # remarked: no longer in the queue
    hist = c.get('/api/mop/float/history?store=DEMO-B&business_date=2026-01-01').json()
    assert len(hist) == 1 and hist[0]['remark'] == 'Pickup missed'
    shown = {r['store']: r for r in c.get('/api/mop/float?view=high').json()['rows']}
    assert shown['DEMO-B']['remark'] == 'Pickup missed' and shown['DEMO-B']['remark_by'] == 'Fictional Operator'


def test_negative_float_is_listed_never_floored(setup):
    c, f = setup
    _high_float_store(f, 'DEMO-C', opening=5000, deposit=20000)                    # a deposit larger than the opening
    login(c)
    body = c.get('/api/mop/float?view=all').json()
    assert body['quality']['negative_n'] == 1 and body['quality']['negative_value'] == -15000.0
    neg = c.get('/api/mop/float/data-quality').json()['negative']
    assert neg[0]['store'] == 'DEMO-C' and neg[0]['float_amount'] == -15000.0


def test_float_upload_validates_every_row_and_applies_nothing_until_confirmed(setup):
    c, f = setup
    _high_float_store(f)
    h = login(c)
    sheet = pd.DataFrame([{'store': 'DEMO-B', 'business_date': '2026-01-01', 'remark': 'Agency delayed'},
                          {'store': 'DEMO-A', 'business_date': '2026-01-01', 'remark': 'not high float'},
                          {'store': 'NOPE', 'business_date': '2026-01-01', 'remark': 'unknown store'}])
    buf = io.BytesIO()
    sheet.to_excel(buf, index=False)
    files = {'file': ('f.xlsx', buf.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
    preview = c.post('/api/mop/float/upload', headers=h, files=files).json()
    assert preview['accepted'] == 1 and preview['rejected'] == 2 and preview['applied'] == 0
    assert c.get('/api/mop/float').json()['total'] == 1                            # nothing applied yet
    done = c.post('/api/mop/float/upload?confirm=true', headers=h, files=files).json()
    assert done['applied'] == 1
    assert c.get('/api/mop/float').json()['total'] == 0


# ------------------------------------------------------------------------------------------------ Summary tables

def test_summary_by_date_carries_total_and_each_date_opens_onto_its_stores(setup):
    c, f = setup
    _high_float_store(f)
    login(c)
    body = c.get('/api/mop/summary/by-date').json()
    assert [r['date'] for r in body['rows']] == ['2026-01-01', 'TOTAL'] and body['date_check'] == []
    stores = c.get('/api/mop/summary/by-date/2026-01-01').json()['rows']
    assert [r['store'] for r in stores] == ['DEMO-A', 'DEMO-B']


def test_summary_tables_report_the_two_sided_controls(setup):
    c, _ = setup
    login(c)
    body = c.get('/api/mop/summary/tables').json()
    assert body['bills_ok'] is True and body['txns_ok'] is True
    assert set(body['bills']) == {'open', 'auto', 'manual', 'total'}
    assert 'unmapped_n' in body['txns']
    assert c.get('/api/mop/summary/controls').status_code == 200
    assert c.get('/api/mop/summary/matched').json()['total'] == 0


def test_the_new_summary_routes_need_sign_in(setup):
    c, _ = setup
    for path in ('/float', '/float/data-quality', '/summary/tables', '/summary/by-date', '/summary/matched', '/exceptions/bills', '/exceptions/transactions'):
        assert c.get('/api/mop' + path).status_code == 401, path


# ------------------------------------------------------------------------------------------------ Exceptions pairing

def _exc(factory, key, kind, store, amount, detail, date='2026-01-01', time=None):
    with factory() as s:
        s.add(Exception_(exception_key=key, reconciliation='R1', type=kind, business_date=date, store=store, amount=amount, detail=json.dumps(detail),
                         status='open', first_seen_run_id=1, last_seen_run_id=1, transaction_time=time))
        s.commit()


def _seed_pair(factory):
    _exc(factory, 'b1', 'bill_not_matched', 'DEMO-A', 500.0, {'bill_no': 'BILL-1', 'mop': 'PAYTM EDC'})
    _exc(factory, 'b2', 'bill_not_matched', 'DEMO-A', 777.0, {'bill_no': 'BILL-2', 'mop': 'PAYTM EDC'})
    _exc(factory, 't1', 'unmapped_gateway_transaction', 'DEMO-A', 500.0, {'gateway': 'paytm', 'transaction_id': 'TXN-AAA'}, time='10:00:00')


def test_exception_bills_carry_the_one_suggested_transaction_and_none_where_nothing_fits(setup):
    c, f = setup
    _seed_pair(f)
    login(c)
    body = c.get('/api/mop/exceptions/bills').json()
    rows = {r['bill_no']: r for r in body['rows']}
    assert rows['BILL-1']['Txn 1'] == 'TXN-AAA' and rows['BILL-1']['Amount 1'] == 500.0 and rows['BILL-1']['Txn 2'] == ''
    assert rows['BILL-2']['Txn 1'] == '' and rows['BILL-2']['Amount 1'] is None
    assert body['pairing']['exact'] == 1 and body['pairing']['none'] == 1
    txns = c.get('/api/mop/exceptions/transactions').json()['rows']
    assert [r['transaction_id'] for r in txns] == ['TXN-AAA'] and 'matched_bill' in txns[0]


def test_a_transaction_is_suggested_to_one_bill_only(setup):
    c, f = setup
    _exc(f, 'b1', 'bill_not_matched', 'DEMO-A', 500.0, {'bill_no': 'BILL-1', 'mop': 'PAYTM EDC'})
    _exc(f, 'b3', 'bill_not_matched', 'DEMO-A', 500.0, {'bill_no': 'BILL-3', 'mop': 'PAYTM EDC'})
    _exc(f, 't1', 'unmapped_gateway_transaction', 'DEMO-A', 500.0, {'gateway': 'paytm', 'transaction_id': 'TXN-AAA'}, time='10:00:00')
    login(c)
    suggested = [r['Txn 1'] for r in c.get('/api/mop/exceptions/bills').json()['rows'] if r['Txn 1']]
    assert len(suggested) <= 1                                                     # never the same transaction against two bills (CLAUDE.md 6.20)


def _stub_match(monkeypatch, valid=True):
    bill = {'business_date': '2026-01-01', 'store': 'DEMO-A', 'bill_no': 'BILL-1', 'mop': 'PAYTM EDC', 'amount': 500.0}
    monkeypatch.setattr(mm, 'find_bill_for_match', lambda s, run_id, n: (bill, True, ''))
    monkeypatch.setattr(mm, 'targeted_pool_many', lambda refs, **k: pd.DataFrame())
    verdict = SimpleNamespace(ok=valid, case=mm.CASE_CLEAN if valid else mm.CASE_DISCREPANT, problems=[] if valid else ['amount differs: 400.00 against 500.00'],
                              gateway='paytm', txn={'amount': 400.0, 'store': 'DEMO-A'})
    monkeypatch.setattr(mm, 'validate_by_id', lambda *a, **k: verdict)
    recorded = []
    monkeypatch.setattr(mm, 'record', lambda *a, **k: recorded.append((a, k)) or SimpleNamespace(id=1))
    return recorded


def test_typing_a_clean_transaction_records_the_match_as_the_signed_in_person(setup, monkeypatch):
    c, _ = setup
    h = login(c)
    recorded = _stub_match(monkeypatch)
    r = c.post('/api/mop/exceptions/match', headers=h, json={'run_id': 1, 'bill_no': 'BILL-1', 'txn1': "'TXN-AAA"})
    assert r.status_code == 200 and 'Matched BILL-1 to TXN-AAA' in r.json()['message']        # the leading apostrophe Excel adds is stripped
    args, kwargs = recorded[0]
    assert args[4] == 'manual_match_by_bill' and args[5] == 'Fictional Operator' and kwargs['source'] == 'inline'


def test_a_transaction_that_does_not_validate_is_refused_with_its_reasons_and_nothing_is_recorded(setup, monkeypatch):
    c, _ = setup
    h = login(c)
    recorded = _stub_match(monkeypatch, valid=False)
    r = c.post('/api/mop/exceptions/match', headers=h, json={'run_id': 1, 'bill_no': 'BILL-1', 'txn1': 'TXN-AAA'})
    assert r.status_code == 422 and 'amount differs' in r.json()['detail'] and recorded == []


def test_matching_is_pinned_to_the_latest_completed_run(setup):
    c, f = setup
    h = login(c)
    from datetime import datetime
    from mop_reco.db import Run
    with f() as s:
        s.add(Run(id=2, started_at=datetime(2026, 1, 2), finished_at=datetime(2026, 1, 2), business_dates=json.dumps(['2026-01-01']), status='completed'))
        s.commit()
    r = c.post('/api/mop/exceptions/match', headers=h, json={'run_id': 1, 'bill_no': 'BILL-1', 'txn1': 'TXN-AAA'})
    assert r.status_code == 409 and 'latest completed run' in r.json()['detail']


def _cells(content: bytes) -> list:
    frame = pd.read_excel(io.BytesIO(content), header=None, dtype=str)
    return [str(v) for v in frame.values.ravel() if str(v) != 'nan']


def test_exception_table_exports_carry_the_pairing_columns_but_never_the_screen_only_ones(setup):
    c, f = setup
    _seed_pair(f)
    login(c)
    r = c.get('/api/mop/exceptions/bills/export')
    assert r.status_code == 200, r.text
    cells = _cells(r.content)
    assert 'Txn 1' in cells and 'TXN-AAA' in cells                                # the suggestion travels with the file
    assert 'More' not in cells and 'Match' not in cells                           # screen affordances are never data
    t = c.get('/api/mop/exceptions/transactions/export')
    assert t.status_code == 200 and 'TXN-AAA' in _cells(t.content)
