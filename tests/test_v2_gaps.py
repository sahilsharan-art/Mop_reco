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


# ------------------------------------------------------------------------------------------------ Exceptions: the filled table, uploaded

BILLS = {'BILL-1': 500.0, 'BILL-2': 777.0}


def _seed_real(factory):
    """Two open bills with their REAL exception keys (an upload finds a bill's exception by key) and one transaction."""
    from mop_reco.exceptions import bill_exception_key
    for number, amount in BILLS.items():
        _exc(factory, bill_exception_key('2026-01-01', 'DEMO-A', number, 'PAYTM EDC'), 'bill_not_matched', 'DEMO-A', amount, {'bill_no': number, 'mop': 'PAYTM EDC'})
    _exc(factory, 'paytm|TXN-AAA', 'unmapped_gateway_transaction', 'DEMO-A', 500.0, {'gateway': 'paytm', 'transaction_id': 'TXN-AAA'}, time='10:00:00')


def _stub_engine(monkeypatch, valid=True):
    """The bill lookup answers per bill number; the validator and the recorder are stubs, so only the upload's own rules are under test."""
    monkeypatch.setattr(mm, 'find_bill_for_match', lambda s, run_id, n: (
        ({'business_date': '2026-01-01', 'store': 'DEMO-A', 'bill_no': n, 'mop': 'PAYTM EDC', 'amount': BILLS[n]}, True, '') if n in BILLS else (None, False, f'bill {n} not found')))
    monkeypatch.setattr(mm, 'targeted_pool_many', lambda refs, **k: pd.DataFrame())
    monkeypatch.setattr(mm, 'targeted_pool', lambda ref, **k: pd.DataFrame())
    problems = [] if valid else ['amount differs: 400.00 against 500.00']
    monkeypatch.setattr(mm, 'validate_by_id', lambda *a, **k: SimpleNamespace(ok=valid, case=mm.CASE_CLEAN if valid else mm.CASE_DISCREPANT, problems=problems,
                                                                             gateway='paytm', txn={}, date_lag_note=''))
    recorded = []
    monkeypatch.setattr(mm, 'record', lambda *a, **k: recorded.append(a) or SimpleNamespace(id=1))
    return recorded


def _fill(content: bytes, edits: dict) -> bytes:
    """The export with cells typed against named bills - what a person does in Excel. The header block above the table is kept."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content))
    ws = wb[wb.sheetnames[0]]
    head = next(r for r in range(1, ws.max_row + 1) if 'bill_no' in [str(x.value) for x in ws[r]])
    names = [str(x.value) for x in ws[head]]
    for r in range(head + 1, ws.max_row + 1):
        bill = str(ws.cell(r, names.index('bill_no') + 1).value)
        for col, val in edits.get(bill, {}).items():
            ws.cell(r, names.index(col) + 1).value = val
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _post_upload(c, h, body, kind='bills', confirm=False, expect=-1, run_id=1):
    files = {'file': ('table.xlsx', body, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
    return c.post(f'/api/mop/exceptions/{kind}/upload', headers=h, files=files,
                  data={'run_id': str(run_id), 'confirm': 'true' if confirm else 'false', 'expect_accepted': str(expect)})


def test_a_filled_table_is_checked_first_and_writes_nothing_until_confirmed(setup, monkeypatch):
    c, f = setup
    _seed_real(f)
    h = login(c)
    recorded = _stub_engine(monkeypatch)
    body = _fill(c.get('/api/mop/exceptions/bills/export').content, {'BILL-2': {'remark': 'chasing the gateway'}})
    out = _post_upload(c, h, body).json()
    assert out['applied'] is False and out['rejected'] == 0 and out['can_apply'] is True
    assert out['actions'] == {'match': 1, 'refund': 0, 'remark': 1}       # BILL-1 carries the system's own suggestion (TXN-AAA), BILL-2 a remark
    assert recorded == []                                                 # a check never writes


def test_one_rejected_row_applies_nothing_and_confirming_is_refused(setup, monkeypatch):
    c, f = setup
    _seed_real(f)
    h = login(c)
    recorded = _stub_engine(monkeypatch, valid=False)
    body = _fill(c.get('/api/mop/exceptions/bills/export').content, {'BILL-2': {'remark': 'fine'}})
    check = _post_upload(c, h, body).json()
    assert check['rejected'] == 1 and check['accepted'] == 1 and check['can_apply'] is False
    assert any('amount differs' in str(row['why']) for row in check['rows'])
    refused = _post_upload(c, h, body, confirm=True, expect=check['accepted'])
    assert refused.status_code == 422 and 'nothing is applied' in refused.json()['detail']
    assert recorded == []                                                 # the acceptable remark row was not applied either


def test_a_clean_file_applies_in_one_step_and_a_changed_check_is_refused(setup, monkeypatch):
    c, f = setup
    _seed_real(f)
    h = login(c)
    recorded = _stub_engine(monkeypatch)
    body = _fill(c.get('/api/mop/exceptions/bills/export').content, {'BILL-2': {'remark': 'chasing the gateway'}})
    check = _post_upload(c, h, body).json()
    stale = _post_upload(c, h, body, confirm=True, expect=check['accepted'] + 5)
    assert stale.status_code == 409 and recorded == []                    # checks differently from what was reviewed: nothing written
    done = _post_upload(c, h, body, confirm=True, expect=check['accepted'])
    assert done.status_code == 200, done.text
    counts = done.json()['counts']
    assert done.json()['applied'] is True and counts['matched'] == 1 and counts['remarks'] == 1 and len(recorded) == 1
    assert _post_upload(c, h, body).json()['actions']['remark'] == 0      # the remark is recorded now: that row is unchanged


def test_a_file_that_is_not_the_table_is_refused_with_the_reason_and_needs_sign_in(setup):
    c, _ = setup
    h = login(c)
    junk = io.BytesIO()
    pd.DataFrame({'a': [1]}).to_excel(junk, index=False)
    r = _post_upload(c, h, junk.getvalue())
    assert r.status_code == 422 and 'does not look like' in r.json()['detail']
    c.cookies.clear()
    assert _post_upload(c, {}, junk.getvalue()).status_code in (401, 403)   # no session, no CSRF token: refused before anything is read


def test_a_filled_transactions_table_matches_by_the_bill_typed_and_never_by_both_match_and_refund(setup, monkeypatch):
    c, f = setup
    _seed_real(f)
    h = login(c)
    recorded = _stub_engine(monkeypatch)
    monkeypatch.setattr(mm, 'validate', lambda *a, **k: SimpleNamespace(ok=True, case=mm.CASE_CLEAN, problems=[], gateway='paytm', txn={}, date_lag_note=''))
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(c.get('/api/mop/exceptions/transactions/export').content))
    ws = wb[wb.sheetnames[0]]
    head = next(r for r in range(1, ws.max_row + 1) if 'transaction_id' in [str(x.value) for x in ws[r]])
    names = [str(x.value) for x in ws[head]]
    ws.cell(head + 1, names.index('matched_bill') + 1).value = 'BILL-1'
    out = io.BytesIO()
    wb.save(out)
    ok = _post_upload(c, h, out.getvalue(), kind='transactions').json()
    assert ok['accepted'] == 1 and ok['rejected'] == 0 and ok['actions']['match'] == 1
    ws.cell(head + 1, names.index('customer_name') + 1).value = 'A Customer'            # a match AND a refund on one row
    both = io.BytesIO()
    wb.save(both)
    bad = _post_upload(c, h, both.getvalue(), kind='transactions').json()
    assert bad['rejected'] == 1 and 'not both' in str(bad['rows'])
    assert recorded == []
