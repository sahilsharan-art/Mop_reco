"""Authenticated V2 workflow adapters. All financial decisions stay in mop_reco."""
from datetime import date, datetime
from io import BytesIO
import hashlib
import json
import logging
import threading
import pandas as pd
from fastapi import Depends, HTTPException, UploadFile, File, Form, Response, Query
from pydantic import BaseModel, Field
from typing import Literal
from mop_reco import manual_match as mm, refunds, cms_blocks as cms, masters_db as masters
from mop_reco import queries as queries, runs, run_coverage as coverage, masters as master_files
from mop_reco.db import ManualMatch, ManualMatchEvent, Run, AppSetting, CmsOverride

class Confirm(BaseModel):
    confirmed: bool = False
    reason: str = Field(min_length=1, max_length=2000)

class MatchInput(Confirm):
    run_id: int
    bill_no: str = Field(min_length=1,max_length=200)
    transaction_ref: str = Field(min_length=1,max_length=200)
    second_transaction_ref: str = ''
    shared_bill_nos: list[str] = Field(default_factory=list,max_length=100)
    reason_code: str = Field(min_length=1,max_length=200)
    override_reason: str = ''

class RefundInput(BaseModel):
    transaction_ref: str = Field(min_length=1,max_length=200)
    customer_name: str = Field(min_length=1,max_length=200)
    zoho_no: str = Field(min_length=1,max_length=200)
    refund_date: date
    bill_no: str = ''
    refund_amount: float | None = Field(default=None,gt=0,allow_inf_nan=False)
    confirmed: bool = False

class AcceptInput(BaseModel):
    run_id: int
    kind: Literal['exact','split']
    confirmed: bool = False
    fingerprint: str = ''

class CmsInput(BaseModel):
    run_id: int
    date_from: date
    date_to: date
    rows: list[dict] = Field(min_length=1,max_length=10000)
    confirmed: bool = False

class Restore(Confirm):
    version_id: int
    expected_version: int | None = None

class RunInput(BaseModel):
    date_from: date
    date_to: date
    confirmed: bool = False
    allow_incomplete_range: bool = False
    use_stored_masters: dict[str,str] = Field(default_factory=dict)

class SnapshotInput(Confirm):
    snapshot: dict
    expected_versions: dict[str,int | None] = Field(default_factory=dict)

LOG=logging.getLogger(__name__)

def confirmation(data):
    if not data.confirmed:
        raise HTTPException(422,'Review and confirm this action first.')
    if hasattr(data,'reason') and not data.reason.strip():
        raise HTTPException(422,'A reason is required.')

def workbook(frame):
    out=BytesIO()
    with pd.ExcelWriter(out,engine='xlsxwriter',engine_kwargs={'options':{'strings_to_formulas':False,'strings_to_urls':False}}) as writer:
        frame.to_excel(writer,index=False)
    return out.getvalue()

