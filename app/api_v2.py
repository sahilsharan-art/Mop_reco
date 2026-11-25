"""HTTP adapter for Mop_reco's React V2. Run beside the existing Python engines.
No Streamlit imports, no new schema, no raw-source reads on dashboard requests.
"""
from __future__ import annotations
import dataclasses
import json
import logging
import math
import os
import secrets
import tempfile
from datetime import date, datetime
from pathlib import Path
from decimal import Decimal
from typing import Literal

import pandas as pd
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer
import threading
import time

from mop_reco import auth, queries as q, exceptions, store_tasks as tasks, masters_db as masters, uploads
from mop_reco import store_sheet_source as source, float_tracker as floats, run_coverage as rc, manual_match as mm
import api_v2_notices as notices
from mop_reco import store_sheet as store_sheet_mod
from mop_reco.db import get_session, Run, StoreDayAgg, AppUser, StoreTask, Exception_, UploadedFile, ManualMatch
from mop_reco.config import load_config
from mop_reco.export import build_export, ExportMeta

LOG = logging.getLogger(__name__)
PREFIX = '/api/mop'
SESSION_COOKIE = 'mop_v2_session'
CSRF_COOKIE = 'mop_v2_csrf'


def clean(value):
    if dataclasses.is_dataclass(value):
        return clean(dataclasses.asdict(value))
    if isinstance(value, pd.DataFrame):
        return clean(value.to_dict('records'))
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    if hasattr(value, 'item'):
        return clean(value.item())
    return value


def user_dto(u):
    return {'id': u.id, 'username': u.username, 'display_name': u.display_name,
            'role': u.role, 'active': u.active, 'must_change_password': u.must_change_password,
            'last_login_at': clean(u.last_login_at), 'locked_until': clean(u.locked_until)}


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)

class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)

class TextAction(BaseModel):
    text: str = Field(min_length=1, max_length=10000)

class TaskAction(TextAction):
    action: Literal['remark', 'review', 'close', 'reply']

class FloatAction(TextAction):
    store: str = Field(min_length=1, max_length=100)
    business_date: date

class ExceptionAction(TextAction):
    exception_key: str = Field(min_length=1, max_length=1000)

class MasterEdit(BaseModel):
    rows: list[dict] = Field(max_length=100000)
    note: str = Field(min_length=1, max_length=2000)
    expected_version: int | None = None

class NewUser(BaseModel):
    username: str
    display_name: str
    password: str
    role: Literal['admin', 'user'] = 'user'

class UserAction(BaseModel):
    action: Literal['activate', 'deactivate', 'unlock', 'role', 'reset_password']
    role: Literal['admin', 'user'] = 'user'
    password: str = ''


