"""Server-injected event boundary; credentials and workers belong to trusted adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool


@dataclass(frozen=True)
class CatalogChange:
    event_id: str
    document_token: str
    change_type: str  # changed / created / deleted; normalized by verified adapter


class PlatformCatalogReader(Protocol):
    """Uses server-owned authorization; never the browser user's local CLI login."""
    def read_directory(self) -> dict[str, Any]: ...
    def read_document(self, document_token: str) -> dict[str, Any]: ...


class VerifiedEventReceiver(Protocol):
    """Authenticate raw bytes, reject replays outside the allowed time window,
    enforce tenant/document-root allowlists, then normalize a catalog event.
    Raise on invalid signatures, unsupported events or authorization failures.
    Browser credentials and fields in the event are never trusted identities.
    """
    def verify_and_decode(self, body: bytes, headers: dict[str, str]) -> CatalogChange: ...


class SnapshotSyncQueue(Protocol):
    """Durable idempotent queue; HTTP event receipt must not perform slow reads.

    enqueue must atomically persist before returning a nonempty job ID. Repeated
    event_id + identical payload returns the same ID, even after process restart;
    a conflicting payload must raise. Failures must permit safe retry, including
    a failure reported after commit. Workers retry failed processing separately.
    """
    def enqueue(self, change: CatalogChange) -> str: ...
    def reconcile(self) -> str: ...


class PersonalAuthorizationStore(Protocol):
    """Per-user encrypted grants, separate from platform catalog credentials."""
    def is_authorized(self, user_id: str, capability: str) -> bool: ...
    def revoke(self, user_id: str, capability: str) -> None: ...


def install_feishu_integration(
    app: FastAPI, policy, *,
    verifier: VerifiedEventReceiver | None = None,
    queue: SnapshotSyncQueue | None = None,
) -> None:
    # Explicit server bootstrap injection only; no public endpoint sets adapters.
    events_enabled = verifier is not None and queue is not None
    @app.get('/api/integrations/feishu/status')
    def integration_status(request: Request):
        access = policy(request, 'read')
        if not access.global_scope:
            raise HTTPException(403, '仅全局授权人员可查看平台接入状态')
        return {
            'status': 'events_configured' if events_enabled else 'not_connected',
            'platform_catalog': {'adapter': 'injected' if events_enabled else 'reserved', 'events_enabled': events_enabled, 'scheduled_sync_enabled': False},
            'personal_authorization': {'login': 'reserved', 'mail': 'reserved'},
            'cloud_writeback_enabled': False,
            'message': ('事件适配器已配置；接收成功仅表示已入队，不代表目录已同步。' if events_enabled
                        else '接口已预留，尚未接入服务器飞书授权；现有本地目录读取保持原方式。'),
        }

    @app.post('/api/integrations/feishu/events', status_code=202)
    async def receive_events(request: Request):
        if not events_enabled:
            raise HTTPException(503, '飞书事件接入尚未启用')
        body = await request.body()
        try:
            change = await run_in_threadpool(verifier.verify_and_decode, body, dict(request.headers))
            if (not isinstance(change, CatalogChange)
                    or any(not isinstance(value, str) or not value.strip()
                           for value in (change.event_id, change.document_token))
                    or change.change_type not in ('changed', 'created', 'deleted')):
                raise ValueError('Invalid normalized catalog change')
        except Exception:
            # Neither payloads nor adapter exception details are returned to callers.
            raise HTTPException(403, '飞书事件验证失败') from None
        try:
            job_id = await run_in_threadpool(queue.enqueue, change)
            if not isinstance(job_id, str) or not job_id.strip():
                raise ValueError('Queue did not confirm durable receipt')
        except Exception:
            # No local dedup marker: the sender can retry an uncertain receipt.
            raise HTTPException(503, '飞书事件暂未确认入队，请重试') from None
        return {'status': 'accepted', 'job_id': job_id}
