"""Synthetic connected workflow tests; no production files or credentials."""
import json
import pandas as pd
import pytest
from test_api_v2 import setup,login
from mop_reco import manual_match as mm,masters_db as masters,refunds,store_sheet_source as source
from mop_reco.db import R1BillLine,Exception_,ManualMatch,ManualMatchEvent,CmsSlip,CmsPtcDeposit,CmsOverride,TransactionRefund,RefundEvent,Run
from mop_reco.exceptions import bill_exception_key,txn_exception_key

DAY='2026-01-01'
POOL=pd.DataFrame([{'gateway':'paytm','txn_ref':'90000000000000000001','business_date':DAY,'store':'DEMO-A','amount':100000,'status':'SUCCESS','txn_type':'ACQUIRING','payment_mode':'UPI'},{'gateway':'paytm','txn_ref':'90000000000000000002','business_date':DAY,'store':'DEMO-A','amount':60000,'status':'SUCCESS','txn_type':'ACQUIRING','payment_mode':'UPI'},{'gateway':'axis','txn_ref':'90000000000000000003','business_date':DAY,'store':'DEMO-A','amount':40000,'status':'SUCCESS','txn_type':'ACQUIRING','payment_mode':'CARD'},{'gateway':'paytm','txn_ref':'90000000000000000004','business_date':DAY,'store':'DEMO-A','amount':30000,'status':'SUCCESS','txn_type':'ACQUIRING','payment_mode':'UPI'}])
@pytest.fixture
def workflow(setup,monkeypatch):
    c,f=setup
    monkeypatch.setattr(mm,'gateway_pool_for_date',lambda *a,**kw:POOL.copy())
    monkeypatch.setattr(mm,'targeted_pool_many',lambda refs,*a,**kw:POOL[POOL.txn_ref.isin(refs)].copy())
    with f() as s:
        s.add(R1BillLine(run_id=1,business_date=DAY,store='DEMO-A',bill_no='SYN-BILL-1',line_seq=1,mop='PAYTM EDC',mop_class='digital',amount=100000,matched=False,status='Not found in gateway',bill_time='12:30:00'))
        s.add(Exception_(exception_key=bill_exception_key(DAY,'DEMO-A','SYN-BILL-1','PAYTM EDC'),reconciliation='R1',type='bill_not_matched',business_date=DAY,store='DEMO-A',amount=100000,detail=json.dumps({'bill_no':'SYN-BILL-1','mop':'PAYTM EDC'}),status='open',first_seen_run_id=1,last_seen_run_id=1))
        for row in POOL.to_dict('records'):
            s.add(Exception_(exception_key=txn_exception_key(row['gateway'],row['txn_ref']),reconciliation='R1',type='unmapped_gateway_transaction',business_date=DAY,store='DEMO-A',amount=row['amount'],detail=json.dumps({'gateway':row['gateway'],'transaction_id':row['txn_ref'],'payment_mode':row['payment_mode']}),status='open',first_seen_run_id=1,last_seen_run_id=1,transaction_time='12:31:00'))
        s.add(CmsSlip(run_id=1,row_type='COL',dep_date=DAY,cr_date=DAY,store='DEMO-A',amount=15000,adj=0,recovered=0))
        s.add(CmsPtcDeposit(run_id=1,dep_date=DAY,store='DEMO-A',amount=20000))
        masters.set_setting(s,masters.SETTING_MASTERS_SOURCE,masters.SOURCE_DATABASE,'test')
        masters.save_version(s,'store',[{'store_name':'Fictional Store','store':'DEMO_A'}],'test','Initial synthetic version')
        s.commit()
    return c,f

def match_input(**extra):
    return dict(run_id=1,bill_no='SYN-BILL-1',transaction_ref=POOL.iloc[0].txn_ref,reason_code='operator_review',reason='Synthetic evidence',confirmed=True,**extra)

def test_every_new_register_requires_login(workflow):
    c,_=workflow
    for path in ['matching','refunds','cms','runs']:
        assert c.get('/api/mop/'+path).status_code==401

