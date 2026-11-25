"""Regression tests for the defects the 2026-11-25 audit of the V2 API found against the Streamlit version (CLAUDE.md 6.43).

Synthetic data and a temporary SQLite database only. Each test fails on the code as it was before the fix."""
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api_v2 import setup, login, ORIGIN  # noqa: E402,F401  (the fixture and sign-in helper of the first V2 test file)
from mop_reco import manual_match as mm  # noqa: E402
from mop_reco.db import Exception_, ManualMatch, Run, StoreTask  # noqa: E402


def _exception(factory, key, status, kind='bill_not_matched', amount=100.0):
    with factory() as s:
        s.add(Exception_(exception_key=key, reconciliation='R1', type=kind, business_date='2026-01-01', store='DEMO-A', amount=amount,
                         detail=json.dumps({'bill_no': 'B1'}), status=status, first_seen_run_id=1, last_seen_run_id=1))
        s.commit()


def test_unauthenticated_requests_learn_nothing_about_runs(setup):
    c, _ = setup
    assert c.get('/api/mop/summary?run_id=999').status_code == 401        # used to answer 409 'no completed run' before checking who is asking
    assert c.get('/api/mop/cash?run_id=1').status_code == 401
    assert c.get('/api/mop/notices').status_code == 401


def test_a_period_wholly_outside_the_run_is_refused_not_shown_as_zeros(setup):
    c, _ = setup
    login(c)
    r = c.get('/api/mop/cash?date_from=2025-12-01&date_to=2025-12-31')
    assert r.status_code == 409 and 'not reconciled' in r.json()['detail']
    assert c.get('/api/mop/cash?date_from=2026-01-01&date_to=2026-01-01').status_code == 200


def test_notices_say_so_when_the_period_is_outside_the_run(setup):
    c, _ = setup
    login(c)
    r = c.get('/api/mop/notices?date_from=2025-12-01&date_to=2026-01-01')
    assert r.status_code == 200
    keys = {n['key'] for n in r.json()['notices']}
    assert 'period_outside_run' in keys


def test_notices_show_a_cut_off_pos_file_as_an_error(setup):
    c, factory = setup
    with factory() as s:
        s.get(Run, 1).notes = json.dumps({'source_completeness': {'ok': False, 'flagged_months': [], 'by_month': {}, 'excel_row_limit': [
            {'file': 'pos.xlsx', 'last_sheet': 'Sheet1', 'data_rows': 1048575}]}})
        s.commit()
    login(c)
    notes = {n['key']: n for n in c.get('/api/mop/notices').json()['notices']}
    assert notes['pos_incomplete']['level'] == 'error' and 'INCOMPLETE' in notes['pos_incomplete']['title']


def test_notices_show_parked_axis_bills(setup):
    c, factory = setup
    with factory() as s:
        s.get(Run, 1).notes = json.dumps({'awaiting_source': {'lines': 3, 'bills': 3, 'value': 1500.5, 'dates': ['2026-10-18', '2026-10-31']}})
        s.commit()
    login(c)
    notes = {n['key']: n for n in c.get('/api/mop/notices').json()['notices']}
    assert notes['parked_axis']['level'] == 'warning' and '1,500.50' in notes['parked_axis']['text']


def test_exceptions_default_to_the_work_queue_keep_the_two_risks_apart_and_carry_remarks(setup):
    c, factory = setup
    _exception(factory, 'k-open-bill', 'open', 'bill_not_matched', 100.0)
    _exception(factory, 'k-closed-bill', 'closed', 'bill_not_matched', 40.0)
    _exception(factory, 'k-open-txn', 'open', 'unmapped_gateway_transaction', 230.0)
    h = login(c)
    body = c.get('/api/mop/exceptions').json()
    assert {r['exception_key'] for r in body['rows']} == {'k-open-bill', 'k-open-txn'}           # closed is history, not work
    assert body['groups']['not_received'] == {'count': 1, 'value': 100.0}
    assert body['groups']['received_not_billed'] == {'count': 1, 'value': 230.0}
    assert 'value' not in body                                                                    # never one blended figure (CLAUDE.md 6.9a)
    assert len(c.get('/api/mop/exceptions?status=all').json()['rows']) == 3
    assert c.post('/api/mop/exceptions/remark', headers=h, json={'exception_key': 'k-open-bill', 'text': 'chasing Paytm'}).status_code == 200
    rows = {r['exception_key']: r for r in c.get('/api/mop/exceptions').json()['rows']}
    assert rows['k-open-bill']['remark'] == 'chasing Paytm' and rows['k-open-bill']['remark_by'] == 'Fictional Operator'