def register(app, *, db, signed_in, cfg, factory, clean, prefix, period, sheet):
    run_lock=threading.Lock()
    def complete(s,run_id):
        run=s.get(Run,run_id)
        if not run or run.status!='completed':raise HTTPException(409,'Choose a completed run.')
        return run
    def pool():
        # Only explicit workflow requests load normalized transaction data, never dashboard reads.
        return mm.gateway_pool_for_date()
    def scoped(frame,p):
        if frame.empty:return frame
        if 'business_date' in frame:
            frame=frame[(frame['business_date'].astype(str)>=str(p[1]))&(frame['business_date'].astype(str)<=str(p[2]))]
        if p[3] and 'store' in frame:frame=frame[frame['store']==p[3]]
        return frame
    def match_validation(s,data):
        complete(s,data.run_id)
        def open_bill(number):
            found,is_open,why=mm.find_bill_for_match(s,data.run_id,number)
            if not found or not is_open:raise HTTPException(422,why or 'Bill is not open.')
            return found
        bill=open_bill(data.bill_no)
        loaded=mm.targeted_pool_many([x for x in [data.transaction_ref,data.second_transaction_ref] if x])
        bills=[bill]
        if data.second_transaction_ref and data.shared_bill_nos:
            raise HTTPException(422,'Use either a split payment or a shared payment, not both.')
        if data.second_transaction_ref:
            hits=[mm.identify_gateway(s,bill['business_date'],ref,loaded) for ref in [data.transaction_ref,data.second_transaction_ref]]
            if any(not h.found or h.problem for h in hits):raise HTTPException(422,'Both split transactions must be found unambiguously.')
            legs=[(h.gateway,ref) for h,ref in zip(hits,[data.transaction_ref,data.second_transaction_ref])]
            result=mm.validate_split(s,bill,legs,loaded)
        elif data.shared_bill_nos:
            if data.bill_no in data.shared_bill_nos or len(set(data.shared_bill_nos))!=len(data.shared_bill_nos):raise HTTPException(422,'Shared bills must be distinct.')
            bills += [open_bill(b) for b in data.shared_bill_nos]
            hit=mm.identify_gateway(s,bill['business_date'],data.transaction_ref,loaded)
            if not hit.found or hit.problem:raise HTTPException(422,'The shared transaction must be found unambiguously.')
            result=mm.validate_shared(s,bills,hit.gateway,data.transaction_ref,loaded)
            result.gateway=hit.gateway
        else:result=mm.validate_by_id(s,bill,data.transaction_ref,loaded)
        return bills,result
    @app.get(prefix+'/parked')
    def parked(p=Depends(period),u=Depends(signed_in),s=Depends(db),offset:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=500)):
        from mop_reco.db import ParkedBillLine
        rows=s.query(ParkedBillLine).filter(ParkedBillLine.run_id==p[0].id,ParkedBillLine.business_date>=str(p[1]),ParkedBillLine.business_date<=str(p[2]))
        if p[3]:rows=rows.filter(ParkedBillLine.store==p[3])
        total=rows.count();records=[{k:getattr(r,k) for k in ['id','business_date','store','bill_no','mop','amount','axis_ref','awaiting_source','reason']} for r in rows.order_by(ParkedBillLine.id).offset(offset).limit(limit).all()]
        return clean({'rows':records,'total':total})
    @app.get(prefix+'/matching')
    def matching(p=Depends(period),u=Depends(signed_in),s=Depends(db),search:str=Query(default='',max_length=200),offset:int=Query(default=0,ge=0),limit:int=Query(default=50,ge=1,le=500),register:Literal['bills','manual']='bills'):
        from mop_reco.db import R1BillLine as BillLine
        model=BillLine if register=='bills' else ManualMatch
        rows=s.query(model).filter(model.business_date>=str(p[1]),model.business_date<=str(p[2]))
        if register=='bills':rows=rows.filter(model.run_id==p[0].id)
        if p[3]:rows=rows.filter(model.store==p[3])
        if search:
            from sqlalchemy import or_
            rows=rows.filter(or_(model.bill_no.contains(search,autoescape=True),model.store.contains(search,autoescape=True)))
        total=rows.count();page=rows.order_by(model.id.desc()).offset(offset).limit(limit).all()
        if register=='bills':
            records=[{k:getattr(r,k,None) for k in ['id','business_date','store','bill_no','mop','amount','matched','status','matched_txn_ref']} for r in page]
            frame=mm.apply_overlay(s,pd.DataFrame(records)) if records else pd.DataFrame()
            history=[]
        else:
            frame=pd.DataFrame()
            history=[{k:getattr(r,k) for k in ['id','bill_no','business_date','store','amount','gateway','transaction_ref','status','lifecycle','operator','batch_id']} for r in page]
        return {'rows':clean(frame),'total':total,'matches':clean(history),'offset':offset,'limit':limit}
    @app.post(prefix+'/matching/preview')
    def match_preview(data:MatchInput,u=Depends(signed_in),s=Depends(db)):
        bills,result=match_validation(s,data)
        return clean({'bills':bills,'validation':result,'needs_override':result.needs_override})
    @app.post(prefix+'/matching/apply')
    def match_apply(data:MatchInput,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);bills,result=match_validation(s,data)
        if len(bills)>1:
            entries=mm.record_shared(s,bills,result.gateway,data.transaction_ref,data.reason_code,u.display_name,result,source=data.reason)
            return {'ok':True,'ids':[x.id for x in entries]}
        second={'gateway':result.gateway2,'txn_ref':data.second_transaction_ref,'amount':result.txn2['amount']} if data.second_transaction_ref and result.txn2 else None
        entry=mm.record(s,bills[0],result.gateway,data.transaction_ref,data.reason_code,u.display_name,result,note=data.reason,override_reason=data.override_reason or None,second=second)
        return {'ok':True,'id':entry.id,'status':entry.status}
    @app.get(prefix+'/matching/{match_id}/history')
    def match_history(match_id:int,u=Depends(signed_in),s=Depends(db)):
        return clean([{'event':r.event,'at':r.at,'operator':r.operator,'detail':r.detail} for r in s.query(ManualMatchEvent).filter_by(manual_match_id=match_id).order_by(ManualMatchEvent.id).all()])
    @app.post(prefix+'/matching/{match_id}/reverse')
    def reverse_match(match_id:int,data:Confirm,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);entry=mm.reverse(s,match_id,u.display_name,data.reason)
        return {'id':entry.id,'lifecycle':entry.lifecycle}
    @app.post(prefix+'/matching/batches/{batch_id}/reverse')
    def reverse_batch(batch_id:int,data:Confirm,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);return {'reversed':mm.reverse_batch(s,batch_id,u.display_name,data.reason)}
    def refund_transaction(s,data):
        loaded=mm.targeted_pool_many([data.transaction_ref])
        hit=mm.identify_gateway(s,str(data.refund_date),data.transaction_ref,loaded)
        if not hit.found or hit.problem:raise HTTPException(422,hit.problem or 'Transaction not found in loaded data.')
        # Guard core bill-refund helper's first-hit assumption across gateways.
        ref=mm._clean_ref(data.transaction_ref)
        hits=loaded[loaded['txn_ref'].astype(str)==ref]
        if len(hits)!=1:raise HTTPException(422,'Transaction reference has multiple rows; resolve it before refunding.')
        txn=dict(hit.txn,gateway=hit.gateway,txn_ref=ref)
        problems=refunds.validate_refund(s,txn,data.customer_name,data.zoho_no,str(data.refund_date))
        if data.bill_no:
            if data.refund_amount is None:raise HTTPException(422,'Enter the refund portion for a bill + refund split.')
            problem=refunds.split_problem(s,data.bill_no,hit.gateway,ref,data.refund_amount,float(txn['amount']),txn.get('store'))
            if problem:problems.append(problem)
        elif data.refund_amount is not None:raise HTTPException(422,'For a whole transaction refund leave the split amount blank.')
        if problems:raise HTTPException(422,'; '.join(problems))
        return txn
    @app.post(prefix+'/refunds/preview')
    def refund_preview(data:RefundInput,u=Depends(signed_in),s=Depends(db)):
        return clean({'transaction':refund_transaction(s,data)})
    @app.post(prefix+'/refunds/apply')
    def refund_apply(data:RefundInput,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);txn=refund_transaction(s,data)
        if data.bill_no:refunds.record_refund_with_bill(s,data.bill_no,txn['gateway'],txn['txn_ref'],data.refund_amount,data.customer_name,data.zoho_no,str(data.refund_date),u.display_name)
        else:refunds.record_refund(s,txn,data.customer_name,data.zoho_no,str(data.refund_date),u.display_name)
        return {'ok':True}
    def acceptance(s,data):
        from mop_reco import exception_pairing as pairing,bulk_accept
        complete(s,data.run_id)
        loaded=pairing.unmapped_pool(s);bills=pairing.open_population(s,data.run_id)
        minutes,days=pairing.window_settings(cfg().raw)
        assignment=pairing.assign(bills,loaded,minutes,days,pairing.mop_family(cfg().raw))
        population=bulk_accept.build(bills,loaded,assignment)
        rows=population.exact if data.kind=='exact' else population.split
        shown=clean(bulk_accept.preview_frame(rows))
        fingerprint=hashlib.sha256(json.dumps(shown,sort_keys=True).encode()).hexdigest()
        return rows,shown,fingerprint
    @app.post(prefix+'/bulk-accept/preview')
    def accept_preview(data:AcceptInput,u=Depends(signed_in),s=Depends(db)):
        rows,shown,fingerprint=acceptance(s,data)
        return {'rows':shown,'fingerprint':fingerprint,'total':len(rows)}
    @app.post(prefix+'/bulk-accept/apply')
    def accept_apply(data:AcceptInput,u=Depends(signed_in),s=Depends(db)):
        from mop_reco import bulk_accept
        confirmation(data);rows,shown,fingerprint=acceptance(s,data)
        if fingerprint!=data.fingerprint:raise HTTPException(409,'Candidates changed. Review the new population before applying.')
        if not rows:raise HTTPException(422,'No eligible candidates.')
        return clean(bulk_accept.apply(s,data.run_id,rows,u.display_name,data.kind))
    @app.get(prefix+'/refunds')
    def refund_rows(p=Depends(period),u=Depends(signed_in),s=Depends(db)):
        return {'rows':clean(refunds.refund_table(s,p[1],p[2],p[3])),'history':clean(refunds.refund_register(s))}
    @app.post(prefix+'/refunds/{refund_id}/reverse')
    def reverse_refund(refund_id:int,data:Confirm,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);refunds.reverse_refund(s,refund_id,u.display_name,data.reason);return {'ok':True}
    @app.get(prefix+'/bulk/{kind}/template')
    def template(kind:Literal['matching','refunds','replies'],u=Depends(signed_in)):
        from mop_reco import store_tasks
        frame={'matching':mm.upload_template,'refunds':refunds.upload_template,'replies':store_tasks.upload_template}[kind]()
        # Templates contain column names only, not legacy sample people or identifiers.
        return Response(workbook(frame.iloc[0:0]),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="{kind}-template.xlsx"'})
    @app.post(prefix+'/bulk/{kind}')
    async def bulk(kind:Literal['matching','refunds','replies','refund_folder'],file:UploadFile=File(...),run_id:int=Form(...),confirm:bool=Form(False),include_warnings:bool=Form(False),override_reason:str=Form(''),u=Depends(signed_in),s=Depends(db)):
        from mop_reco import store_tasks
        complete(s,run_id)
        content=await file.read(20*1024*1024+1);await file.close()
        if len(content)>20*1024*1024:raise HTTPException(413,'Spreadsheet exceeds 20 MB.')
        if str(file.filename).lower().endswith('.csv'):frame=pd.read_csv(BytesIO(content),dtype=str,keep_default_na=False)
        elif str(file.filename).lower().endswith('.xlsx'):frame=pd.read_excel(BytesIO(content),dtype=str,keep_default_na=False)
        else:raise HTTPException(422,'Choose an XLSX or CSV file.')
        if kind=='matching':preview=mm.preview_upload(s,frame,pool(),run_id);shown=preview.as_frame();good=len(preview.accepted);bad=len(preview.rejected);warn=len(preview.warned)
        elif kind=='refunds':preview=refunds.preview_upload(s,frame);shown=preview.as_frame();good=len(preview.accepted);bad=len(preview.rejected);warn=0
        elif kind=='refund_folder':preview=refunds.preview_folder(s,frame);shown=preview;good=int((preview['verdict']=='apply').sum());bad=len(preview)-good;warn=0
        else:preview=store_tasks.preview_replies(s,frame);shown=preview.as_frame();good=len(preview.accepted);bad=len(preview.rejected);warn=0
        result={'rows':clean(shown),'accepted':good,'rejected':bad,'warned':warn,'applied':False}
        if confirm:
            if not good and not (kind=='matching' and include_warnings and warn):raise HTTPException(422,'No eligible rows to apply.')
            if kind=='matching':batch,entries,failed=mm.apply_upload(s,preview,u.display_name,include_warnings,override_reason or None,file.filename);result.update(applied=True,applied_count=len(entries),failed_count=len(failed),batch_id=batch.id)
            elif kind=='refunds':entries,rejected=refunds.apply_upload(s,preview,u.display_name,file.filename);result.update(applied=True,applied_count=len(entries))
            elif kind=='refund_folder':result.update(applied=True,applied_count=refunds.apply_folder(s,preview,u.display_name))
            else:result.update(applied=True,applied_count=store_tasks.apply_replies(s,preview,u.display_name))
        return clean(result)
    def cms_model(s,run_id):
        complete(s,run_id);slips,deposits=cms.load(s,run_id);first,last=cms.run_range(s,run_id)
        if not first or not last or (slips.empty and deposits.empty):raise HTTPException(409,'No CMS source dates are available for this run. Upload the required source and reconcile first.')
        return cms.build_model(slips,deposits,first,last,cms.opening_from_config(cfg().raw))
    @app.get(prefix+'/cms')
    def cms_rows(p=Depends(period),u=Depends(signed_in),s=Depends(db)):
        model=cms_model(s,p[0].id);effective=cms.apply_overrides(model,cms.active_overrides(s))
        blocks=cms.blocks_frame(effective,str(p[1]),str(p[2]));history=cms.all_overrides(s)
        history.insert(0,'id',[r.id for r in s.query(CmsOverride).order_by(CmsOverride.id).all()])
        if p[3]:
            for frame in [blocks,history]:
                if not frame.empty and 'store' in frame:frame.drop(frame[frame['store']!=p[3]].index,inplace=True)
        return clean({'rows':blocks,'overrides':history,'controls':cms.block_controls(effective,str(p[1]),str(p[2]))})
    @app.get(prefix+'/cms/remarks')
    def cms_remarks(p=Depends(period),u=Depends(signed_in),s=Depends(db)):
        rows=[{'store':store,'remark':vals[0],'remark_by':vals[1],'remark_at':vals[2]} for store,vals in cms.remarks_for(s,str(p[1]),str(p[2])).items() if not p[3] or p[3]==store]
        return clean({'rows':rows})
    @app.get(prefix+'/cms/remarks/history')
    def cms_remark_history(store:str,p=Depends(period),u=Depends(signed_in),s=Depends(db)):
        return clean(cms.remark_history(s,store,str(p[1]),str(p[2])))
    @app.post(prefix+'/cms/remarks')
    def cms_remark(data:dict,p=Depends(period),u=Depends(signed_in),s=Depends(db)):
        store=str(data.get('store','')).strip()
        if store not in cms_model(s,p[0].id).stores:raise HTTPException(422,'Choose a store present in this run.')
        remark=str(data.get('reason','')).strip()
        if not remark or len(remark)>2000:raise HTTPException(422,'Enter a remark of 1–2000 characters.')
        cms.set_remark(s,store,str(p[1]),str(p[2]),remark,u.display_name);return {'ok':True}
    @app.post(prefix+'/cms/overrides/preview')
    def cms_preview(data:CmsInput,u=Depends(signed_in),s=Depends(db)):
        if data.date_from>data.date_to:raise HTTPException(422,'Start date must precede end date.')
        import math
        for row in data.rows:
            try:value=float(str(row.get('amount','')).replace(',',''))
            except (TypeError,ValueError):raise HTTPException(422,'Every override amount must be a finite number.')
            if not math.isfinite(value):raise HTTPException(422,'Every override amount must be a finite number.')
        accepted,rejected=cms.validate_overrides(cms_model(s,data.run_id),data.rows,str(data.date_from),str(data.date_to))
        return clean({'accepted':accepted,'rejected':rejected})
    @app.post(prefix+'/cms/overrides/apply')
    def cms_apply(data:CmsInput,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);preview=cms_preview(data,u,s)
        if preview['rejected']:raise HTTPException(422,'Fix every rejected row. No overrides applied.')
        cms.record_overrides(s,preview['accepted'],u.display_name);return {'ok':True,'applied_count':len(preview['accepted'])}
    @app.post(prefix+'/cms/overrides/{override_id}/revert')
    def cms_revert(override_id:int,data:Confirm,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);cms.revert_override(s,override_id,u.display_name);return {'ok':True}
    @app.post(prefix+'/uploads/{upload_id}/withdraw')
    def withdraw(upload_id:int,data:Confirm,u=Depends(signed_in),s=Depends(db)):
        from mop_reco import uploads
        confirmation(data)
        active=run_lock.locked() or s.query(Run.id).filter_by(status='running').first() is not None
        ok,message=uploads.delete_upload(s,upload_id,u.display_name,active,cfg())
        if not ok:raise HTTPException(409,message)
        return {'ok':True,'message':'Upload withdrawn; history retained and original file archived when present.'}
    @app.get(prefix+'/masters-import/snapshot')
    def migration_snapshot(u=Depends(signed_in),s=Depends(db)):
        # The core supplies the snapshot and all original version dates/fingerprints.
        if masters.in_database(s):
            entries={}
            for name in masters.MASTER_FIELDS:
                versions=masters._stored_versions(s,name)
                entries[name]={'versions':versions,'row_count':len(versions[-1]['rows']) if versions else 0,'fingerprint':versions[-1]['fingerprint'] if versions else masters._fingerprint([])}
            snapshot={'created_at':datetime.now().isoformat(),'masters':entries,'report':['Database snapshot; no workbook has been read.']}
        else:snapshot=masters.build_snapshot(cfg(),s)
        check_snapshot(snapshot)
        return Response(json.dumps(clean(snapshot),allow_nan=False),media_type='application/json',headers={'Content-Disposition':'attachment; filename="master-migration-snapshot.json"'})
    @app.post(prefix+'/masters/{name}/restore')
    def restore(name:str,data:Restore,u=Depends(signed_in),s=Depends(db)):
        confirmation(data)
        if name not in masters.MASTER_FIELDS:raise HTTPException(404,'Unknown master.')
        if not masters.in_database(s):raise HTTPException(409,'Complete the master migration first.')
        s.query(AppSetting).filter(AppSetting.key==masters.SETTING_MASTERS_SOURCE).with_for_update().first()
        current=masters.current_version(s,name)
        if (current.id if current else None)!=data.expected_version:raise HTTPException(409,'Master changed. Reload before restoring.')
        version,created,diff=masters.restore_version(s,name,data.version_id,u.display_name,data.reason)
        return clean({'version':version.id,'created':created,'diff':diff})
    def _check_snapshot(snapshot):
        if set(snapshot.get('masters',{}))!=set(masters.MASTER_FIELDS):raise HTTPException(422,'Snapshot must include all six masters.')
        for name,entry in snapshot['masters'].items():
            versions=entry.get('versions')
            if not versions:raise HTTPException(422,f'{name}: snapshot has no versions.')
            for v in versions:
                datetime.fromisoformat(v['loaded_at'])
                if v.get('source_mtime'):datetime.fromisoformat(v['source_mtime'])
                if not isinstance(v['rows'],list):raise HTTPException(422,f'{name}: rows must be a list.')
                rows=[masters._clean_row(name,r) for r in v['rows']]
                if name=='opening_balance':
                    import math
                    for row in rows:
                        if not math.isfinite(float(row['opening'])):raise HTTPException(422,'Opening balance amounts must be finite.')
                    meta=v.get('meta') or {}
                    if not meta.get('as_at'):raise HTTPException(422,'Opening balance snapshot must retain its as-at date.')
                    date.fromisoformat(str(meta['as_at']))
                if masters._fingerprint(rows)!=v['fingerprint']:raise HTTPException(422,f'{name}: damaged snapshot fingerprint.')
            if entry['fingerprint']!=versions[-1]['fingerprint']:raise HTTPException(422,f'{name}: newest version does not match snapshot.')
        return [{'master':name,'versions':len(entry['versions']),'rows':len(entry['versions'][-1]['rows'])} for name,entry in snapshot['masters'].items()]
    def check_snapshot(snapshot):
        try:return _check_snapshot(snapshot)
        except (KeyError,ValueError,TypeError,AttributeError):raise HTTPException(422,'Malformed snapshot. Validate its dates, amounts, versions, and fingerprints.')
    @app.post(prefix+'/masters-import/preview')
    def import_preview(data:SnapshotInput,u=Depends(signed_in),s=Depends(db)):
        return {'rows':check_snapshot(data.snapshot),'current_versions':{k:(v.id if (v:=masters.current_version(s,k)) else None) for k in masters.MASTER_FIELDS}}
    @app.post(prefix+'/masters-import/apply')
    def import_apply(data:SnapshotInput,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);check_snapshot(data.snapshot)
        current={k:(v.id if (v:=masters.current_version(s,k)) else None) for k in masters.MASTER_FIELDS}
        if current!=data.expected_versions:raise HTTPException(409,'Master versions changed. Validate the snapshot again.')
        # Core imports commit versions individually. Report a partial failure honestly.
        try:report=masters.import_snapshot(s,data.snapshot,u.display_name,data.reason)
        except Exception:
            LOG.exception('Master import failed after potentially committed versions')
            raise HTTPException(500,'Import failed; some historical versions may have been saved. Inspect masters and revalidate before retrying.')
        return {'ok':True,'report':report}
    def preflight(s,data):
        if data.date_from>data.date_to:raise HTTPException(422,'Start date must precede end date.')
        dates=coverage.all_source_dates(cfg());problems=coverage.range_problems(dates,data.date_from,data.date_to)
        checks=master_files.preflight_masters(cfg(),s,raise_on_failure=False)
        return {'warnings':[coverage.range_warning(*p) for p in problems],'axis_note':coverage.axis_parking_note(dates,data.date_from,data.date_to),'masters':clean([{'master':c.master,'problems':c.problems,'stored_available':masters.current_version(s,c.master) is not None} for c in checks if c.problems])}
    @app.get(prefix+'/runs')
    def run_history(u=Depends(signed_in),s=Depends(db)):
        # Older snapshots can store one business date, whereas legacy run_history expects a pair.
        records=[];now=datetime.now()
        for row in s.query(Run).order_by(Run.started_at.desc()).limit(100):
            try:notes=json.loads(row.notes or '{}')
            except (TypeError,ValueError):notes={'failure_reason':row.notes}
            if not isinstance(notes,dict):notes={}
            try:dates=sorted(json.loads(row.business_dates or '[]'))
            except (TypeError,ValueError):dates=[]
            age=(now-row.heartbeat_at).total_seconds() if row.heartbeat_at else None
            stalled=row.status=='running' and (age is None or age>runs.STALE_RUN_SECONDS)
            r1=notes.get('r1') or {}
            records.append({'run':row.id,'started':row.started_at,'range':f'{dates[0] if dates else "—"} .. {dates[-1] if dates else "—"}','status':'stalled' if stalled else row.status,'current_stage':row.current_stage,'progress_detail':row.progress_detail,'heartbeat_age_s':age,'failure_reason':notes.get('failure_reason'),'duration_s':((row.finished_at-row.started_at).total_seconds() if row.finished_at else (now-row.started_at).total_seconds() if row.status=='running' else None),'bill_lines':r1.get('digital_bill_lines'),'matched':r1.get('matched_total'),'by':row.triggered_by})
        return {'rows':clean(records),'starting':run_lock.locked()}
    @app.post(prefix+'/runs/preflight')
    def run_preflight(data:RunInput,u=Depends(signed_in),s=Depends(db)):
        return preflight(s,data)
    @app.post(prefix+'/runs/{run_id}/mark-failed')
    def mark_failed(run_id:int,data:Confirm,u=Depends(signed_in),s=Depends(db)):
        confirmation(data)
        if run_lock.locked():raise HTTPException(409,'This API still owns an active reconciliation worker. Do not mark it failed.')
        row=s.query(Run).filter_by(id=run_id).with_for_update().first()
        if row is None:raise HTTPException(404,'Run not found.')
        age=(datetime.now()-(row.heartbeat_at or row.started_at)).total_seconds()
        if row.status!='running' or age<runs.STALE_RUN_SECONDS:raise HTTPException(409,'Only a genuinely stalled run may be marked failed.')
        row.status='failed';row.finished_at=datetime.now()
        notes=json.loads(row.notes or '{}')
        notes['failure_reason']=f'Marked failed by {u.display_name}: {data.reason}; last heartbeat {age:.0f}s ago at {row.current_stage or "unknown stage"}.'
        row.notes=json.dumps(notes);s.commit();return {'ok':True}
    @app.post(prefix+'/runs/start')
    def start(data:RunInput,u=Depends(signed_in),s=Depends(db)):
        confirmation(data);check=preflight(s,data)
        if check['warnings'] and not data.allow_incomplete_range:raise HTTPException(422,'Acknowledge the incomplete source coverage first.')
        for m in check['masters']:
            if not m['stored_available'] or not data.use_stored_masters.get(m['master'],'').strip():raise HTTPException(422,f"Resolve master {m['master']} or explicitly authorize its stored version with a reason.")
        if not run_lock.acquire(blocking=False):raise HTTPException(409,'A run is already starting in this API process.')
        if s.query(Run.id).filter_by(status='running').first():run_lock.release();raise HTTPException(409,'A running or stalled run already exists; nothing has been queued.')
        actor=u.display_name
        def work():
            session=factory()
            try:runs.run_reconciliation(cfg(),session,data.date_from,data.date_to,triggered_by=actor,use_stored_masters=data.use_stored_masters or None)
            except Exception:LOG.exception('Reconciliation worker failed; inspect persisted run history')
            finally:session.close();run_lock.release()
        threading.Thread(target=work,daemon=False,name='mop-reconciliation').start()
        return {'accepted':True,'message':'Run accepted. Follow the run history for progress.'}
    @app.get(prefix+'/store-sheet/export')
    def specialized_export(p=Depends(period),u=Depends(signed_in),s=Depends(db)):
        from mop_reco.store_sheet_export import build_workbook
        from mop_reco import store_sheet as ss,store_sheet_source
        sheet(s,p) # enforce the covered aggregate selection before explicit export work
        bundle=store_sheet_source.build(s,p[0].id,p[1],p[2])
        frame=bundle.sheet
        excluded=bundle.excluded
        contribution=bundle.contribution
        if p[3]:
            frame=frame[frame['store']==p[3]].copy()
            if 'store' in excluded:excluded=excluded[excluded['store']==p[3]].copy()
            if 'store' in contribution:contribution=contribution[contribution['store']==p[3]].copy()
        summary=ss.summarise_by_date(frame)
        data=build_workbook(frame,summary,excluded,run_id=(f"{p[0].id} (store {p[3]}; Checks cover the whole run)" if p[3] else p[0].id),run_timestamp=p[0].started_at,date_from=p[1],date_to=p[2],checks=bundle.checks,summary_by_store=ss.summarise_by_store(frame),grand_total=ss.grand_total_row(frame),contribution=contribution,other_types=bundle.other_types)
        return Response(data,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="store-sheet.xlsx"'})