def test_manual_match_preview_apply_readback_reverse_retains_raw(workflow):
    c,f=workflow;h=login(c)
    r=c.post('/api/mop/matching/preview',headers=h,json=match_input());assert r.status_code==200,r.text
    assert r.json()['validation']['ok'] is True
    r=c.post('/api/mop/matching/apply',headers=h,json=match_input());assert r.status_code==200,r.text
    mid=r.json()['id']
    rows=c.get('/api/mop/matching').json()['rows'];assert rows[0]['effective_matched'] is True
    assert rows[0]['match_source']=='manual'
    assert c.post(f'/api/mop/matching/{mid}/reverse',headers=h,json={'confirmed':True,'reason':'Synthetic reversal evidence'}).status_code==200
    with f() as s:
        assert s.query(ManualMatch).one().lifecycle=='reversed'
        assert s.query(ManualMatchEvent).count()==2
        assert s.query(R1BillLine).one().matched is False
    assert c.get('/api/mop/matching').json()['rows'][0]['effective_matched'] is False

def test_apply_revalidates_stale_bill(workflow):
    c,_=workflow;h=login(c)
    assert c.post('/api/mop/matching/preview',headers=h,json=match_input()).status_code==200
    assert c.post('/api/mop/matching/apply',headers=h,json=match_input()).status_code==200
    assert c.post('/api/mop/matching/apply',headers=h,json=match_input()).status_code==422

def test_confirmation_required_before_matching(workflow):
    c,f=workflow;h=login(c);data=match_input();data['confirmed']=False
    assert c.post('/api/mop/matching/apply',headers=h,json=data).status_code==422
    with f() as s:assert s.query(ManualMatch).count()==0

def test_split_retains_both_transaction_ids(workflow):
    c,f=workflow;h=login(c);data=match_input(second_transaction_ref=POOL.iloc[2].txn_ref);data['transaction_ref']=POOL.iloc[1].txn_ref
    r=c.post('/api/mop/matching/apply',headers=h,json=data);assert r.status_code==200,r.text
    with f() as s:
        row=s.query(ManualMatch).one();assert row.transaction_ref==POOL.iloc[1].txn_ref;assert row.transaction_ref_2==POOL.iloc[2].txn_ref

def test_missing_transaction_is_asserted_not_matched(workflow):
    c,_=workflow;h=login(c);data=match_input();data['transaction_ref']='SYN-ABSENT'
    r=c.post('/api/mop/matching/apply',headers=h,json=data);assert r.status_code==200,r.text
    assert r.json()['status']=='asserted_unverified'
    assert c.get('/api/mop/matching').json()['rows'][0]['effective_matched'] is False

def test_cms_override_recomputes_effective_and_retains_computed(workflow):
    c,f=workflow;h=login(c)
    data={'run_id':1,'date_from':DAY,'date_to':DAY,'confirmed':True,'rows':[{'store':'DEMO-A','deposit_date':DAY,'column':'same_day','amount':12000,'reason':'Synthetic bank evidence'}]}
    r=c.post('/api/mop/cms/overrides/preview',headers=h,json=data);assert r.status_code==200,r.text
    assert r.json()['accepted'][0]['computed_amount']==15000
    assert c.post('/api/mop/cms/overrides/apply',headers=h,json=data).status_code==200
    assert c.get('/api/mop/cms').status_code==200
    with f() as s:
        override=s.query(CmsOverride).one();assert float(override.computed_amount)==15000;assert float(override.override_amount)==12000;oid=override.id
    assert c.post(f'/api/mop/cms/overrides/{oid}/revert',headers=h,json={'confirmed':True,'reason':'Synthetic revert'}).status_code==200
    with f() as s:assert s.get(CmsOverride,oid).status=='reverted'

def test_cms_invalid_batch_applies_nothing(workflow):
    c,f=workflow;h=login(c)
    data={'run_id':1,'date_from':DAY,'date_to':DAY,'confirmed':True,'rows':[{'store':'UNKNOWN','deposit_date':DAY,'column':'same_day','amount':12000,'reason':'Synthetic'}]}
    assert c.post('/api/mop/cms/overrides/apply',headers=h,json=data).status_code==422
    with f() as s:assert s.query(CmsOverride).count()==0