def create_app(session_factory=None, config=None, origin=None):
    app = FastAPI(title='MOP Reconciliation V2 API', docs_url=None, redoc_url=None, openapi_url=None)
    factory = session_factory or get_session
    allowed_origin = origin or os.environ.get('MOP_V2_ORIGIN', 'http://localhost:8091')
    secure = allowed_origin.startswith('https://')

    def cfg():
        return config if config is not None else load_config()

    def auth_cfg():
        return auth.settings(cfg().raw)

    def db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    @app.middleware('http')
    async def security(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('origin') != allowed_origin:
                return JSONResponse({'detail': 'Request origin is not allowed.'}, status_code=403)
            if request.url.path != PREFIX + '/auth/login':
                cookie = request.cookies.get(CSRF_COOKIE, '')
                header = request.headers.get('x-csrf-token', '')
                if not cookie or not secrets.compare_digest(cookie, header):
                    return JSONResponse({'detail': 'Refresh the page before saving.'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def safe_json(detail, status_code):
        return JSONResponse({'detail': detail}, status_code=status_code,
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return safe_json(str(exc), 422)

    @app.exception_handler(LookupError)
    async def not_found_error(request, exc):
        return safe_json('That record was not found. Refresh the page and try again.', 404)

    @app.exception_handler(IntegrityError)
    async def conflict_error(request, exc):
        LOG.warning('MOP V2 conflicting write: %s', request.url.path, exc_info=True)
        return safe_json('Someone else changed this at the same moment. Refresh the page and check before trying again.', 409)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        LOG.exception('MOP V2 request failed: %s', request.url.path)
        return safe_json('The operation failed. No success has been recorded; contact your administrator.', 500)

    def identity(request: Request, s=Depends(db)):
        u = auth.resolve_session(s, request.cookies.get(SESSION_COOKIE, ''), auth_cfg())
        if u is None:
            raise HTTPException(401, 'Sign in to continue.')
        return u

    def signed_in(u=Depends(identity)):
        if u.must_change_password:
            raise HTTPException(403, 'Change your temporary password before continuing.')
        return u

    def admin(u=Depends(signed_in)):
        if u.role != 'admin':
            raise HTTPException(403, 'Administrator access required.')
        return u

    def resolve_period(s, date_from, date_to, store, run_id):
        # Run.notes is about 1.4 MB per run: it is deferred here and only read by the notices that need it
        light = s.query(Run).options(defer(Run.notes))
        run = light.filter(Run.id == run_id).first() if run_id else light.filter(Run.status == 'completed').order_by(Run.id.desc()).first()
        if run is None or run.status != 'completed':
            raise HTTPException(409, 'No completed reconciliation run is available.')
        dates = sorted(json.loads(run.business_dates or '[]'))
        if not dates:
            raise HTTPException(409, 'This run has no business dates.')
        lo = date_from or date.fromisoformat(dates[0])
        hi = date_to or date.fromisoformat(dates[-1])
        if lo > hi:
            raise HTTPException(422, 'Start date must be on or before end date.')
        return run, lo, hi, store or None, (date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]))

    def period_any(u=Depends(signed_in), date_from: date | None = None, date_to: date | None = None, store: str | None = None,
                   run_id: int | None = None, s=Depends(db)):
        """The period exactly as asked, even when it lies outside the run (the notices endpoint says so instead of failing)."""
        return resolve_period(s, date_from, date_to, store, run_id)[:4]

    def period(u=Depends(signed_in), date_from: date | None = None, date_to: date | None = None, store: str | None = None,
               run_id: int | None = None, s=Depends(db)):
        run, lo, hi, st, (first, last) = resolve_period(s, date_from, date_to, store, run_id)
        if rc.entirely_outside(first, last, lo, hi):
            # CLAUDE.md 6.24: 'not reconciled' is said, never shown as an empty grid or a row of zeros
            raise HTTPException(409, f'Run #{run.id} covers {first:%d/%m/%Y} to {last:%d/%m/%Y}. The period you chose is not reconciled; run it from Upload first.')
        return run, lo, hi, st

    def require_aggregates(s, p):
        run, lo, hi, st = p
        query = s.query(StoreDayAgg.id).filter(StoreDayAgg.run_id == run.id,
                StoreDayAgg.business_date >= lo.isoformat(), StoreDayAgg.business_date <= hi.isoformat())
        if st:
            query = query.filter(StoreDayAgg.store == st)
        if query.first() is None:
            # Never invoke load_fast's slow fallback, which can read raw files on page load.
            raise HTTPException(409, 'No precomputed store-day data for this selection. Choose a covered period or run the existing reconciliation pipeline.')

    sheet_cache = {}
    sheet_lock = threading.Lock()

    def match_state(s):
        # a new match raises the highest id; a reversal changes the active count - either changes the key, so the cache can never serve a stale F
        active, newest = s.query(func.count(ManualMatch.id), func.max(ManualMatch.id)).filter(ManualMatch.lifecycle == 'active').one()
        return int(active or 0), int(newest or 0), int(s.query(func.max(ManualMatch.id)).scalar() or 0)

    notice_cache = {}

    @app.get(PREFIX + '/health')
    def health():
        return {'status': 'ok', 'version': '2.0'}

    @app.post(PREFIX + '/auth/login')
    def login(data: Credentials, request: Request, response: Response, s=Depends(db)):
        result = auth.authenticate(s, data.username, data.password, auth_cfg(), ip=request.client.host if request.client else '')
        if result.user is None:
            raise HTTPException(401, result.message)
        token = auth.create_session(s, result.user, auth_cfg())
        response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure, samesite='strict', path=PREFIX,
                            max_age=auth.cookie_max_age_seconds(auth_cfg()))
        response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), httponly=False, secure=secure, samesite='strict', path='/')
        return user_dto(result.user)

    @app.get(PREFIX + '/auth/me')
    def me(response: Response, request: Request, u=Depends(identity)):
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), secure=secure, samesite='strict', path='/')
        return user_dto(u)

    @app.post(PREFIX + '/auth/logout')
    def logout(request: Request, response: Response, u=Depends(identity), s=Depends(db)):
        auth.end_session(s, request.cookies.get(SESSION_COOKIE, ''), u.username)
        response.delete_cookie(SESSION_COOKIE, path=PREFIX)
        response.delete_cookie(CSRF_COOKIE, path='/')
        return {'ok': True}

    @app.post(PREFIX + '/auth/password')
    def password(data: PasswordChange, request: Request, u=Depends(identity), s=Depends(db)):
        user = s.get(AppUser, u.id)
        auth.change_password(s, user, data.current_password, data.new_password, auth_cfg(),
                             keep_token_hash=auth.token_hash_of(request.cookies.get(SESSION_COOKIE, '')))
        return user_dto(user)

    @app.get(PREFIX + '/bootstrap')
    def bootstrap(u=Depends(signed_in), s=Depends(db)):
        runs = s.query(Run).options(defer(Run.notes)).order_by(Run.id.desc()).limit(50).all()
        completed = next((r for r in runs if r.status == 'completed'), None)
        dates = sorted(json.loads(completed.business_dates or '[]')) if completed else []
        stores = s.query(StoreDayAgg.store).filter(StoreDayAgg.run_id == completed.id).distinct().order_by(StoreDayAgg.store).all() if completed else []
        return {'user': user_dto(u), 'runs': [{'id': r.id, 'status': r.status, 'started_at': clean(r.started_at),
                    'current_stage': r.current_stage, 'progress_detail': r.progress_detail} for r in runs],
                'run_id': completed.id if completed else None, 'date_from': dates[0] if dates else '',
                'date_to': dates[-1] if dates else '', 'stores': [r[0] for r in stores],
                'masters': [{'key': k, 'title': masters.TITLES[k]} for k in masters.MASTER_FIELDS],
                'sources': [{'key': x.key, 'title': x.display_name, 'extensions': list(x.extensions),
                             'month_specific': x.month_specific, 'note': x.note} for x in uploads.SOURCE_REGISTRY]}

    @app.get(PREFIX + '/summary')
    def summary(p=Depends(period), u=Depends(signed_in), s=Depends(db)):
        require_aggregates(s, p)
        run, lo, hi, st = p
        return clean({'matrix': source.mop_particulars_matrix(s, run.id, lo, hi, st),
                      'verdict': q.verdict(s, run.id, lo, hi, st), 'risk': q.risk_split(s, lo, hi, st),
                      'trend': q.daily_trend(s, run.id, lo, hi, st), 'exceptions': q.exceptions_by_type(s, lo, hi, st),
                      'cash': source.store_cash_summary(s, run.id, lo, hi, st),
                      'stores': store_sheet_mod.summarise_by_store(sheet(s, p)),
                      'tasks': tasks.tiles(s, date.today(), date_from=lo, date_to=hi, store=st),
                      # CLAUDE.md 6.9b: matched by the SYSTEM, matched by a PERSON and asserted-but-unverified are never blended into one rate
                      'bills': q.bill_split(s, run.id, lo, hi, st),
                      'asserted': int(s.query(func.count(ManualMatch.id)).filter(ManualMatch.status == mm.STATUS_ASSERTED, ManualMatch.lifecycle == 'active').scalar() or 0)})

    @app.get(PREFIX + '/notices')
    def notices_for(p=Depends(period_any), u=Depends(signed_in), s=Depends(db)):
        """The warnings the Streamlit dashboard showed on every page: cut-off POS file, parked Axis, unreconciled months, failed controls, MISMATCH."""
        run, lo, hi, st = p
        # one minute per (run, period, store, manual-match state): the MISMATCH controls cost about 2 s to compute and every screen asks
        key = (run.id, lo, hi, st, match_state(s))
        now = time.monotonic()
        with sheet_lock:
            hit = notice_cache.get(key)
            if hit and now - hit[0] < 60:
                return hit[1]
        result = {'run_id': run.id, 'notices': notices.build(s, s.get(Run, run.id), lo, hi, st, cfg())}
        with sheet_lock:
            while len(notice_cache) >= 12:
                notice_cache.pop(min(notice_cache, key=lambda k: notice_cache[k][0]))
            notice_cache[key] = (now, result)
        return result

    def sheet(s, p):
        """The store-day sheet. Cash and Float page through it and Summary groups it: it is built once per (run, period, store, manual-match state)
        and kept two minutes, so a page of 50 rows no longer rebuilds the whole run."""
        require_aggregates(s, p)
        run, lo, hi, st = p
        key = (run.id, lo, hi, st, match_state(s))
        now = time.monotonic()
        with sheet_lock:
            hit = sheet_cache.get(key)
            if hit and now - hit[0] < 120:
                return hit[1].copy()
        frame = source.load_fast(s, run.id, lo, hi, st)[0]
        with sheet_lock:
            while len(sheet_cache) >= 6:
                sheet_cache.pop(min(sheet_cache, key=lambda k: sheet_cache[k][0]))
            sheet_cache[key] = (now, frame)
        return frame.copy()

    @app.get(PREFIX + '/cash')
    def cash(p=Depends(period), u=Depends(signed_in), s=Depends(db), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        frame = sheet(s, p)
        return {'rows': clean(frame.iloc[offset:offset+limit]), 'total': len(frame), 'summary': clean(source.store_cash_summary(s, p[0].id, p[1], p[2], p[3]))}

    def float_frame(s, p):
        grid = floats.build_float_grid(sheet(s, p), cfg().raw)
        return floats.with_remarks(grid, floats.float_remarks_for_range(s, p[1], p[2], p[3]))

    @app.get(PREFIX + '/float')
    def float_rows(p=Depends(period), u=Depends(signed_in), s=Depends(db), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        frame = float_frame(s, p)
        return {'rows': clean(frame.iloc[offset:offset+limit]), 'total': len(frame)}

    @app.post(PREFIX + '/float/remark')
    def float_remark(data: FloatAction, u=Depends(signed_in), s=Depends(db)):
        if not s.query(StoreDayAgg.id).filter(StoreDayAgg.store == data.store, StoreDayAgg.business_date == data.business_date.isoformat()).first():
            raise HTTPException(404, 'No store-day record exists.')
        floats.set_float_remark(s, data.store, data.business_date.isoformat(), data.text, u.display_name)
        return {'ok': True}

    @app.get(PREFIX + '/tasks')
    def task_rows(p=Depends(period), u=Depends(signed_in), s=Depends(db), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  search: str = '', status: str = '', sort: str = 'Overdue first, oldest due'):
        # default = the live tasks (open, replied, under review); 'all' shows closed ones too, as the Streamlit page did
        wanted = None if status == 'all' else ([status] if status else [tasks.OPEN, tasks.REPLIED, tasks.UNDER_REVIEW])
        filters = {'date_from': p[1], 'date_to': p[2], 'store': p[3], 'search': search or None, 'statuses': wanted}
        frame = tasks.tasks_page(s, date.today(), sort, limit, offset, **filters)
        total = tasks._filtered(s, **filters).count()
        return {'rows': clean(frame), 'total': total, 'summary': clean(tasks.tiles(s, date.today(), **filters))}

    @app.get(PREFIX + '/tasks/{task_id}/history')
    def task_history(task_id: int, u=Depends(signed_in), s=Depends(db)):
        return clean(tasks.history(s, task_id))

    @app.post(PREFIX + '/tasks/{task_id}')
    def task_action(task_id: int, data: TaskAction, u=Depends(signed_in), s=Depends(db)):
        row = s.get(StoreTask, task_id)
        if row is None:
            raise HTTPException(404, 'Task not found.')
        if row.status == tasks.CLOSED and data.action != 'close':
            raise HTTPException(409, 'This task is closed. Closed tasks are history; nothing can be added to them.')
        if data.action == 'remark':
            tasks.add_remark(s, task_id, data.text, u.display_name)
        elif data.action == 'review':
            tasks.mark_under_review(s, task_id, u.display_name)
        elif data.action == 'close':
            tasks.close_task(s, task_id, data.text, u.display_name, date.today())
        else:
            frame = pd.DataFrame([{'task_id': task_id, 'store': row.store, 'reply': data.text, 'replied_by': u.display_name}])
            preview = tasks.preview_replies(s, frame)
            if preview.rejected:
                raise HTTPException(422, '; '.join(preview.rejected[0].reasons))
            tasks.apply_replies(s, preview, u.display_name)
        return {'ok': True}

    def exception_types(s):
        return [r[0] for r in s.query(Exception_.type).distinct().all()]

    @app.get(PREFIX + '/exceptions')
    def exception_rows(p=Depends(period), u=Depends(signed_in), s=Depends(db), offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                       status: str = '', sort: str = q.DEFAULT_EXCEPTION_SORT):
        # default = the work queue (open + under investigation); 'all' also lists closed and auto-resolved (the audit record)
        statuses = ('open', 'under_investigation', 'closed', 'auto_resolved') if status == 'all' else ((status,) if status else ('open', 'under_investigation'))
        frame, total, value = q.exception_page(s, p[1], p[2], p[3], exception_types(s),
                    statuses=statuses, limit=limit, offset=offset, sort=sort)
        if not frame.empty and 'exception_key' in frame:
            remarks = q.latest_remarks_full(s, list(frame['exception_key']))
            frame['remark'] = [remarks.get(k, ('', '', None))[0] for k in frame['exception_key']]
            frame['remark_by'] = [remarks.get(k, ('', '', None))[1] for k in frame['exception_key']]
            frame['remark_at'] = [remarks.get(k, ('', '', None))[2] for k in frame['exception_key']]
        # CLAUDE.md 6.9a: money NOT received and money received-but-not-billed are opposite situations and are never added together
        bn, bv, tn, tv = q.exception_counts(s, p[1], p[2], p[3], q.NOT_RECEIVED_TYPES, q.RECEIVED_NOT_BILLED_TYPES, statuses=statuses)
        groups = {'not_received': {'count': bn, 'value': bv}, 'received_not_billed': {'count': tn, 'value': tv}}
        return {'rows': clean(frame.drop(columns=['detail'], errors='ignore')), 'total': total, 'groups': clean(groups)}

    @app.post(PREFIX + '/exceptions/remark')
    def exception_remark(data: ExceptionAction, u=Depends(signed_in), s=Depends(db)):
        exceptions.add_remark(s, data.exception_key, data.text, u.display_name)
        return {'ok': True}

    @app.get(PREFIX + '/masters/{name}')
    def master_rows(name: str, u=Depends(signed_in), s=Depends(db)):
        if name not in masters.MASTER_FIELDS:
            raise HTTPException(404, 'Unknown master.')
        v = masters.current_version(s, name)
        return clean({'rows': masters.current_rows(s, name), 'fields': masters.MASTER_FIELDS[name],
                      'version': v.id if v else None, 'history': masters.history(s, name),
                      'in_database': masters.in_database(s)})

    @app.post(PREFIX + '/masters/{name}')
    def save_master(name: str, data: MasterEdit, u=Depends(signed_in), s=Depends(db)):
        if not masters.in_database(s):
            raise HTTPException(409, 'Masters are still sourced from Excel. Complete the existing master migration before editing here.')
        # Serialize writers against the app setting row on PostgreSQL.
        from mop_reco.db import AppSetting
        s.query(AppSetting).filter(AppSetting.key == masters.SETTING_MASTERS_SOURCE).with_for_update().first()
        current = masters.current_version(s, name)
        if (current.id if current else None) != data.expected_version:
            raise HTTPException(409, 'This master changed. Reload before saving.')
        version, created, diff = masters.save_version(s, name, data.rows, u.display_name, data.note)
        return {'version': version.id, 'created': created, 'diff': clean(diff)}

    @app.get(PREFIX + '/uploads')
    def upload_history(u=Depends(signed_in), s=Depends(db)):
        rows = s.query(UploadedFile).order_by(UploadedFile.id.desc()).limit(200).all()
        keys = ('id', 'uploaded_at', 'source_key', 'report_name', 'month', 'original_file_name', 'size_bytes', 'row_count', 'uploaded_by', 'status')
        return {'rows': [clean({k: getattr(r, k) for k in keys}) for r in rows], 'total': s.query(UploadedFile).count()}

    @app.post(PREFIX + '/uploads')
    async def upload(file: UploadFile = File(...), source_key: str = Form(...), month: str = Form(''),
                     confirm: bool = Form(False), u=Depends(signed_in), s=Depends(db)):
        spec = uploads.SOURCE_BY_KEY.get(source_key)
        if spec is None:
            raise HTTPException(422, 'Unknown report type.')
        if spec.month_specific and not month:
            raise HTTPException(422, 'Choose a month for this report.')
        original = file.filename or 'upload'
        suffix = Path(original).suffix.lower()
        if suffix not in spec.extensions:
            raise HTTPException(422, 'This file type is not accepted for the selected report.')
        temp = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as dest:
                temp = Path(dest.name)
                size = 0
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 1024 * 1024 * 1024:
                        raise HTTPException(413, 'The file exceeds the existing 1024 MB limit.')
                    dest.write(chunk)
            preview = uploads.validate_upload(spec, temp, original, month or None, cfg())
            result = {'ok': preview.ok, 'errors': preview.errors, 'warnings': preview.warnings,
                      'row_count': preview.row_count, 'date_range': preview.date_range, 'columns_found': preview.columns_found,
                      'rows': clean(preview.first_rows) if preview.first_rows is not None else []}
            if confirm:
                if not preview.ok:
                    raise HTTPException(422, 'Validation failed. Nothing was stored.')
                row = uploads.store_file(spec, temp, original, month or None, u.display_name, s, cfg(), preview.row_count, preview.date_range)
                result.update({'stored': True, 'filename': Path(row.stored_path).name, 'id': row.id})
                if getattr(row, '_config_gap_note', None):
                    result['warnings'].append(row._config_gap_note)
            return clean(result)
        finally:
            await file.close()
            if temp is not None:
                temp.unlink(missing_ok=True)

    @app.get(PREFIX + '/users')
    def user_rows(u=Depends(admin), s=Depends(db)):
        return {'rows': [user_dto(x) for x in auth.list_users(s)]}

    @app.post(PREFIX + '/users')
    def new_user(data: NewUser, u=Depends(admin), s=Depends(db)):
        return user_dto(auth.create_user(s, data.username, data.display_name, data.role, data.password, u.display_name,
                                       must_change=True, cfg=auth_cfg()))

    @app.post(PREFIX + '/users/{user_id}')
    def update_user(user_id: int, data: UserAction, u=Depends(admin), s=Depends(db)):
        if data.action in ('activate', 'deactivate'):
            auth.set_active(s, user_id, data.action == 'activate', u.display_name)
        elif data.action == 'unlock':
            auth.unlock(s, user_id, u.display_name)
        elif data.action == 'role':
            auth.set_role(s, user_id, data.role, u.display_name)
        else:
            auth.admin_reset_password(s, user_id, data.password, u.display_name, auth_cfg())
        return {'ok': True}

    @app.get(PREFIX + '/export/{kind}')
    def export(kind: Literal['cash', 'float', 'tasks', 'exceptions'], p=Depends(period), u=Depends(signed_in), s=Depends(db), status: str = '', search: str = ''):
        run, lo, hi, st = p
        if kind == 'cash': frame = sheet(s, p)
        elif kind == 'float': frame = float_frame(s, p)
        elif kind == 'tasks': frame = tasks.tasks_all(s, date.today(), date_from=lo, date_to=hi, store=st, search=search or None, statuses=[status] if status else None)
        else: frame = q.exception_export(s, lo, hi, st, exception_types(s), statuses=(status,) if status else None).drop(columns=['detail', 'exception_key'], errors='ignore')
        filters = {'store': st or 'All stores', 'status': status or 'Default'}
        if kind == 'tasks':
            filters['search'] = search       # only tasks are filtered by search on the server; the other grids are searched on screen only
        value_column = {'exceptions': 'amount'}.get(kind)
        result = build_export(frame, ExportMeta(run_id=run.id, run_timestamp=run.started_at, date_from=lo, date_to=hi,
                              filters=filters, value_column=value_column, title=kind))
        mime = 'text/csv' if result.fmt == 'csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        return Response(result.data, media_type=mime, headers={'Content-Disposition': f'attachment; filename="{result.filename}"'})

    from api_v2_workflows import register
    register(app, db=db, signed_in=signed_in, cfg=cfg, factory=factory, clean=clean, prefix=PREFIX, period=period, sheet=sheet)
    return app

app = create_app()
