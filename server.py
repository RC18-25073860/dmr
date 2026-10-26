#!/usr/bin/env python3
"""FastAPI entrypoint for the local DMR app."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import asyncio
import os
import logging
import signal
import socket
import threading
import uuid
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from backend.auth import check_loopback_request, check_request
from backend.feishu_catalog import install_feishu_catalog
from backend.feishu_integration import install_feishu_integration
from backend.manual_catalog_updates import install_manual_catalog_updates
from backend.nas import install_nas, is_storage_sync_request
from backend.mail_ingest import install_mail_ingest, is_mail_ingest_request
from backend.transfer_manifest import looks_like_transfer_manifest
from backend.audit_log import install_audit_log
from backend.runtime_lock import acquire as acquire_runtime_lock
from backend.config import DEFAULT_PORT, ROOT, AppConfig, load_config, validate_deployment_config
from backend.service import (
    MAX_BODY,
    STATIC_ALLOWLIST,
    DMRStore,
    MAX_FILE_BYTES,
    clean_filename,
    clean_text,
    file_type,
    json_dumps,
    now_iso,
    preview_xlsx,
    sha256_bytes,
    size_label,
)

STORE: DMRStore | None = None
CONFIG: AppConfig | None = None


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config):
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next):
        try:
            check_request(request, self.config, allow_missing_origin=is_storage_sync_request(request) or is_mail_ingest_request(request))
            if request.method in {"POST", "PUT", "PATCH"}:
                content_type = request.headers.get("content-type", "")
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="POST仅接受application/json")
                length = int(request.headers.get("content-length", "0") or "0")
                if length > MAX_BODY:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体过大")
            if is_mail_ingest_request(request) and request.method == "POST":
                # Bound chunked bodies too, before JSON parsing allocates the payload.
                chunks, total = [], 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_BODY:
                        raise HTTPException(413, "请求体过大")
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({"error": str(exc.detail), "status": exc.status_code}, status_code=exc.status_code)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response


def _store() -> DMRStore:
    assert STORE is not None
    return STORE



async def _validated_payload(request: Request, payload: Any) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请求体过大")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JSON请求体必须是对象")
    return payload


def _validate_files_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    files = payload.get("files", [])
    if not isinstance(files, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="files必须是数组")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("data"), str):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="files数组项必须包含字符串name和data")
    return files


def create_app(
    store: DMRStore, config: AppConfig, *,
    feishu_event_verifier=None, feishu_sync_queue=None,
) -> FastAPI:
    global STORE, CONFIG
    STORE, CONFIG = store, config
    from backend.identity import IdentityService
    from backend.access import AccessPolicy
    validate_deployment_config(config)
    identity = IdentityService(store)
    if config.environment in {'prod', 'production'} and identity.setup_required():
        raise RuntimeError('请先在本机初始化管理员，再启用正式部署配置')
    app = FastAPI(title="DMR Local Backend", version="2.3-nas-local")
    app.add_middleware(SecurityMiddleware, config=config)
    app.state.identity = identity
    COOKIE = 'dmr_session'

    def local_mode():
        if config.auth_mode != 'local':
            raise HTTPException(503, '企业身份未配置，拒绝执行受控操作')

    def policy(request, permission=None, allow_password_change=False):
        local_mode()
        user = identity.current(request.cookies.get(COOKIE, ''))
        if user.get('must_change_password') and not allow_password_change:
            raise HTTPException(403, {'error':'请先修改初始密码，再使用资料功能','code':'PASSWORD_CHANGE_REQUIRED'})
        access = AccessPolicy(store, user)
        if permission: access.require(permission)
        return access

    def cookie(response, token):
        response.set_cookie(COOKIE, token, httponly=True, samesite='strict', secure=config.cookie_secure, max_age=43200, path='/')

    async def as_actor(request, permission, func):
        def call():
            with store.lock, store.conn.immediate():
                access = policy(request, permission)
                with store.acting_as(f"{access.user['display_name']} [{access.user['id']}]"):
                    return func(access)
        return await run_in_threadpool(call)

    nas = install_nas(app, store, identity, config)
    install_mail_ingest(app, store, identity, policy)
    install_feishu_catalog(app, config, policy)
    install_feishu_integration(app, policy, verifier=feishu_event_verifier, queue=feishu_sync_queue)
    manual_updates = install_manual_catalog_updates(app, store, config, policy)
    install_audit_log(app, store, policy)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse((exc.detail if isinstance(exc.detail, dict) else {'error': str(exc.detail)}) | {'status': exc.status_code}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith('/api/integrations/mail/'):
            return JSONResponse({'status': 422, 'error': '邮件接入字段校验失败',
                'errors': [{'field': '.'.join(str(p) for p in e['loc']),
                            'type': e['type'], 'message': e['msg']} for e in exc.errors()]}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(ValueError)
    async def value_error(_request: Request, exc: ValueError):
        return JSONResponse({'error': str(exc), 'status': 400}, status_code=400)

    @app.exception_handler(KeyError)
    async def key_error(_request: Request, exc: KeyError):
        return JSONResponse({'error': str(exc), 'status': 404}, status_code=404)

    @app.exception_handler(Exception)
    async def generic_error(_request: Request, exc: Exception):
        logging.getLogger('dmr').exception('Request failed',exc_info=exc)
        return JSONResponse({'error': '服务处理失败，请联系管理员查看本地日志', 'status': 500}, status_code=500)

    @app.get('/api/auth/status')
    def auth_status():
        with store.lock:
            return {'setup_required': identity.setup_required() if config.auth_mode=='local' and config.setup_allowed else False, 'mode':config.auth_mode}

    @app.get('/api/auth/me')
    def auth_me(request: Request):
        with store.lock:
            access=policy(request, allow_password_change=True)
            return {'user':access.user,'permissions':access.permissions}

    @app.post('/api/auth/setup')
    async def auth_setup(request: Request, response: Response, payload: dict = Body(...)):
        local_mode()
        if not config.setup_allowed:
            raise HTTPException(403, '当前部署不允许首次管理员初始化')
        payload=await _validated_payload(request,payload)
        if set(payload)!={'display_name','username','password'}: raise ValueError('请填写显示名称、账号和密码')
        def perform():
            with store.lock:
                return identity.setup(payload['display_name'],payload['username'],payload['password'])
        token,user=await run_in_threadpool(perform)
        cookie(response,token)
        return {'user':user}

    @app.post('/api/auth/login')
    async def auth_login(request: Request, response: Response, payload: dict = Body(...)):
        local_mode()
        payload=await _validated_payload(request,payload)
        if set(payload)!={'username','password'}: raise ValueError('请填写账号和密码')
        token,user=await run_in_threadpool(identity.login,payload['username'],payload['password'])
        cookie(response,token)
        return {'user':user}

    @app.post('/api/auth/change-password')
    async def auth_change_password(request: Request, response: Response, payload: dict = Body(...)):
        local_mode()
        payload = await _validated_payload(request, payload)
        if set(payload) != {'current_password', 'new_password'}:
            raise ValueError('请填写当前密码和新密码')
        user = await run_in_threadpool(identity.change_password, request.cookies.get(COOKIE, ''), payload['current_password'], payload['new_password'])
        response.delete_cookie(COOKIE, path='/', httponly=True, samesite='strict', secure=config.cookie_secure)
        return {'user': user, 'must_login': True}

    @app.post('/api/auth/logout')
    async def auth_logout(request: Request, response: Response):
        await run_in_threadpool(identity.logout,request.cookies.get(COOKIE,''))
        response.delete_cookie(COOKIE,path='/',httponly=True,samesite='strict',secure=config.cookie_secure)
        return {'ok':True}

    @app.get('/api/users')
    def users(request: Request):
        with store.lock:
            policy(request,'manage_users')
            users = identity.list_users()
            for user in users:
                account = nas.user_account(user['id'])
                user.update(nas_account_configured=account['password_configured'], nas_account=account['account'], nas_check_status=account['check_status'])
            return {'users': users}

    @app.get('/api/users/audit')
    def users_audit(request: Request):
        with store.lock:
            policy(request,'manage_users')
            entries=identity.audit()
            return {'entries':[entry|{'time':entry['created_at'],'actor_id':entry['actor']['id'],'actor_name':entry['actor']['display_name'],'target_id':entry['target']['id']} for entry in entries]}

    @app.post('/api/users')
    async def add_user(request: Request, payload: dict = Body(...)):
        payload=await _validated_payload(request,payload)
        return await as_actor(request,'manage_users',lambda p:{'user':identity.create_user(payload,p.user)})

    @app.post('/api/users/{user_id}')
    async def edit_user(user_id: str, request: Request, payload: dict = Body(...)):
        payload=await _validated_payload(request,payload)
        return await as_actor(request,'manage_users',lambda p:{'user':identity.update_user(user_id,payload,p.user)})

    @app.get('/api/health')
    def health(request: Request):
        with store.lock:
            state=policy(request,'read').filtered_state()
            return {'ok':True,'backend':'fastapi','database':'sqlite','records':len(state['records']),'files':len(state['libraryFiles'])}

    @app.get('/api/capabilities')
    def capabilities():
        return {'backend':'FastAPI + Uvicorn','frontendRoot':'frontend/dist','database':'SQLAlchemy + Alembic + SQLite',
                'auth':{'mode':config.auth_mode,'enterpriseConfigured':False,'localMode':config.auth_mode=='local','sessionRequired':True},
                'externalSystems':{'feishu':'未接入','erp':'未接入','plm':'未接入','email':'未接入'}}

    @app.get('/api/archive-subjects')
    def archive_subjects(request: Request):
        from backend.subjects import ALLOWED_ARCHIVE_SUBJECTS
        with store.lock:
            policy(request,'read')
            return {'subjects': sorted(ALLOWED_ARCHIVE_SUBJECTS)}

    @app.get('/api/state')
    def api_state(request: Request):
        with store.lock:
            p=policy(request,'read')
            state=p.filtered_state()
            for file in state['libraryFiles']:
                job = store.execute('SELECT status,last_error,target_path,synced_at FROM nas_sync_jobs WHERE file_id=?', (file['id'],)).fetchone()
                if job:
                    file.update(sync_status=job['status'], sync_error=job['last_error'] or '', nas_path=job['target_path'] or '', synced_at=job['synced_at'] or '')
            read_ids=identity.read_notification_ids(p.user)
            for entry in state['notifications']: entry['read']=entry['id'] in read_ids
            return state

    @app.get('/api/batch-files/{batch_id}/{item_id}/{action}')
    def api_batch_file(batch_id: str, item_id: str, action: str, request: Request):
        if action not in {'preview', 'download'}:
            raise HTTPException(404, '操作不存在')
        with store.lock:
            access = policy(request, action)
            access.batch(batch_id)
            row = store.execute('SELECT * FROM batch_items WHERE id=? AND batch_id=?', (item_id, batch_id)).fetchone()
            if not row:
                raise HTTPException(404, '本批次中没有此文件')
            if row['status'] == 'committed':
                raise HTTPException(409, '文件已归档，请通过受控文件入口查看')
            metadata = json.loads(row['metadata'] or '{}')
            try:
                data = store.read_blob(metadata.get('blob_path', ''))
            except (FileNotFoundError, IsADirectoryError):
                raise HTTPException(404, '暂存原文件不存在')
            if sha256_bytes(data) != row['sha256']:
                raise HTTPException(409, '原文件校验失败，拒绝读取')
            name = row['file_name']
            headers = {'Cache-Control': 'no-store', 'Content-Security-Policy': "sandbox", 'ETag': row['sha256']}
            with store.acting_as(f"{access.user['display_name']} [{access.user['id']}]") , store.conn:
                store.add_log('核对原文件', name, f"{action} · 批次 {batch_id} · 文件 {item_id}", f'REVIEW-{uuid.uuid4().hex[:8]}', '成功')
            if action == 'download':
                headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(name)}"
                return Response(data, media_type='application/octet-stream', headers=headers)
            headers['Content-Disposition'] = 'inline'
            ext = Path(name).suffix.lower()
            if ext == '.pdf':
                return Response(data, media_type='application/pdf', headers=headers)
            if ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
                return Response(data, media_type={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif','.webp':'image/webp'}[ext], headers=headers)
            if ext in {'.xlsx', '.xls'}:
                return HTMLResponse(preview_xlsx(data, name), headers=headers)
            return PlainTextResponse(f'{name}\n此格式暂不支持在线预览，请下载原文件，用对应软件打开。', headers=headers)

    @app.get('/api/batches/{batch_id:path}')
    def api_batch(batch_id: str, request: Request):
        with store.lock: return policy(request,'analyze').batch(batch_id)

    def check_archive_subjects(batch_id):
        from backend.subjects import validate_new_archive_subject
        rows = store.execute("SELECT * FROM batch_items WHERE batch_id=? AND status='matched'", (batch_id,)).fetchall()
        for row in rows:
            meta = json.loads(row['metadata'] or '{}')
            if meta.get('incomplete_archive') and clean_text(row['registrant']) in ('', '待确认'):
                continue  # Explicitly approved missing subject; commit rechecks approval.
            if store.find_records_for_item(dict(row)):
                continue
            try:
                validate_new_archive_subject(row['registrant'])
            except HTTPException:
                msg = f"新归档主体未在允许范围内：{row['registrant']}"
                store.create_exception_for_item(batch_id, row['id'], row['file_name'], 'conflict', msg, row['record_id'], row['code'])
                store.execute(
                    "UPDATE batch_items SET status='conflict',recommendation=?,result_message=?,updated_at=? WHERE id=?",
                    (msg, msg, now_iso(), row['id']),
                )

    @app.post('/api/analyze')
    async def api_analyze(request: Request, payload: dict = Body(...)):
        payload=await _validated_payload(request,payload)
        files=_validate_files_payload(payload)
        if any(looks_like_transfer_manifest(item['name']) for item in files):
            raise HTTPException(422, 'DMR文件转移清单请使用专用192归档入口，无需补受控文件字段')
        def perform(p):
            # Service validates each parsed item inside its rollback/temporary-file cleanup boundary.
            result=store.analyze(files,validate_item=p.validate_upload)
            with store.conn:
                store.execute('UPDATE batches SET owner_user_id=? WHERE id=?',(p.user['id'],result['batch_id']))
                if payload.get('restrict_new_subject') is True:
                    check_archive_subjects(result['batch_id'])
                    result['items'] = store.get_batch(result['batch_id'])['items']
                    result['summary'] = {'total': len(result['items']), **{
                        status: sum(item['status'] == status for item in result['items'])
                        for status in ('matched', 'conflict', 'unmatched', 'duplicate')}}
                    store.execute('UPDATE batches SET summary=? WHERE id=?',
                                  (json.dumps(result['summary']), result['batch_id']))
            return result
        return await as_actor(request,'analyze',perform)

    @app.post('/api/transfer-manifests/upload')
    async def api_upload_transfer_manifest(request: Request, payload: dict = Body(...)):
        payload = await _validated_payload(request, payload)
        filename = payload.get('name')
        encoded = payload.get('data')
        if not isinstance(filename, str) or not isinstance(encoded, str):
            raise HTTPException(400, '需要文件名和文件内容')
        if clean_filename(filename) != filename or not looks_like_transfer_manifest(filename):
            raise HTTPException(422, '请选择文件名以DMR文件转移清单开头的电子表格')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, '文件内容不是合法的base64') from exc
        if not raw or len(raw) > MAX_FILE_BYTES:
            raise HTTPException(413, '转移清单文件为空或超过大小限制')
        def archive():
            access = policy(request, 'commit')
            return app.state.transfer_manifest_archive.ingest_web(access.user, filename, raw)
        code, body = await run_in_threadpool(archive)
        return JSONResponse(body, status_code=code)

    @app.post('/api/review')
    async def api_review(request: Request, payload: dict = Body(...)):
        payload=await _validated_payload(request,payload)
        def perform(p):
            p.batch(clean_text(payload.get('batch_id')))
            for key in ('record_id','context_record_id'):
                if payload.get(key): p.record(clean_text(payload[key]))
            # Database helper nests transactions; post-validation failure rolls back review and its logs.
            with store.conn.immediate():
                result=store.review(payload)
                if payload.get('restrict_new_subject') is True:
                    check_archive_subjects(payload['batch_id'])
                    result = {'item': store.row_item(store.execute("SELECT * FROM batch_items WHERE id=?", (payload['item_id'],)).fetchone())}
                p.batch(clean_text(payload.get('batch_id')))
                return result
        return await as_actor(request,'review',perform)

    @app.get('/api/files/{file_id}/correction')
    def api_correction_details(file_id: str, request: Request):
        from backend.archive_correction import correction_details
        with store.lock, store.conn.immediate():
            return correction_details(store, policy(request, 'correct_archive'), file_id)

    @app.post('/api/files/{file_id}/correction')
    async def api_correct_archive(file_id: str, request: Request, payload: dict = Body(...)):
        from backend.archive_correction import correct_archive
        payload = await _validated_payload(request, payload)
        def perform(access):
            result = correct_archive(store, access, file_id, payload)
            result['manual_updates'] = manual_updates.enqueue_archive_correction(result, access.user)
            return result
        return await as_actor(request, 'correct_archive', perform)

    @app.get('/api/files/{file_id}/share')
    def api_share_file_details(file_id: str, request: Request):
        from backend.shared_files import share_details
        with store.lock, store.conn.immediate():
            return share_details(store, policy(request, 'commit'), file_id)

    @app.post('/api/files/{file_id}/share')
    async def api_share_file(file_id: str, request: Request, payload: dict = Body(...)):
        from backend.shared_files import share_file
        payload = await _validated_payload(request, payload)
        def perform(access):
            result = share_file(store, access, file_id, payload)
            if result.get('status') == 'committed':
                result['nas_jobs'] = nas.enqueue_commit_results(result, access.user)
                result['manual_updates'] = manual_updates.enqueue_commit(result, access.user)
            return result
        return await as_actor(request, 'commit', perform)

    @app.post('/api/records/{record_id:path}/classification')
    async def api_record_classification(record_id: str, request: Request, payload: dict = Body(...)):
        payload=await _validated_payload(request,payload)
        def perform(p):
            with store.conn.immediate():
                record=p.record(record_id)
                category=payload.get('project_category')
                rows=store.execute('SELECT * FROM records WHERE registrant=? AND product=?',(record['registrant'],record['product'])).fetchall()
                pending=store.execute("SELECT * FROM batch_items WHERE registrant=? AND product=? AND status!='committed'",(record['registrant'],record['product'])).fetchall()
                for row in list(rows)+list(pending):
                    p.require_resource(dict(row));p.require_resource(dict(row)|{'project_category':category})
                return store.update_project_classification(record_id,category,payload.get('expected_edit_token'))
        return await as_actor(request,'classify',perform)

    @app.post('/api/commit')
    async def api_commit(request: Request, payload: dict = Body(...)):
        payload=await _validated_payload(request,payload)
        def perform(p):
            batch_id=clean_text(payload.get('batch_id'));p.batch(batch_id,commit=True)
            initial_latest_only = payload.get('initial_latest_only', False)
            if not isinstance(initial_latest_only, bool):
                raise HTTPException(400, 'initial_latest_only必须是布尔值')
            restrict_new_subject = payload.get('restrict_new_subject', False)
            if not isinstance(restrict_new_subject, bool):
                raise HTTPException(400, 'restrict_new_subject必须是布尔值')
            if restrict_new_subject:
                check_archive_subjects(batch_id)
            result = store.commit_batch(batch_id, initial_latest_only=initial_latest_only)
            result['nas_jobs'] = nas.enqueue_commit_results(result, p.user)
            result['manual_updates'] = manual_updates.enqueue_commit(result, p.user)
            return result
        return await as_actor(request, 'commit', perform)

    @app.post('/api/exceptions/{exception_id:path}/resolve')
    async def api_resolve(exception_id: str, request: Request):
        def perform(p):
            row=store.execute('SELECT * FROM exceptions WHERE id=?',(exception_id,)).fetchone()
            if not row: raise HTTPException(404,'异常不存在或无权访问')
            if row['batch_id']: p.batch(row['batch_id'])
            elif row['record_id']: p.record(row['record_id'])
            elif not p.global_scope: raise HTTPException(404,'异常不存在或无权访问')
            return store.resolve_exception(exception_id)
        return await as_actor(request,'resolve',perform)

    @app.post('/api/notifications/read')
    async def api_notifications_read(request: Request):
        return await as_actor(request,'notify',lambda p:identity.mark_notifications_read(p.user))

    @app.post('/api/sync')
    async def api_sync(request: Request):
        return await as_actor(request,'sync',lambda p:store.sync_local())

    @app.post('/api/sync/confirm')
    async def api_sync_confirm(request: Request):
        return await as_actor(request,'sync',lambda p:store.confirm_sync())

    def require_local_file(file_id):
        job = store.execute("SELECT status FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        if job and job['status'] == 'synced':
            raise HTTPException(409, {'error': '此文件已同步到192，请使用个人192账号读取入口', 'code': 'NAS_READ_REQUIRED'})

    @app.get('/api/download/{file_id:path}')
    def api_download(file_id: str, request: Request):
        with store.lock:
            access=policy(request,'download');row=access.file(file_id)
            require_local_file(file_id)
            data=store.read_blob(row['blob_path'])
            if sha256_bytes(data)!=row['sha256']: raise HTTPException(409,'本地文件校验失败，拒绝下载')
            with store.acting_as(f"{access.user['display_name']} [{access.user['id']}]") , store.conn:
                store.add_log('下载文件',row['name'],f"下载{row['status']} {row['version']} · {row['name']}",f'DOWNLOAD-{uuid.uuid4().hex[:8]}','成功')
            return Response(content=data,media_type='application/octet-stream',headers={'Content-Disposition':f"attachment; filename*=UTF-8''{quote(row['name'])}",'ETag':row['sha256']})

    @app.get('/api/preview/{file_id:path}')
    def api_preview(file_id: str, request: Request):
        with store.lock:
            access=policy(request,'preview');row=access.file(file_id)
            require_local_file(file_id)
            data=store.read_blob(row['blob_path'])
            if sha256_bytes(data)!=row['sha256']: raise HTTPException(409,'本地文件校验失败，拒绝预览')
            with store.acting_as(f"{access.user['display_name']} [{access.user['id']}]") , store.conn:
                store.add_log('预览文件',row['name'],f"预览{row['status']} {row['version']} · {row['name']}",f'PREVIEW-{uuid.uuid4().hex[:8]}','成功')
            name=row['name'];ext=Path(name).suffix.lower()
            if ext=='.pdf': return Response(content=data,media_type='application/pdf',headers={'Content-Disposition':'inline'})
            if ext in {'.xlsx','.xls'}: return HTMLResponse(preview_xlsx(data,name),headers={'Content-Disposition':'inline'})
            sample=data[:4096]
            try: value=sample.decode('utf-8')
            except UnicodeDecodeError: value=sample.hex(' ',1)
            return PlainTextResponse(f"{name}\n类型: {file_type(name)}\n大小: {size_label(len(data))}\nSHA256: {row['sha256']}\n\n{value}",headers={'Content-Disposition':'inline'})

    @app.get('/favicon.ico')
    def favicon(): return Response(status_code=204)

    @app.get('/{path:path}')
    def static_app(path: str):
        if path.startswith(('.runtime/','data/','logs/','backups/')) or path.startswith('.'):
            raise HTTPException(404,'路径不存在')
        if not config.static_root.exists(): raise HTTPException(503,'前端尚未构建：缺少 frontend/dist')
        target=(config.static_root/('index.html' if path in {'','/'} else path)).resolve()
        try: target.relative_to(config.static_root.resolve())
        except ValueError as exc: raise HTTPException(404,'路径不存在') from exc
        if not target.exists() or not target.is_file(): raise HTTPException(404,'路径不存在')
        return FileResponse(target)
    return app


class UvicornCompatServer:
    def __init__(self, app: FastAPI, port: int, bind_host="127.0.0.1", bound_socket=None):
        self.app = app
        self.socket = bound_socket or socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if bound_socket is None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((bind_host, port))
        self.socket.listen(2048)
        self.server_port = self.socket.getsockname()[1]
        config = uvicorn.Config(app, host=bind_host, port=self.server_port, log_level="warning", access_log=False, lifespan="off", proxy_headers=False)
        self.server = uvicorn.Server(config)

    def serve_forever(self) -> None:
        asyncio.run(self.server.serve(sockets=[self.socket]))

    def shutdown(self) -> None:
        self.server.should_exit = True

    def server_close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass
        if getattr(self, "runtime_lock", None):
            self.runtime_lock.close()
            self.runtime_lock = None


def build_server(port: int | None = None, data_dir: Path | None = None):
    global STORE
    config = load_config(data_dir)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    runtime_lock = acquire_runtime_lock(config.data_dir)
    try:
        STORE = DMRStore(config.data_dir, config.database_url)
    except Exception:
        runtime_lock.close()
        raise
    bind_port = int(os.environ.get("DMR_PORT", DEFAULT_PORT)) if port is None else port
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound.bind((config.bind_host, bind_port))
        actual_port = bound.getsockname()[1]
        changes = {'port': actual_port}
        if config.environment == 'local':
            if not os.environ.get('DMR_ALLOWED_HOSTS'):
                changes['allowed_hosts'] = ('127.0.0.1', 'localhost', f'127.0.0.1:{actual_port}', f'localhost:{actual_port}')
            if not os.environ.get('DMR_TRUSTED_ORIGINS'):
                changes['trusted_origins'] = ('http://127.0.0.1', 'http://localhost', f'http://127.0.0.1:{actual_port}', f'http://localhost:{actual_port}')
        config = replace(config, **changes)
        app = create_app(STORE, config)
        result = UvicornCompatServer(app, actual_port, config.bind_host, bound)
        result.runtime_lock = runtime_lock
        return result
    except Exception:
        bound.close()
        STORE.close()
        runtime_lock.close()
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run local DMR FastAPI backend")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DMR_PORT", DEFAULT_PORT)))
    parser.add_argument("--open", action="store_true", help="Open local app in the default browser")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else None
    server = build_server(args.port, data_dir)
    url = CONFIG.public_origin + "/" if CONFIG and CONFIG.public_origin else f"http://127.0.0.1:{server.server_port}/"
    print(f"DMR FastAPI backend running at {url}", flush=True)
    if args.open:
        webbrowser.open(url)

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        if STORE:
            STORE.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