def test_refund_create_reverse_preserves_events(workflow):
    c,f=workflow;h=login(c)
    data={'transaction_ref':POOL.iloc[3].txn_ref,'customer_name':'Synthetic Person','zoho_no':'SYN-Z-1','refund_date':DAY,'confirmed':True}
    r=c.post('/api/mop/refunds/preview',headers=h,json=data);assert r.status_code==200,r.text
    assert c.post('/api/mop/refunds/apply',headers=h,json=data).status_code==200
    row=c.get('/api/mop/refunds').json()['rows'][0];assert row['amount']==30000;assert row['transaction_id']==POOL.iloc[3].txn_ref
    assert c.post(f"/api/mop/refunds/{row['id']}/reverse",headers=h,json={'confirmed':True,'reason':'Synthetic reversal'}).status_code==200
    with f() as s:
        assert s.query(TransactionRefund).count()==0;assert s.query(RefundEvent).count()==2

def test_matching_upload_reports_partial_semantics(workflow):
    c,f=workflow;h=login(c)
    data=b'bill_no,business_date,store,transaction_id,remark\nSYN-BILL-1,2026-01-01,DEMO-A,90000000000000000001,Synthetic evidence\nUNKNOWN,2026-01-01,DEMO-A,SYN-ABSENT,Rejected\n'
    r=c.post('/api/mop/bulk/matching',headers=h,data={'run_id':'1','confirm':'false'},files={'file':('synthetic.csv',data,'text/csv')});assert r.status_code==200,r.text
    assert r.json()['accepted']==1;assert r.json()['rejected']==1
    r=c.post('/api/mop/bulk/matching',headers=h,data={'run_id':'1','confirm':'true'},files={'file':('synthetic.csv',data,'text/csv')});assert r.status_code==200,r.text
    assert r.json()['applied_count']==1;assert r.json()['failed_count']==0;assert r.json()['batch_id'] is not None

def test_bulk_replies_validate_then_readback(workflow):
    c,_=workflow;h=login(c)
    content=b'task_id,store,reply,replied_by\n1,DEMO-A,Synthetic store evidence,Synthetic responder\n'
    r=c.post('/api/mop/bulk/replies',headers=h,data={'run_id':'1','confirm':'true'},files={'file':('replies.csv',content,'text/csv')});assert r.status_code==200,r.text
    assert r.json()['applied_count']==1
    assert c.get('/api/mop/tasks').json()['rows'][0]['Store reply']=='Synthetic store evidence'

def test_master_restore_rejects_stale_and_preserves_versions(workflow):
    c,f=workflow;h=login(c)
    before=c.get('/api/mop/masters/store').json()['version']
    edit={'rows':[{'store_name':'Synthetic Changed Store','store':'DEMO_A'}],'note':'Synthetic correction','expected_version':before}
    r=c.post('/api/mop/masters/store',headers=h,json=edit);assert r.status_code==200
    current=r.json()['version']
    restore={'version_id':before,'expected_version':before,'reason':'Restore synthetic original','confirmed':True}
    assert c.post('/api/mop/masters/store/restore',headers=h,json=restore).status_code==409
    restore['expected_version']=current
    r=c.post('/api/mop/masters/store/restore',headers=h,json=restore);assert r.status_code==200,r.text
    assert c.get('/api/mop/masters/store').json()['rows'][0]['store_name']=='Fictional Store'
    assert len(c.get('/api/mop/masters/store').json()['history'])==3

def test_snapshot_incomplete_is_blocked_before_writes(workflow):
    c,_=workflow;h=login(c)
    r=c.post('/api/mop/masters-import/apply',headers=h,json={'snapshot':{'masters':{}},'confirmed':True,'reason':'Synthetic import'})
    assert r.status_code==422

def test_bulk_accept_preview_rejects_stale_fingerprint(workflow):
    c,_=workflow;h=login(c)
    r=c.post('/api/mop/bulk-accept/preview',headers=h,json={'run_id':1,'kind':'exact'});assert r.status_code==200,r.text
    assert r.json()['total']==1
    r=c.post('/api/mop/bulk-accept/apply',headers=h,json={'run_id':1,'kind':'exact','confirmed':True,'fingerprint':'stale'});assert r.status_code==409

