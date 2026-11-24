"""Run from Mop_reco root after copying app/api_v2.py and this file.
All fixtures are synthetic and use a temporary SQLite database, never production.
"""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from mop_reco.db import Base, Run, StoreDayAgg, DateTotalAgg, MopDayAgg, StoreTask, StoreTaskEvent, AppUser
from mop_reco import auth, store_sheet_source, masters_db
from api_v2 import create_app, SESSION_COOKIE

ORIGIN = 'http://localhost:8091'
# Literal fixtures only. No real accounts or sales values.
PASSWORD = 'FictionalTest!892'

@pytest.fixture
def setup(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'fictional.sqlite'), connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    cfg = SimpleNamespace(raw={},base_dir=tmp_path,sources={'bank_statement':tmp_path/'fictional-bank'},encoding_for=lambda key: 'utf-8')
    with factory() as s:
        admin=auth.create_user(s,'demo.admin','Fictional Administrator','admin',PASSWORD,'test',must_change=False)
        operator=auth.create_user(s,'demo.operator','Fictional Operator','user',PASSWORD,'test',must_change=False)
        forced=auth.create_user(s,'demo.temporary','Fictional Temporary','user',PASSWORD,'test',must_change=True)
        s.add(Run(id=1,started_at=datetime(2026,1,1),finished_at=datetime(2026,1,1),business_dates=json.dumps(['2026-01-01']),status='completed'))
        amounts=dict(total_sale=100000,digital_sale=60000,cash_sale=40000,opening_balance=30000,cash_deposit=20000,cash_received=15000,cash_receivable=5000,cash_expense_income=0,closing_balance=50000,float_amount=10000)
        s.add(StoreDayAgg(run_id=1,business_date='2026-01-01',store='DEMO-A',**amounts))
        s.add(DateTotalAgg(run_id=1,business_date='2026-01-01',**amounts))
        s.add(MopDayAgg(run_id=1,business_date='2026-01-01',store='DEMO-A',bucket='cash',sale=40000))
        s.add(StoreTask(id=1,task_key='fictional-task',task_type='high_float',store='DEMO-A',business_date='2026-01-01',amount=10000,raised_on='2026-01-01',due_by='2026-01-02',status='open',detail='{}'))
        s.commit()
    client=TestClient(create_app(factory,cfg,ORIGIN),raise_server_exceptions=False)
    yield client,factory
    client.close();engine.dispose()


def login(client, username='demo.operator'):
    response=client.post('/api/mop/auth/login',headers={'Origin':ORIGIN},json={'username':username,'password':PASSWORD})
    assert response.status_code==200, response.text
    return {'Origin':ORIGIN,'X-CSRF-Token':client.cookies.get('mop_v2_csrf')}


def test_signed_out_cannot_read_records(setup):
    c,_=setup
    assert c.get('/api/mop/tasks').status_code==401


def test_login_sets_httponly_session_and_never_returns_token(setup):
    c,_=setup
    r=c.post('/api/mop/auth/login',headers={'Origin':ORIGIN},json={'username':'demo.operator','password':PASSWORD})
    assert r.status_code==200
    assert 'HttpOnly' in r.headers['set-cookie']
    assert 'token' not in r.json() and 'password_hash' not in r.json()
    assert c.get('/api/mop/auth/me').json()['username']=='demo.operator'


def test_wrong_origin_cannot_login(setup):
    c,_=setup
    assert c.post('/api/mop/auth/login',headers={'Origin':'https://untrusted.invalid'},json={'username':'demo.operator','password':PASSWORD}).status_code==403


def test_mutation_requires_csrf(setup):
    c,_=setup;login(c)
    assert c.post('/api/mop/tasks/1',headers={'Origin':ORIGIN},json={'action':'remark','text':'Test'}).status_code==403


def test_operator_cannot_manage_users(setup):
    c,_=setup;h=login(c)
    assert c.get('/api/mop/users').status_code==403
    assert c.post('/api/mop/users/1',headers=h,json={'action':'deactivate'}).status_code==403


def test_forced_password_change_blocks_data(setup):
    c,_=setup;login(c,'demo.temporary')
    assert c.get('/api/mop/bootstrap').status_code==403
    assert c.get('/api/mop/auth/me').json()['must_change_password'] is True