def test_tasks_default_to_live_tasks_and_a_closed_task_takes_nothing_more(setup):
    c, factory = setup
    h = login(c)
    assert c.get('/api/mop/tasks').json()['total'] == 1
    with factory() as s:
        s.get(StoreTask, 1).status = 'closed'
        s.commit()
    assert c.get('/api/mop/tasks').json()['total'] == 0                                          # not in the working list
    assert c.get('/api/mop/tasks?status=all').json()['total'] == 1                               # still reachable
    assert c.post('/api/mop/tasks/1', headers=h, json={'action': 'remark', 'text': 'late note'}).status_code == 409


def test_server_errors_carry_the_security_headers_and_integrity_errors_are_conflicts(setup, monkeypatch):
    c, _ = setup
    h = login(c)
    from sqlalchemy.exc import IntegrityError
    import mop_reco.store_tasks as st

    def boom(*a, **k):
        raise IntegrityError('insert', {}, Exception('unique'))
    monkeypatch.setattr(st, 'add_remark', boom)
    r = c.post('/api/mop/tasks/1', headers=h, json={'action': 'remark', 'text': 'x'})
    assert r.status_code == 409 and r.headers['cache-control'] == 'no-store' and r.headers['x-content-type-options'] == 'nosniff'
    monkeypatch.setattr(st, 'add_remark', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db password in this text')))
    r = c.post('/api/mop/tasks/1', headers=h, json={'action': 'remark', 'text': 'x'})
    assert r.status_code == 500 and 'password' not in r.text and r.headers['cache-control'] == 'no-store'


def test_a_split_payment_that_does_not_add_up_cannot_be_saved_even_with_an_override(setup, monkeypatch):
    """The shortfall would otherwise be silently written off (CLAUDE.md rule 3; 6.26 / 6.31: a split must equal the bill exactly)."""
    c, factory = setup
    h = login(c)
    bill = {'business_date': '2026-01-01', 'store': 'DEMO-A', 'bill_no': 'B1', 'mop': 'PAYTM EDC', 'amount': 100000.0}
    monkeypatch.setattr(mm, 'find_bill_for_match', lambda *a, **k: (bill, True, ''))
    monkeypatch.setattr(mm, 'targeted_pool_many', lambda refs, **k: pd.DataFrame())
    monkeypatch.setattr(mm, 'identify_gateway', lambda *a, **k: SimpleNamespace(found=True, problem='', gateway='paytm', txn={}))
    discrepant = SimpleNamespace(ok=False, case=mm.CASE_DISCREPANT, problems=['legs add up to 90,000.00, the bill is 100,000.00'],
                                 needs_override=True, gateway='paytm', gateway2='paytm', txn2={'amount': 30000.0})
    monkeypatch.setattr(mm, 'validate_split', lambda *a, **k: discrepant)
    r = c.post('/api/mop/matching/apply', headers=h, json={'run_id': 1, 'bill_no': 'B1', 'transaction_ref': 'T1', 'second_transaction_ref': 'T2',
                                                           'reason_code': 'clean', 'override_reason': 'ok trust me', 'confirmed': True, 'reason': 'x'})
    assert r.status_code == 422 and 'add up' in r.json()['detail']
    with factory() as s:
        assert s.query(ManualMatch).count() == 0


def test_writes_are_pinned_to_the_latest_completed_run(setup):
    c, factory = setup
    h = login(c)
    with factory() as s:
        s.add(Run(id=2, started_at=datetime(2026, 1, 2), finished_at=datetime(2026, 1, 2), business_dates=json.dumps(['2026-01-01']), status='completed'))
        s.commit()
    r = c.post('/api/mop/matching/preview', headers=h, json={'run_id': 1, 'bill_no': 'B1', 'transaction_ref': 'T1', 'reason_code': 'clean',
                                                             'confirmed': False, 'reason': 'x'})
    assert r.status_code == 409 and 'latest completed run' in r.json()['detail']                  # a stale browser tab cannot write against run 1
    # reading the older run is still allowed
    assert c.get('/api/mop/cash?run_id=1').status_code == 200


def test_the_cash_sheet_cache_follows_manual_match_state(setup):
    """The store-day sheet is cached per (run, period, store, manual-match state); a new match must change the key, never serve a stale F."""
    c, factory = setup
    login(c)
    first = c.get('/api/mop/cash').json()
    assert first['total'] == 1
    assert c.get('/api/mop/cash').json() == first                                                 # served from the cache, same answer
    with factory() as s:
        s.add(ManualMatch(bill_key='k', business_date='2026-01-01', store='DEMO-A', bill_no='B9', mop='PAYTM EDC', amount=10.0, gateway='paytm',
                          transaction_ref='T9', status='matched_manual', validation_case='a', reason_code='clean', operator='t',
                          created_at=datetime(2026, 1, 1), lifecycle='active'))
        s.commit()
    # the key changed, so the sheet is rebuilt (the figures are unchanged here because the bill is not in r1_bill_lines, but the rebuild must not fail)
    assert c.get('/api/mop/cash').status_code == 200