def test_specialized_workbook_has_five_audited_tabs(workflow,monkeypatch):
    from mop_reco.store_sheet_export import build_workbook
    from mop_reco import store_sheet as ss
    from openpyxl import load_workbook
    from io import BytesIO
    from types import SimpleNamespace
    c,f=workflow;login(c)
    with f() as s:frame=source.load_fast(s,1,__import__('datetime').date(2026,1,1),__import__('datetime').date(2026,1,1))[0]
    monkeypatch.setattr(source,'build',lambda *a,**kw:SimpleNamespace(sheet=frame,excluded=pd.DataFrame(),contribution=pd.DataFrame(),other_types=pd.DataFrame(),checks=None))
    r=c.get('/api/mop/store-sheet/export?store=DEMO-A');assert r.status_code==200,r.text
    book=load_workbook(BytesIO(r.content));assert len(book.sheetnames)==5

def test_shared_payment_records_two_bills_and_reverses_together(workflow):
    c,f=workflow;h=login(c)
    with f() as s:
        s.query(R1BillLine).one().amount=60000
        s.add(R1BillLine(run_id=1,business_date=DAY,store='DEMO-A',bill_no='SYN-BILL-2',line_seq=1,mop='PAYTM EDC',mop_class='digital',amount=40000,matched=False,status='Not found in gateway'));s.commit()
    r=c.post('/api/mop/matching/apply',headers=h,json=match_input(shared_bill_nos=['SYN-BILL-2']));assert r.status_code==200,r.text
    assert len(r.json()['ids'])==2
    with f() as s:assert len(set(x.shared_group for x in s.query(ManualMatch)))==1
    assert c.post(f"/api/mop/matching/{r.json()['ids'][0]}/reverse",headers=h,json={'confirmed':True,'reason':'Synthetic shared reversal'}).status_code==200
    with f() as s:assert all(x.lifecycle=='reversed' for x in s.query(ManualMatch))

def test_cms_history_includes_working_revert_identifier(workflow):
    c,_=workflow;h=login(c)
    data={'run_id':1,'date_from':DAY,'date_to':DAY,'confirmed':True,'rows':[{'store':'DEMO-A','deposit_date':DAY,'column':'same_day','amount':12000,'reason':'Synthetic bank evidence'}]}
    assert c.post('/api/mop/cms/overrides/apply',headers=h,json=data).status_code==200
    row=c.get('/api/mop/cms').json()['overrides'][0];assert isinstance(row['id'],int)
    assert c.post(f"/api/mop/cms/overrides/{row['id']}/revert",headers=h,json={'confirmed':True,'reason':'Synthetic revert'}).status_code==200

@pytest.mark.parametrize('value',['Infinity','-Infinity','NaN'])
def test_cms_nonfinite_amount_never_saves(workflow,value):
    c,f=workflow;h=login(c)
    data={'run_id':1,'date_from':DAY,'date_to':DAY,'confirmed':True,'rows':[{'store':'DEMO-A','deposit_date':DAY,'column':'same_day','amount':value,'reason':'Synthetic bank evidence'}]}
    assert c.post('/api/mop/cms/overrides/apply',headers=h,json=data).status_code==422
    with f() as s:assert s.query(CmsOverride).count()==0

def test_cms_remark_preserves_history(workflow):
    c,_=workflow;h=login(c)
    assert c.post('/api/mop/cms/remarks',headers=h,json={'store':'DEMO-A','reason':'Synthetic CMS review'}).status_code==200
    assert c.get('/api/mop/cms/remarks').json()['rows'][0]['remark']=='Synthetic CMS review'
    assert c.get('/api/mop/cms/remarks/history?store=DEMO-A').json()[0]['Remark']=='Synthetic CMS review'