def test_task_remark_reads_back_and_actor_is_session_user(setup):
    c,f=setup;h=login(c)
    r=c.post('/api/mop/tasks/1',headers=h,json={'action':'remark','text':'Synthetic reconciliation note','operator':'attacker'})
    assert r.status_code==200,r.text
    rows=c.get('/api/mop/tasks?store=DEMO-A').json()['rows']
    assert rows[0]['HO remark']=='Synthetic reconciliation note'
    with f() as s:
        assert s.query(StoreTaskEvent).one().by=='Fictional Operator'


def test_task_close_requires_reason_and_preserves_history(setup):
    c,f=setup;h=login(c)
    assert c.post('/api/mop/tasks/1',headers=h,json={'action':'close','text':' '}).status_code==422
    assert c.post('/api/mop/tasks/1',headers=h,json={'action':'close','text':'Fictional evidence reviewed'}).status_code==200
    with f() as s:
        assert s.get(StoreTask,1).status=='closed'
        assert s.query(StoreTaskEvent).one().event=='closed'


def test_cash_retains_existing_amounts(setup):
    c,_=setup;login(c)
    r=c.get('/api/mop/cash?store=DEMO-A')
    assert r.status_code==200,r.text
    row=r.json()['rows'][0]
    assert row['cash_deposit']==20000 and row['cash_received']==15000
    assert row['cash_receivable']==5000 and row['float_amount']==10000


def test_missing_aggregates_never_read_raw_history(setup,monkeypatch):
    c,_=setup;login(c)
    def fail(*a,**kw): raise AssertionError('raw fallback must not run')
    monkeypatch.setattr(store_sheet_source,'build',fail)
    assert c.get('/api/mop/cash?store=NOT-EXTRACTED').status_code==409


def test_logout_invalidates_existing_session(setup):
    c,_=setup;h=login(c)
    token=c.cookies.get(SESSION_COOKIE)
    assert c.post('/api/mop/auth/logout',headers=h).status_code==200
    assert c.get('/api/mop/auth/me',headers={'Cookie':f'{SESSION_COOKIE}={token}'}).status_code==401


def test_last_admin_protection_uses_existing_engine(setup):
    c,_=setup;h=login(c,'demo.admin')
    assert c.post('/api/mop/users/1',headers=h,json={'action':'deactivate'}).status_code==422
    assert c.get('/api/mop/users').status_code==200


def test_store_filter_does_not_return_another_store(setup):
    c,_=setup;login(c)
    assert c.get('/api/mop/tasks?store=DEMO-B').json()['rows']==[]


def test_upload_validate_then_confirm_preserves_original_file(setup):
    c,f=setup;h=login(c)
    content=b"date,amount\n2026-01-01,100000\n"
    payload={'source_key':'bank_statement','month':'2026-01','confirm':'false'}
    r=c.post('/api/mop/uploads',headers=h,data=payload,files={'file':('fictional-bank.csv',content,'text/csv')})
    assert r.status_code==200,r.text
    assert r.json()['ok'] is True
    assert c.get('/api/mop/uploads').json()['total']==0
    payload['confirm']='true'
    r=c.post('/api/mop/uploads',headers=h,data=payload,files={'file':('fictional-bank.csv',content,'text/csv')})
    assert r.status_code==200,r.text
    assert r.json()['stored'] is True
    assert c.get('/api/mop/uploads').json()['total']==1
    from mop_reco.db import UploadedFile
    with f() as s:
        stored=s.query(UploadedFile).one()
        assert Path(stored.stored_path).read_bytes()==content
        assert stored.uploaded_by=='Fictional Operator'


def test_master_save_reads_back_and_rejects_stale_version(setup):
    c,f=setup;h=login(c)
    with f() as s:
        masters_db.set_setting(s,masters_db.SETTING_MASTERS_SOURCE,masters_db.SOURCE_DATABASE,'test')
    payload={'rows':[{'store_name':'Fictional Store A','store':'DEMO_A'}],'note':'Fictional master test','expected_version':None}
    r=c.post('/api/mop/masters/store',headers=h,json=payload)
    assert r.status_code==200,r.text
    assert c.get('/api/mop/masters/store').json()['rows'][0]['store']=='DEMO_A'
    assert c.post('/api/mop/masters/store',headers=h,json=payload).status_code==409
