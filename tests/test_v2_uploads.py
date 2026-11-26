"""The V2 Upload API: one upload and one check (stage), store by token (confirm), the month grid, the history and the folder comparison.
Synthetic files and a temporary SQLite database only."""
import io
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api_v2 import setup, login, ORIGIN  # noqa: E402,F401
from mop_reco import upload_tools as ut  # noqa: E402

PAYTM_COLS = ['transaction_id', 'transaction_date', 'status', 'transaction_type', 'original_mid', 'amount', 'commission', 'gst', 'payout_id', 'utr_no',
              'settled_amount', 'settled_date', 'payment_mode', 'reference_transaction_id']


def _paytm_zip(days):
    rows = [','.join(PAYTM_COLS)]
    for i, d in enumerate(days):
        dd = f"{d[8:10]}-{d[5:7]}-{d[0:4]}"                        # Paytm writes dd-mm-yyyy
        rows.append(f"'T{i}','{dd} 10:00:00','SUCCESS','ACQUIRING','M1','100.00','1.5','0.5','P1','U1','98.00','{dd}','UPI',''")
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('part-00000.csv', '\n'.join(rows) + '\n')
    return out.getvalue()


def _stage(c, h, body, source='paytm_settlement', month='2026-11', name='p.zip'):
    return c.post('/api/mop/uploads/stage', headers=h, files={'file': (name, body, 'application/zip')}, data={'source_key': source, 'month': month})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv('MOP_SOURCE_POS', 'erp')
    monkeypatch.setenv('MOP_SOURCE_PTC', 'erp')
    monkeypatch.setattr(ut, 'first_month', lambda config: '2026-10')


def _wait_read(c, upload_id, tries=60):
    for _ in range(tries):
        row = next(r for r in c.get('/api/mop/uploads/history').json()['rows'] if r['id'] == upload_id)
        if row['stats'] != 'reading':
            return row
        time.sleep(0.1)
    raise AssertionError('statistics were not read in time')


def test_the_erp_sources_and_masters_are_not_offered_and_are_refused(setup):
    c, _ = setup
    h = login(c)
    keys = {x['key'] for x in c.get('/api/mop/uploads/sources').json()['sources']}
    assert {'paytm_settlement', 'axis_edc', 'bajaj', 'axis_cms'} <= keys and not ({'pos_bill_wise', 'ptc'} & keys)
    assert 'ptc' in c.get('/api/mop/uploads/sources').json()['from_erp']
    r = _stage(c, h, b'x', source='ptc', name='p.csv')
    assert r.status_code == 422 and 'ERP' in r.json()['detail']


def test_stage_checks_once_stores_nothing_and_confirm_stores_by_token_only_once(setup):
    c, _ = setup
    h = login(c)
    staged = _stage(c, h, _paytm_zip(['2026-11-01', '2026-11-02']))
    assert staged.status_code == 200, staged.text
    body = staged.json()
    assert body['ok'] is True and body['row_count'] == 2 and body['date_range'][0].startswith('2026-11-01')
    assert c.get('/api/mop/uploads/history').json()['total'] == 0          # staged is not stored
    done = c.post('/api/mop/uploads/confirm', headers=h, json={'token': body['token']})
    assert done.status_code == 200, done.text
    assert c.get('/api/mop/uploads/history').json()['total'] == 1
    assert c.post('/api/mop/uploads/confirm', headers=h, json={'token': body['token']}).status_code == 404   # a token is used once


def test_a_wrong_month_fails_the_check_and_cannot_be_confirmed(setup):
    c, _ = setup
    h = login(c)
    body = _stage(c, h, _paytm_zip(['2026-11-01']), month='2026-10').json()
    assert body['ok'] is False and any('wrong' in e.lower() or 'month' in e.lower() for e in body['errors'])
    assert c.post('/api/mop/uploads/confirm', headers=h, json={'token': body['token']}).status_code == 422


def test_only_the_person_who_staged_a_file_can_confirm_it(setup):
    c, _ = setup
    h = login(c)
    token = _stage(c, h, _paytm_zip(['2026-11-01'])).json()['token']
    c.cookies.clear()
    other = login(c, 'demo.admin')
    assert c.post('/api/mop/uploads/confirm', headers=other, json={'token': token}).status_code == 403


def test_after_confirming_statistics_are_read_and_the_grid_and_history_show_it(setup):
    c, _ = setup
    h = login(c)
    token = _stage(c, h, _paytm_zip(['2026-11-01', '2026-11-02', '2026-11-02'])).json()['token']
    upload_id = c.post('/api/mop/uploads/confirm', headers=h, json={'token': token}).json()['id']
    row = _wait_read(c, upload_id)
    assert row['stats'] == 'read' and row['data'] == 'Paytm settlement' and row['month'] == '2026-11' and row['rows'] == 3
    assert row['uploaded_by'] == 'Fictional Operator'
    grid = c.get('/api/mop/uploads/coverage').json()
    paytm = next(r for r in grid['rows'] if r['source_key'] == 'paytm_settlement')['months']
    assert paytm['2026-10']['state'] == 'none' and paytm['2026-11']['dates_present'] == 2 and paytm['2026-11']['files'] == 1
    assert c.get('/api/mop/uploads/history?month=2026-10').json()['total'] == 0
    assert c.get('/api/mop/uploads/history?source_key=paytm_settlement').json()['total'] == 1


def test_withdrawing_removes_it_from_the_grid_but_keeps_the_history(setup):
    c, _ = setup
    h = login(c)
    token = _stage(c, h, _paytm_zip(['2026-11-01'])).json()['token']
    upload_id = c.post('/api/mop/uploads/confirm', headers=h, json={'token': token}).json()['id']
    _wait_read(c, upload_id)
    r = c.post(f'/api/mop/uploads/{upload_id}/withdraw', headers=h, json={'confirmed': True, 'reason': 'wrong file'})
    assert r.status_code == 200, r.text
    paytm = next(x for x in c.get('/api/mop/uploads/coverage').json()['rows'] if x['source_key'] == 'paytm_settlement')['months']
    assert paytm['2026-11']['state'] == 'none'
    assert c.get('/api/mop/uploads/history?status=deleted').json()['total'] == 1


def test_compare_without_a_baseline_is_said_not_shown_as_zero(setup, monkeypatch, tmp_path):
    c, _ = setup
    login(c)
    monkeypatch.setattr(ut, 'BASELINE_PATH', tmp_path / 'none.json')
    monkeypatch.setattr(ut, 'COMPARE_PATH', tmp_path / 'none_compare.json')
    body = c.get('/api/mop/uploads/compare').json()
    assert body['quick']['available'] is False and body['exact']['state'] == 'never'
    h = login(c)
    assert c.post('/api/mop/uploads/compare/run', headers=h).status_code == 409        # nothing to compare with: refused, said so


def test_the_new_routes_need_sign_in(setup):
    c, _ = setup
    for path in ('/uploads/sources', '/uploads/coverage', '/uploads/history', '/uploads/compare'):
        assert c.get('/api/mop' + path).status_code == 401, path