def synthetic_snapshot():
    base={'store':[{'store_name':'Synthetic Store','store':'DEMO_A'}],'mid_axis':[{'mid':'SYN_A','store':'DEMO_A'}],'mid_paytm':[{'mid':'SYN_P','store':'DEMO_A'}],'pickup_code':[{'pickup_code':'SYN_PC','store':'DEMO_A'}],'pickup_location':[{'pickup_code':'SYN_PC','location':'Synthetic Location','store':'DEMO_A'}],'opening_balance':[{'cost_centre':'Synthetic Store','opening':100000}]}
    output={}
    for name,rows in base.items():
        fingerprint=masters._fingerprint([masters._clean_row(name,r) for r in rows])
        output[name]={'versions':[{'loaded_at':'2026-01-01T00:00:00','source_mtime':None,'source':'synthetic.xlsx','rows':rows,'fingerprint':fingerprint,'meta':{'as_at':'2026-01-01','grand_total':100000} if name=='opening_balance' else None}],'fingerprint':fingerprint,'row_count':len(rows)}
    return {'created_at':'2026-01-01T00:00:00','masters':output,'report':[]}

def test_full_snapshot_import_retains_dates_and_switches_source(workflow):
    c,f=workflow;h=login(c);data={'snapshot':synthetic_snapshot(),'reason':'Synthetic migration','confirmed':True}
    r=c.post('/api/mop/masters-import/preview',headers=h,json=data);assert r.status_code==200,r.text
    assert len(r.json()['rows'])==6
    data['expected_versions']=r.json()['current_versions']
    r=c.post('/api/mop/masters-import/apply',headers=h,json=data);assert r.status_code==200,r.text
    with f() as s:
        assert masters.in_database(s)
        assert masters.current_version(s,'opening_balance').loaded_at.isoformat()=='2026-01-01T00:00:00'
        assert masters.current_rows(s,'mid_axis')[0]['mid']=='SYN_A'

def test_bad_snapshot_date_blocks_every_master(workflow):
    c,f=workflow;h=login(c);snapshot=synthetic_snapshot();snapshot['masters']['mid_axis']['versions'][0]['loaded_at']='not-a-date'
    assert c.post('/api/mop/masters-import/preview',headers=h,json={'snapshot':snapshot,'reason':'Synthetic migration'}).status_code==422
    with f() as s:assert masters.current_version(s,'mid_axis') is None

def test_parked_axis_is_not_an_exception(workflow):
    from mop_reco.db import ParkedBillLine
    c,f=workflow;login(c)
    with f() as s:
        before=s.query(Exception_).count();s.add(ParkedBillLine(run_id=1,business_date=DAY,store='DEMO-A',bill_no='SYN-PARKED',mop='AXIS',amount=25000,awaiting_source='axis_edc'));s.commit()
    assert c.get('/api/mop/parked').json()['rows'][0]['amount']==25000
    with f() as s:assert s.query(Exception_).count()==before

def test_mark_failed_requires_stale_and_operator_reason(workflow):
    from datetime import datetime,timedelta
    c,f=workflow;h=login(c)
    with f() as s:s.add(Run(id=2,started_at=datetime.now(),business_dates='[]',status='running',heartbeat_at=datetime.now()));s.commit()
    action={'confirmed':True,'reason':'Verified synthetic worker is stopped'}
    assert c.post('/api/mop/runs/2/mark-failed',headers=h,json=action).status_code==409
    with f() as s:s.get(Run,2).heartbeat_at=datetime.now()-timedelta(hours=1);s.commit()
    assert c.post('/api/mop/runs/2/mark-failed',headers=h,json=action).status_code==200
    with f() as s:
        row=s.get(Run,2);assert row.status=='failed';assert 'Fictional Operator' in row.notes;assert row.finished_at is not None

def test_run_start_checks_confirmation_then_calls_original_engine(workflow,monkeypatch):
    import api_v2_workflows as adapter
    from types import SimpleNamespace
    from datetime import datetime
    c,f=workflow;h=login(c)
    monkeypatch.setattr(adapter.coverage,'all_source_dates',lambda cfg:{})
    monkeypatch.setattr(adapter.coverage,'range_problems',lambda *a:[])
    monkeypatch.setattr(adapter.coverage,'axis_parking_note',lambda *a:None)
    monkeypatch.setattr(adapter.master_files,'preflight_masters',lambda *a,**kw:[])
    recorded=[]
    def run(cfg,session,lo,hi,**kw):
        recorded.append((lo.isoformat(),hi.isoformat(),kw['triggered_by']));session.add(Run(id=2,started_at=datetime.now(),finished_at=datetime.now(),status='completed',business_dates=json.dumps([DAY])));session.commit()
    monkeypatch.setattr(adapter.runs,'run_reconciliation',run)
    payload={'date_from':DAY,'date_to':DAY}
    assert c.post('/api/mop/runs/start',headers=h,json=payload).status_code==422
    payload['confirmed']=True
    r=c.post('/api/mop/runs/start',headers=h,json=payload);assert r.status_code==200,r.text
    import time
    for _ in range(50):
        with f() as s:finished=s.get(Run,2)
        if finished is not None:break
        time.sleep(.02)
    assert recorded==[(DAY,DAY,'Fictional Operator')]
    assert c.get('/api/mop/runs').json()['rows'][0]['status']=='completed'

def test_matching_paginates_before_overlay(workflow):
    c,f=workflow;login(c)
    with f() as s:
        for n in range(1,122):s.add(R1BillLine(run_id=1,business_date=DAY,store='DEMO-A',bill_no=f'SYN-PAGE-{n}',mop='PAYTM',amount=n,matched=False,status='Not found in gateway'))
        s.commit()
    r=c.get('/api/mop/matching?offset=50&limit=20');assert r.status_code==200
    assert r.json()['total']==122;assert len(r.json()['rows'])==20
    assert c.get('/api/mop/matching?search=SYN-PAGE-121').json()['total']==1

def test_upload_withdraw_archives_original_and_keeps_history(workflow):
    from mop_reco.db import UploadedFile
    from datetime import datetime
    from pathlib import Path
    c,f=workflow;h=login(c)
    with f() as s:
        folder=Path(s.bind.url.database).parent/'source';folder.mkdir();original=folder/'fictional.csv';original.write_bytes(b'synthetic original bytes')
        s.add(UploadedFile(id=1,uploaded_at=datetime.now(),source_key='axis_edc',report_name='Fictional Axis',original_file_name='fictional.csv',stored_path=str(original),size_bytes=24,uploaded_by='synthetic',status='stored'));s.commit()
    r=c.post('/api/mop/uploads/1/withdraw',headers=h,json={'confirmed':True,'reason':'Synthetic withdrawal'});assert r.status_code==200,r.text
    with f() as s:
        row=s.get(UploadedFile,1);assert row.status=='deleted';assert not original.exists();assert Path(row.archived_path).read_bytes()==b'synthetic original bytes';assert row.deleted_by=='Fictional Operator'
    assert c.get('/api/mop/uploads').json()['rows'][0]['status']=='deleted'

def test_upload_withdraw_refused_while_run_active(workflow):
    from datetime import datetime
    c,f=workflow;h=login(c)
    with f() as s:s.add(Run(id=2,started_at=datetime.now(),business_dates='[]',status='running'));s.commit()
    assert c.post('/api/mop/uploads/1/withdraw',headers=h,json={'confirmed':True,'reason':'Synthetic withdrawal'}).status_code==409

def test_database_snapshot_never_reads_excel(workflow,monkeypatch):
    c,f=workflow;h=login(c);data={'snapshot':synthetic_snapshot(),'reason':'Synthetic migration','confirmed':True}
    data['expected_versions']=c.post('/api/mop/masters-import/preview',headers=h,json=data).json()['current_versions']
    assert c.post('/api/mop/masters-import/apply',headers=h,json=data).status_code==200
    monkeypatch.setattr(masters,'build_snapshot',lambda *a,**kw:pytest.fail('Migrated masters must not read Excel.'))
    r=c.get('/api/mop/masters-import/snapshot');assert r.status_code==200,r.text
    assert r.json()['masters']['opening_balance']['versions'][0]['loaded_at'].startswith('2026-01-01')

def test_empty_cms_run_reports_missing_sources_not_server_crash(workflow):
    c,f=workflow;login(c)
    with f() as s:s.query(CmsSlip).delete();s.query(CmsPtcDeposit).delete();s.commit()
    r=c.get('/api/mop/cms');assert r.status_code==409;assert 'No CMS source dates' in r.json()['detail']
