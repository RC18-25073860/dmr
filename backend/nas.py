
"""Local NAS handoff and personal 192-account sync contracts.

This module deliberately does not talk to a real NAS. It persists encrypted
personal credentials, directory mappings, sync jobs, leases, and short-lived
read tickets so the DMR app can be wired to an internal worker later.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from backend.access import AccessPolicy
from backend.service import clean_filename, clean_text, json_dumps, sha256_bytes

COOKIE_NAME = "dmr_session"
SYNC_TOKEN_ENV = "DMR_NAS_SYNC_TOKEN"
READ_BASE_URL_ENV = "DMR_NAS_READ_BASE_URL"
FERNET_KEY_ENV = "DMR_NAS_CREDENTIALS_KEY"
LEASE_MINUTES = 2
READ_TICKET_MINUTES = 2
STATUS_PENDING = "pending"
STATUS_TRANSFERRING = "transferring"
STATUS_BLOCKED = "blocked"
STATUS_SYNCED = "synced"
DIR_STATUS_PENDING = "pending"
DIR_STATUS_CREATING = "creating"
DIR_STATUS_SUCCEEDED = "succeeded"
DIR_STATUS_FAILED = "failed"

_ERROR_MESSAGES = {
    "network": "网络连接失败，稍后自动重试",
    "auth": "个人192账号登录失败，请检查密码、锁定或验证要求",
    "permission": "个人192账号无目录或File Station操作权限",
    "disk": "NAS空间不足",
    "conflict": "目标正式文件冲突",
    "protocol": "NAS接口响应异常",
    "credentials": "个人192账号配置已变更或不可用",
}


def utcish_now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _time_text(value: datetime | None = None) -> str:
    return (value or utcish_now()).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _future(minutes: int = LEASE_MINUTES) -> str:
    return _time_text(utcish_now() + timedelta(minutes=minutes))


def _live(value: str | None) -> bool:
    parsed = _parse_time(value)
    return bool(parsed and parsed > utcish_now())


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_error(error_code: str, diagnostic: dict[str, Any] | None = None) -> str:
    base = _ERROR_MESSAGES.get(error_code, _ERROR_MESSAGES["protocol"])
    if not diagnostic:
        return base
    parts = [base]
    api = clean_text(diagnostic.get("api"))[:80]
    method = clean_text(diagnostic.get("method"))[:40]
    if api:
        parts.append(f"{api}.{method}" if method else api)
    for key, label in (("api_version", "API v"), ("nas_code", "NAS错误码 "), ("http_status", "HTTP ")):
        value = diagnostic.get(key)
        if value is not None:
            parts.append(f"{label}{value}")
    return " · ".join(parts)[:240]


def _sync_token() -> str:
    return os.environ.get(SYNC_TOKEN_ENV, "").strip()


def _read_base_url() -> str:
    return os.environ.get(READ_BASE_URL_ENV, "").strip()


def storage_service_configured() -> bool:
    return bool(_sync_token()) and _read_base_url().startswith("https://")


def is_storage_sync_request(request: Request) -> bool:
    """Return true for valid worker calls that may be allowed without Origin.

    The main app middleware should still reject bad Origin values. This helper is
    only for the production case where an internal worker POST has no browser
    Origin and authenticates with the service bearer token.
    """
    if not request.url.path.startswith("/api/storage-sync/"):
        return False
    token = _sync_token()
    if not token:
        return False
    header = request.headers.get("authorization", "")
    return hmac.compare_digest(header.encode("utf-8"), f"Bearer {token}".encode("utf-8"))


class CredentialCrypto:
    def __init__(self, store, config=None):
        self.store = store
        self.config = config

    def _fernet(self):
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail="缺少cryptography依赖，不能保存192账号密码") from exc
        key = os.environ.get(FERNET_KEY_ENV, "").strip()
        if not key and self.config is not None and getattr(self.config, "environment", "local") in {"prod", "production"}:
            raise HTTPException(status_code=503, detail="生产环境必须配置DMR_NAS_CREDENTIALS_KEY")
        if not key:
            key_path = (self.store.data_dir / "nas-credentials.key").resolve()
            try:
                key_path.relative_to(self.store.data_dir.resolve())
            except ValueError as exc:
                raise HTTPException(status_code=503, detail="192账号密钥路径必须位于本地数据目录内") from exc
            if key_path.exists():
                key = key_path.read_text(encoding="ascii").strip()
            else:
                key = Fernet.generate_key().decode("ascii")
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(key)
        try:
            return Fernet(key.encode("ascii") if isinstance(key, str) else key), InvalidToken
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail="192账号加密密钥无效") from exc

    def encrypt(self, password: str) -> str:
        fernet, _invalid = self._fernet()
        return fernet.encrypt(password.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        fernet, invalid = self._fernet()
        try:
            return fernet.decrypt(encrypted.encode("ascii")).decode("utf-8")
        except invalid as exc:
            raise HTTPException(status_code=409, detail="192账号密码无法解密，请重新配置") from exc


@dataclass
class NASService:
    store: Any
    identity: Any
    config: Any = None

    @property
    def crypto(self) -> CredentialCrypto:
        return CredentialCrypto(self.store, self.config)

    def user_account(self, user_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return {
                "account": "",
                "password_configured": False,
                "check_status": "unconfigured",
                "check_error": "",
                "checked_at": "",
                "revision": 0,
            }
        return {
            "account": row["account"],
            "password_configured": bool(row["password_encrypted"]),
            "check_status": row["check_status"],
            "check_error": row["check_error"] or "",
            "checked_at": row["checked_at"] or "",
            "revision": int(row["revision"] or 0),
        }

    def save_account(self, user_id: str, account: str, password: str | None, actor: dict[str, Any]) -> dict[str, Any]:
        account = clean_text(account, "192账号")[:200]
        if not account:
            raise HTTPException(status_code=422, detail="192账号不能为空")
        existing = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (user_id,)).fetchone()
        if existing and not password and account == existing["account"]:
            encrypted = existing["password_encrypted"]
        else:
            if not password:
                raise HTTPException(status_code=422, detail="首次配置或更换账号时必须填写密码")
            encrypted = self.crypto.encrypt(str(password))
        now = _time_text()
        revision = int(existing["revision"] if existing else 0) + 1
        if existing:
            self.store.execute(
                """UPDATE nas_credentials
                   SET account=?,password_encrypted=?,revision=?,check_status='unverified',check_error='',checked_at=NULL,
                       check_lease=NULL,check_until=NULL,updated_at=? WHERE user_id=?""",
                (account, encrypted, revision, now, user_id),
            )
        else:
            self.store.execute(
                """INSERT INTO nas_credentials(user_id,account,password_encrypted,revision,check_status,check_error,checked_at,check_lease,check_until,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, account, encrypted, revision, "unverified", "", None, None, None, now, now),
            )
        self._audit(actor, "save_nas_account", user_id, {"account": account, "password_configured": True})
        return self.user_account(user_id)

    def clear_account(self, user_id: str, actor: dict[str, Any]) -> dict[str, Any]:
        self.store.execute("DELETE FROM nas_credentials WHERE user_id=?", (user_id,))
        self.store.execute(
            """UPDATE nas_sync_jobs
               SET status='blocked',lease_token=NULL,lease_until=NULL,last_error='个人192账号已清除，请重新配置后由管理员重试',updated_at=?
               WHERE owner_user_id=? AND status IN ('pending','transferring')""",
            (_time_text(), user_id),
        )
        self._audit(actor, "clear_nas_account", user_id, {"blocked_active_jobs": True})
        return self.user_account(user_id)

    def request_account_check(self, user_id: str, actor: dict[str, Any]) -> dict[str, Any]:
        if not storage_service_configured():
            raise HTTPException(status_code=503, detail="尚未配置内网同步服务地址和服务令牌，不能显示验证成功")
        row = self._credential_row(user_id)
        now = _time_text()
        self.store.execute(
            """UPDATE nas_credentials SET revision=revision+1,check_status='pending',check_error='',checked_at=NULL,
                   check_lease=NULL,check_until=NULL,updated_at=? WHERE user_id=?""",
            (now, user_id),
        )
        self._audit(actor, "request_nas_account_check", user_id, {"account": row["account"]})
        return self.user_account(user_id)

    def upsert_mapping(self, record_id: str, nas_path: str, actor: dict[str, Any]) -> dict[str, Any]:
        record = self.store.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not record:
            raise HTTPException(status_code=404, detail="记录不存在")
        path = self._validate_nas_path(nas_path)
        existing = self.store.execute("SELECT * FROM nas_directory_mappings WHERE record_id=?", (record_id,)).fetchone()
        now = _time_text()
        if existing:
            self.store.execute(
                "UPDATE nas_directory_mappings SET nas_path=?,active=1,updated_by=?,updated_at=? WHERE record_id=?",
                (path, actor["id"], now, record_id),
            )
        else:
            self.store.execute(
                """INSERT INTO nas_directory_mappings(id,record_id,nas_path,active,created_by,updated_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (f"NASMAP-{uuid.uuid4().hex}", record_id, path, 1, actor["id"], actor["id"], now, now),
            )
        self._audit(actor, "save_nas_mapping", record_id, {"nas_path": path})
        return self.mapping_for_record(record_id)

    def delete_mapping(self, record_id: str, actor: dict[str, Any]) -> dict[str, Any]:
        self.store.execute("UPDATE nas_directory_mappings SET active=0,updated_by=?,updated_at=? WHERE record_id=?", (actor["id"], _time_text(), record_id))
        self._audit(actor, "disable_nas_mapping", record_id, {})
        return {"ok": True}

    def list_mappings(self) -> list[dict[str, Any]]:
        rows = self.store.execute(
            """SELECT nas_directory_mappings.*, records.business_code, records.registrant, records.project_category, records.product, records.module
               FROM nas_directory_mappings JOIN records ON records.id=nas_directory_mappings.record_id
               ORDER BY nas_directory_mappings.updated_at DESC"""
        ).fetchall()
        return [self._mapping_payload(row) for row in rows]

    def mapping_for_record(self, record_id: str) -> dict[str, Any]:
        row = self.store.execute(
            """SELECT nas_directory_mappings.*, records.business_code, records.registrant, records.project_category, records.product, records.module
               FROM nas_directory_mappings JOIN records ON records.id=nas_directory_mappings.record_id
               WHERE nas_directory_mappings.record_id=?""",
            (record_id,),
        ).fetchone()
        if not row:
            return {"record_id": record_id, "nas_path": "", "active": False}
        return self._mapping_payload(row)

    def enqueue_file(self, file_id: str, actor: dict[str, Any] | None = None) -> dict[str, Any]:
        file_row = self.store.file_with_record(file_id)
        if not file_row:
            raise HTTPException(status_code=404, detail="文件不存在")
        existing = self.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        if existing:
            return self._job_payload(existing)
        batch = self.store.execute("SELECT * FROM batches WHERE id=?", (file_row["source"],)).fetchone()
        owner_id = batch["owner_user_id"] if batch and batch["owner_user_id"] else ""
        owner = self.store.execute("SELECT * FROM identity_users WHERE id=?", (owner_id,)).fetchone() if owner_id else None
        account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (owner_id,)).fetchone() if owner_id else None
        mapping = self.store.execute("SELECT * FROM nas_directory_mappings WHERE record_id=? AND active=1", (file_row["record_id"],)).fetchone()
        status_text = STATUS_PENDING
        error = ""
        target_name = self._target_name(file_row)
        target_path = ""
        revision = None
        account_snapshot = ""
        if not mapping:
            status_text = STATUS_BLOCKED
            error = "缺少NAS目录映射，已保留本地归档，等待配置正式目录后重试"
        else:
            target_path = f"{mapping['nas_path'].rstrip('/')}/{target_name}"
        if not owner:
            status_text = STATUS_BLOCKED
            error = "缺少原始上传人，无法按个人192账号同步"
        elif not account or not account["password_encrypted"] or not account["account"]:
            status_text = STATUS_BLOCKED
            error = "原始上传人未配置个人192账号"
        elif account["check_status"] == "failed":
            status_text = STATUS_BLOCKED
            error = account["check_error"] or "原始上传人192账号验证失败"
        if account:
            revision = int(account["revision"] or 0)
            account_snapshot = account["account"]
        now = _time_text()
        job_id = f"NASJOB-{uuid.uuid4().hex}"
        self.store.execute(
            """INSERT INTO nas_sync_jobs(id,file_id,record_id,owner_user_id,owner_display_name,owner_nas_account_snapshot,
                   credential_revision,target_path,target_name,status,attempts,next_attempt_at,lease_token,lease_until,last_heartbeat_at,
                   synced_at,last_error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, file_id, file_row["record_id"], owner_id or "", owner["display_name"] if owner else "", account_snapshot,
                revision, target_path, target_name, status_text, 0, now, None, None, None, None, error, now, now,
            ),
        )
        self.store.add_log("NAS同步入队", file_row["name"], error or "等待内网同步服务领取", file_row["source"] or job_id, "待处理" if error else "成功")
        return self._job_payload(self.store.execute("SELECT * FROM nas_sync_jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue_commit_results(self, commit_result: dict[str, Any], actor: dict[str, Any]) -> list[dict[str, Any]]:
        jobs = []
        for item in commit_result.get("results", []):
            if item.get("status") == "committed" and item.get("file_id"):
                jobs.append(self.enqueue_file(item["file_id"], actor))
        return jobs

    def claim_jobs(self, limit: int = 2) -> list[dict[str, Any]]:
        self._require_service_configured()
        limit = max(1, min(int(limit or 2), 2))
        now = _time_text()
        self.store.execute(
            """UPDATE nas_sync_jobs SET status='pending',lease_token=NULL,lease_until=NULL,last_error='上次租约过期，等待重新领取',updated_at=?
               WHERE status='transferring' AND lease_until IS NOT NULL AND lease_until<?""",
            (now, now),
        )
        rows = self.store.execute(
            """SELECT * FROM nas_sync_jobs WHERE status='pending' AND next_attempt_at<=?
               ORDER BY next_attempt_at,created_at LIMIT ?""",
            (now, limit * 5),
        ).fetchall()
        jobs = []
        busy_users = {row["owner_user_id"] for row in self.store.execute("SELECT owner_user_id FROM nas_sync_jobs WHERE status='transferring' AND lease_until>?", (now,)).fetchall()}
        for row in rows:
            if row["owner_user_id"] in busy_users:
                continue
            mapping = self.store.execute("SELECT id FROM nas_directory_mappings WHERE record_id=? AND active=1", (row["record_id"],)).fetchone()
            if not mapping:
                self._block_job(row["id"], "NAS目录映射缺失或已停用，请重新配置后由管理员重试")
                continue
            user, account = self._job_account(row)
            if not user or not account:
                self._block_job(row["id"], "个人192账号配置不可用，请处理后重试")
                continue
            lease = str(uuid.uuid4())
            until = _future()
            self.store.execute(
                """UPDATE nas_sync_jobs SET status='transferring',attempts=attempts+1,lease_token=?,lease_until=?,last_heartbeat_at=?,updated_at=?,last_error='' WHERE id=?""",
                (lease, until, now, now, row["id"]),
            )
            fresh = self.store.execute("SELECT * FROM nas_sync_jobs WHERE id=?", (row["id"],)).fetchone()
            transfer = self._file_transfer_metadata(fresh["file_id"])
            jobs.append({
                "id": fresh["id"],
                "file_id": fresh["file_id"],
                "name": fresh["target_name"],
                "target_path": fresh["target_path"],
                "lease_token": lease,
                "owner_user_id": fresh["owner_user_id"],
                "size": transfer["size"],
                "sha256": transfer["sha256"],
            })
            busy_users.add(fresh["owner_user_id"])
            if len(jobs) >= limit:
                break
        self._touch_agent()
        return jobs

    def file_credentials(self, file_id: str, lease_token: str) -> dict[str, str]:
        row = self._owned_job(file_id, lease_token)
        _user, account = self._job_account(row)
        if not account:
            raise HTTPException(status_code=409, detail="个人192账号配置不可用，请处理后重试")
        password = self.crypto.decrypt(account["password_encrypted"])
        self.store.execute("UPDATE nas_sync_jobs SET credential_revision=?,updated_at=? WHERE id=?", (account["revision"], _time_text(), row["id"]))
        self._audit({"id": "storage-sync", "display_name": "内网同步服务"}, "worker_read_nas_credentials", row["owner_user_id"], {"file_id": file_id, "account": account["account"]})
        return {"account": account["account"], "password": password}

    def file_content_response(self, file_id: str, lease_token: str) -> FileResponse:
        row = self._owned_job(file_id, lease_token)
        _user, account = self._job_account(row)
        if not account or row["credential_revision"] != account["revision"]:
            raise HTTPException(status_code=409, detail="个人192账号配置已变更，请重新领取任务")
        file_row = self.store.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not file_row:
            raise HTTPException(status_code=404, detail="文件不存在")
        data = self.store.read_blob(file_row["blob_path"])
        if sha256_bytes(data) != file_row["sha256"]:
            raise HTTPException(status_code=409, detail="本地文件校验失败，拒绝同步")
        path = (self.store.data_dir / file_row["blob_path"]).resolve()
        return FileResponse(path, media_type="application/octet-stream", filename=file_row["name"], headers={"Cache-Control": "no-store", "X-NAS-Job": row["id"]})

    def heartbeat(self, file_id: str, lease_token: str) -> dict[str, Any]:
        row = self._owned_job(file_id, lease_token)
        _user, account = self._job_account(row)
        if not account or row["credential_revision"] != account["revision"]:
            raise HTTPException(status_code=409, detail="个人192账号配置已变更，请重新领取任务")
        now = _time_text()
        self.store.execute("UPDATE nas_sync_jobs SET lease_until=?,last_heartbeat_at=?,updated_at=? WHERE id=?", (_future(), now, now, row["id"]))
        self._touch_agent()
        return {"ok": True}

    def result(self, file_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        lease = clean_text(payload.get("lease_token"))
        row = self.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="同步任务不存在")
        file_row = self.store.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()
        if not file_row:
            raise HTTPException(status_code=404, detail="文件不存在")
        if row["status"] == STATUS_SYNCED:
            if payload.get("success") is True and payload.get("target_path") == row["target_path"] and payload.get("sha256") == file_row["sha256"]:
                return {"ok": True, "status": STATUS_SYNCED}
            raise HTTPException(status_code=409, detail="同步结果与已完成任务不一致")
        if row["status"] != STATUS_TRANSFERRING or row["lease_token"] != lease or not _live(row["lease_until"]):
            raise HTTPException(status_code=409, detail="领取已失效")
        now = _time_text()
        if payload.get("success") is True:
            if not file_row:
                raise HTTPException(status_code=404, detail="文件不存在")
            if payload.get("target_path") != row["target_path"] or payload.get("sha256") != file_row["sha256"]:
                raise HTTPException(status_code=409, detail="同步结果与领取任务不一致，拒绝标记成功")
            _user, account = self._job_account(row)
            if not account or row["credential_revision"] != account["revision"]:
                raise HTTPException(status_code=409, detail="同步期间192账号已变更，请重新领取任务")
            self.store.execute(
                """UPDATE nas_sync_jobs SET status='synced',synced_at=?,lease_token=NULL,lease_until=NULL,last_error='',updated_at=? WHERE id=?""",
                (now, now, row["id"]),
            )
            self.store.execute("UPDATE files SET storage='本地保存 + NAS已同步' WHERE id=?", (file_id,))
            self._audit({"id": "storage-sync", "display_name": "内网同步服务"}, "nas_sync_result", row["owner_user_id"], {"file_id": file_id, "success": True, "target_path": row["target_path"]})
            return {"ok": True, "status": STATUS_SYNCED}
        error_code = clean_text(payload.get("error_code"), "protocol")[:40]
        message = _safe_error(error_code, payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else None)
        if error_code == "network":
            delay = [10, 30, 120, 300][min(max(int(row["attempts"] or 1) - 1, 0), 3)]
            next_attempt = _time_text(utcish_now() + timedelta(seconds=delay))
            self.store.execute(
                """UPDATE nas_sync_jobs SET status='pending',next_attempt_at=?,lease_token=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE id=?""",
                (next_attempt, message, now, row["id"]),
            )
        else:
            self.store.execute(
                """UPDATE nas_sync_jobs SET status='blocked',lease_token=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE id=?""",
                (message, now, row["id"]),
            )
            if error_code == "auth" and row["owner_user_id"]:
                account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (row["owner_user_id"],)).fetchone()
                if account and row["credential_revision"] == account["revision"]:
                    self.store.execute("UPDATE nas_credentials SET check_status='failed',check_error=?,checked_at=?,updated_at=? WHERE user_id=?", (message, now, now, row["owner_user_id"]))
        final_status = self.store.execute("SELECT status FROM nas_sync_jobs WHERE id=?", (row["id"],)).fetchone()["status"]
        self._audit({"id": "storage-sync", "display_name": "内网同步服务"}, "nas_sync_result", row["owner_user_id"], {"file_id": file_id, "success": False, "status": final_status, "error": message})
        return {"ok": True, "status": final_status}

    def retry_job(self, file_id: str, payload: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        if not row or row["status"] != STATUS_BLOCKED:
            raise HTTPException(status_code=409, detail="只能重试异常同步任务")
        user, account = self._job_account(row, allow_account_change=True)
        if not user or not account:
            raise HTTPException(status_code=409, detail="个人192账号配置不可用，请处理后重试")
        changed = account["account"] != row["owner_nas_account_snapshot"]
        if changed and not payload.get("confirm_account_change"):
            raise HTTPException(status_code=409, detail="账号名已变化，请确认本文件改用当前账号后再重试")
        mapping = self.store.execute("SELECT * FROM nas_directory_mappings WHERE record_id=? AND active=1", (row["record_id"],)).fetchone()
        if not mapping:
            raise HTTPException(status_code=409, detail="缺少NAS目录映射，不能重试")
        target_name = row["target_name"] or self._target_name(self.store.file_with_record(file_id))
        target_path = row["target_path"] or f"{mapping['nas_path'].rstrip('/')}/{target_name}"
        now = _time_text()
        self.store.execute(
            """UPDATE nas_sync_jobs SET status='pending',owner_nas_account_snapshot=?,credential_revision=?,target_path=?,target_name=?,
                   next_attempt_at=?,lease_token=NULL,lease_until=NULL,last_error='',updated_at=? WHERE id=?""",
            (account["account"], account["revision"], target_path, target_name, now, now, row["id"]),
        )
        self._audit(actor, "retry_nas_sync", file_id, {"account_changed": changed})
        return self._job_payload(self.store.execute("SELECT * FROM nas_sync_jobs WHERE id=?", (row["id"],)).fetchone())

    def overview(self) -> dict[str, Any]:
        rows = self.store.execute("SELECT * FROM nas_sync_jobs ORDER BY updated_at DESC LIMIT 200").fetchall()
        agent = self.store.execute("SELECT * FROM nas_agents WHERE id='primary'").fetchone()
        return {
            "configured": storage_service_configured(),
            "read_base_url": _read_base_url() if _read_base_url().startswith("https://") else "",
            "online": bool(storage_service_configured() and agent and _parse_time(agent["last_seen_at"]) and _parse_time(agent["last_seen_at"]) > utcish_now() - timedelta(minutes=7)),
            "last_seen_at": agent["last_seen_at"] if agent else "",
            "jobs": [self._job_payload(row) for row in rows],
        }

    def list_directory_creation_jobs(self) -> list[dict[str, Any]]:
        rows = self.store.execute("SELECT * FROM nas_directory_creation_jobs ORDER BY updated_at DESC LIMIT 200").fetchall()
        return [self._directory_job_payload(row) for row in rows]

    def request_directory_creation(self, record_id: str, parent_path: str, folder_name: str, actor: dict[str, Any]) -> dict[str, Any]:
        self._require_service_configured()
        record = self.store.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not record:
            raise HTTPException(status_code=404, detail="记录不存在")
        account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (actor["id"],)).fetchone()
        if not account or not account["account"] or not account["password_encrypted"]:
            raise HTTPException(status_code=409, detail="当前管理员未配置个人192账号，不能新建192目录")
        if account["check_status"] == "failed":
            raise HTTPException(status_code=409, detail=account["check_error"] or "当前管理员192账号验证失败，不能新建192目录")
        parent = self._validate_nas_path(parent_path)
        name = self._validate_folder_name(folder_name)
        target = self._validate_nas_path(f"{parent.rstrip('/')}/{name}")
        record_snapshot = self._record_snapshot(record)
        mapping_snapshot = self._mapping_snapshot(record_id)
        active = self.store.execute("SELECT * FROM nas_directory_mappings WHERE record_id=? AND active=1", (record_id,)).fetchone()
        if active:
            existing = self.store.execute(
                "SELECT * FROM nas_directory_creation_jobs WHERE record_id=? AND nas_path=? AND status='succeeded' ORDER BY updated_at DESC LIMIT 1",
                (record_id, target),
            ).fetchone()
            if active["nas_path"] == target and existing and existing["record_snapshot"] == record_snapshot and existing["mapping_snapshot"] == mapping_snapshot:
                return self._directory_job_payload(existing)
            raise HTTPException(status_code=409, detail="该记录已绑定192目录，如需更换请先停用原映射")
        existing = self.store.execute(
            """SELECT * FROM nas_directory_creation_jobs
               WHERE record_id=? AND nas_path=? AND status IN ('pending','creating','succeeded')
               ORDER BY updated_at DESC LIMIT 1""",
            (record_id, target),
        ).fetchone()
        if existing:
            if (
                existing["record_snapshot"] == record_snapshot
                and existing["mapping_snapshot"] == mapping_snapshot
                and existing["requested_by"] == actor["id"]
                and int(existing["credential_revision"] or 0) == int(account["revision"] or 0)
            ):
                return self._directory_job_payload(existing)
            self._fail_directory_job(existing["id"], "记录或目录映射已变化，请重新提交创建")
        now = _time_text()
        job_id = f"NASDIR-{uuid.uuid4().hex}"
        self.store.execute(
            """INSERT INTO nas_directory_creation_jobs(id,record_id,parent_path,folder_name,nas_path,record_snapshot,mapping_snapshot,requested_by,requested_display_name,
                   nas_account_snapshot,credential_revision,status,lease_token,lease_until,last_error,completed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, record_id, parent, name, target, record_snapshot, mapping_snapshot, actor["id"], actor.get("display_name") or actor["id"],
                account["account"], int(account["revision"] or 0), DIR_STATUS_PENDING, None, None, "", None, now, now,
            ),
        )
        self._audit(actor, "request_nas_directory_creation", record_id, {"parent_path": parent, "folder_name": name, "nas_path": target, "account": account["account"]})
        return self._directory_job_payload(self.store.execute("SELECT * FROM nas_directory_creation_jobs WHERE id=?", (job_id,)).fetchone())

    def claim_directory_creation_jobs(self, limit: int = 1) -> list[dict[str, Any]]:
        self._require_service_configured()
        limit = max(1, min(int(limit or 1), 2))
        now = _time_text()
        self.store.execute(
            """UPDATE nas_directory_creation_jobs
               SET status='pending',lease_token=NULL,lease_until=NULL,last_error='上次租约过期，等待重新领取',updated_at=?
               WHERE status='creating' AND lease_until IS NOT NULL AND lease_until<?""",
            (now, now),
        )
        rows = self.store.execute(
            """SELECT * FROM nas_directory_creation_jobs WHERE status='pending'
               ORDER BY created_at LIMIT ?""",
            (limit * 5,),
        ).fetchall()
        jobs = []
        busy_users = {
            row["requested_by"]
            for row in self.store.execute("SELECT requested_by FROM nas_directory_creation_jobs WHERE status='creating' AND lease_until>?", (now,)).fetchall()
        }
        for row in rows:
            if row["requested_by"] in busy_users:
                continue
            record = self.store.execute("SELECT id FROM records WHERE id=?", (row["record_id"],)).fetchone()
            if not record:
                self._fail_directory_job(row["id"], "记录已不存在，拒绝创建目录")
                continue
            if not self._directory_job_snapshots_current(row):
                self._fail_directory_job(row["id"], "记录或目录映射已变化，请重新提交创建")
                continue
            if not self._directory_job_actor_authorized(row):
                self._fail_directory_job(row["id"], "发起人已无权管理192目录，请重新提交创建")
                continue
            account = self._directory_job_account(row)
            if not account:
                self._fail_directory_job(row["id"], "个人192账号配置已变更或不可用，请重新提交创建")
                continue
            active = self.store.execute("SELECT nas_path FROM nas_directory_mappings WHERE record_id=? AND active=1", (row["record_id"],)).fetchone()
            if active and active["nas_path"] != row["nas_path"]:
                self._fail_directory_job(row["id"], "该记录已绑定其他192目录，拒绝覆盖")
                continue
            lease = str(uuid.uuid4())
            until = _future()
            self.store.execute(
                """UPDATE nas_directory_creation_jobs
                   SET status='creating',lease_token=?,lease_until=?,last_error='',updated_at=? WHERE id=?""",
                (lease, until, now, row["id"]),
            )
            fresh = self.store.execute("SELECT * FROM nas_directory_creation_jobs WHERE id=?", (row["id"],)).fetchone()
            jobs.append({
                "id": fresh["id"],
                "record_id": fresh["record_id"],
                "parent_path": fresh["parent_path"],
                "folder_name": fresh["folder_name"],
                "nas_path": fresh["nas_path"],
                "lease_token": lease,
                "requested_by": fresh["requested_by"],
            })
            busy_users.add(fresh["requested_by"])
            if len(jobs) >= limit:
                break
        self._touch_agent()
        return jobs

    def directory_creation_credentials(self, job_id: str, lease_token: str) -> dict[str, str]:
        row = self._owned_directory_job(job_id, lease_token)
        if not self._directory_job_snapshots_current(row):
            self._fail_directory_job(row["id"], "记录或目录映射已变化，请重新提交创建")
            return {"error": "failed", "detail": "记录或目录映射已变化，请重新提交创建"}
        if not self._directory_job_actor_authorized(row):
            self._fail_directory_job(row["id"], "发起人已无权管理192目录，请重新提交创建")
            return {"error": "failed", "detail": "发起人已无权管理192目录，请重新提交创建"}
        account = self._directory_job_account(row)
        if not account:
            self._fail_directory_job(row["id"], "个人192账号配置已变更或不可用，请重新提交创建")
            return {"error": "failed", "detail": "个人192账号配置已变更或不可用，请重新提交创建"}
        self._audit({"id": "storage-sync", "display_name": "内网同步服务"}, "worker_read_nas_credentials", row["requested_by"], {"directory_job_id": job_id, "account": account["account"]})
        return {"account": account["account"], "password": self.crypto.decrypt(account["password_encrypted"])}

    def directory_creation_result(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        lease = clean_text(payload.get("lease_token"))
        row = self.store.execute("SELECT * FROM nas_directory_creation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="目录创建任务不存在")
        if row["status"] == DIR_STATUS_SUCCEEDED:
            if payload.get("success") is True and payload.get("nas_path") == row["nas_path"]:
                return {"ok": True, "status": DIR_STATUS_SUCCEEDED}
            raise HTTPException(status_code=409, detail="创建结果与已完成任务不一致")
        if row["status"] != DIR_STATUS_CREATING or row["lease_token"] != lease or not _live(row["lease_until"]):
            raise HTTPException(status_code=409, detail="目录创建领取已失效")
        now = _time_text()
        if payload.get("success") is True:
            if payload.get("nas_path") != row["nas_path"]:
                raise HTTPException(status_code=409, detail="创建结果与领取任务不一致，拒绝绑定映射")
            if not self._directory_job_snapshots_current(row):
                self._fail_directory_job(row["id"], "记录或目录映射已变化，请重新提交创建")
                return {"ok": True, "status": DIR_STATUS_FAILED}
            if not self._directory_job_actor_authorized(row):
                self._fail_directory_job(row["id"], "发起人已无权管理192目录，请重新提交创建")
                return {"ok": True, "status": DIR_STATUS_FAILED}
            account = self._directory_job_account(row)
            if not account:
                self._fail_directory_job(row["id"], "目录创建期间192账号已变更，请重新提交创建")
                return {"ok": True, "status": DIR_STATUS_FAILED}
            active = self.store.execute("SELECT nas_path FROM nas_directory_mappings WHERE record_id=? AND active=1", (row["record_id"],)).fetchone()
            if active and active["nas_path"] != row["nas_path"]:
                self._fail_directory_job(row["id"], "该记录已绑定其他192目录，拒绝覆盖")
                return {"ok": True, "status": DIR_STATUS_FAILED}
            self.upsert_mapping(row["record_id"], row["nas_path"], {"id": "storage-sync", "display_name": "内网同步服务"})
            self.store.execute(
                """UPDATE nas_directory_creation_jobs
                   SET status='succeeded',lease_token=NULL,lease_until=NULL,last_error='',completed_at=?,updated_at=? WHERE id=?""",
                (now, now, row["id"]),
            )
            self._refresh_blocked_jobs_for_record(row["record_id"])
            self._audit({"id": "storage-sync", "display_name": "内网同步服务"}, "nas_directory_creation_result", row["requested_by"], {"job_id": row["id"], "success": True, "nas_path": row["nas_path"]})
            return {"ok": True, "status": DIR_STATUS_SUCCEEDED}
        error_code = clean_text(payload.get("error_code"), "protocol")[:40]
        message = _safe_error(error_code, payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else None)
        self.store.execute(
            """UPDATE nas_directory_creation_jobs
               SET status='failed',lease_token=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE id=?""",
            (message, now, row["id"]),
        )
        if error_code == "auth" and row["requested_by"]:
            account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (row["requested_by"],)).fetchone()
            if account and int(row["credential_revision"] or 0) == int(account["revision"] or 0):
                self.store.execute("UPDATE nas_credentials SET check_status='failed',check_error=?,checked_at=?,updated_at=? WHERE user_id=?", (message, now, now, row["requested_by"]))
        self._audit({"id": "storage-sync", "display_name": "内网同步服务"}, "nas_directory_creation_result", row["requested_by"], {"job_id": row["id"], "success": False, "error": message})
        return {"ok": True, "status": DIR_STATUS_FAILED}

    def create_read_ticket(self, file_id: str, actor: dict[str, Any], action: str = "download") -> dict[str, Any]:
        self._require_service_configured()
        action = clean_text(action, "download")
        if action not in {"preview", "download"}:
            raise HTTPException(status_code=422, detail="action只能是preview或download")
        job = self.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        if not job or job["status"] != STATUS_SYNCED:
            return {"mode": "local", "reason": "文件尚未同步到NAS，继续使用本地下载/预览"}
        account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (actor["id"],)).fetchone()
        if not account or not account["password_encrypted"]:
            raise HTTPException(status_code=409, detail="当前查看人未配置个人192账号，不能读取已同步NAS文件")
        if account["check_status"] == "failed":
            raise HTTPException(status_code=409, detail="当前查看人192账号验证失败，不能读取已同步NAS文件")
        raw = secrets.token_urlsafe(32)
        ticket_hash = _hash_token(raw)
        now = _time_text()
        expires = _future(READ_TICKET_MINUTES)
        self.store.execute(
            "INSERT INTO nas_read_tickets(ticket_hash,file_id,user_id,expires_at,redeemed_at,created_at) VALUES(?,?,?,?,NULL,?)",
            (ticket_hash, file_id, actor["id"], expires, now),
        )
        self.store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (f"nas_read_ticket_action:{ticket_hash}", action))
        return {"mode": "nas", "ticket": raw, "expires_at": expires, "read_base_url": _read_base_url(), "target_path": job["target_path"], "action": action}

    def authorize_read(self, ticket: str) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM nas_read_tickets WHERE ticket_hash=?", (_hash_token(ticket or ""),)).fetchone()
        if not row or row["redeemed_at"] or not _live(row["expires_at"]):
            raise HTTPException(status_code=409, detail="读取票据无效或已过期")
        user = self.store.execute("SELECT * FROM identity_users WHERE id=? AND active=1", (row["user_id"],)).fetchone()
        account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (row["user_id"],)).fetchone() if user else None
        job = self.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (row["file_id"],)).fetchone()
        action_row = self.store.execute("SELECT value FROM settings WHERE key=?", (f"nas_read_ticket_action:{row['ticket_hash']}",)).fetchone()
        action = action_row["value"] if action_row else "download"
        if not user or not account or account["check_status"] == "failed" or not job or job["status"] != STATUS_SYNCED:
            raise HTTPException(status_code=409, detail="NAS读取授权不可用")
        public_user = self._public_user_from_row(user)
        if public_user.get("must_change_password"):
            raise HTTPException(status_code=409, detail="NAS读取授权已失效")
        try:
            access = AccessPolicy(self.store, public_user)
            access.require("preview" if action == "preview" else "download")
            access.file(row["file_id"])
        except HTTPException as exc:
            raise HTTPException(status_code=409, detail="NAS读取授权已失效") from exc
        self.store.execute("UPDATE nas_read_tickets SET redeemed_at=? WHERE ticket_hash=?", (_time_text(), row["ticket_hash"]))
        self._audit({"id": "storage-sync", "display_name": "内网读取服务"}, "nas_read_redeem", row["user_id"], {"file_id": row["file_id"], "action": action, "target_path": job["target_path"]})
        file_row = self.store.execute("SELECT name,sha256,blob_path FROM files WHERE id=?", (row["file_id"] ,)).fetchone()
        transfer = self._file_transfer_metadata(row["file_id"])
        self.store.execute("DELETE FROM settings WHERE key=?", (f"nas_read_ticket_action:{row['ticket_hash']}",))
        return {
            "account": account["account"],
            "password": self.crypto.decrypt(account["password_encrypted"]),
            "target_path": job["target_path"],
            "path": job["target_path"],
            "file_id": row["file_id"],
            "name": file_row["name"] if file_row else "",
            "size": transfer["size"],
            "sha256": transfer["sha256"],
            "action": action,
        }

    def claim_account_checks(self, limit: int = 1) -> list[dict[str, Any]]:
        self._require_service_configured()
        limit = max(1, min(int(limit or 1), 2))
        now = _time_text()
        rows = self.store.execute(
            """SELECT * FROM nas_credentials WHERE check_status='pending' OR (check_status='checking' AND check_until<?)
               ORDER BY updated_at LIMIT ?""",
            (now, limit),
        ).fetchall()
        result = []
        for row in rows:
            lease = str(uuid.uuid4())
            until = _future()
            self.store.execute("UPDATE nas_credentials SET check_status='checking',check_lease=?,check_until=?,updated_at=? WHERE user_id=?", (lease, until, now, row["user_id"]))
            result.append({"user_id": row["user_id"], "account": row["account"], "lease_token": lease})
        self._touch_agent()
        return result

    def account_check_credentials(self, user_id: str, lease_token: str) -> dict[str, str]:
        row = self._credential_row(user_id)
        if row["check_lease"] != lease_token or not _live(row["check_until"]):
            raise HTTPException(status_code=409, detail="验证领取已失效")
        return {"account": row["account"], "password": self.crypto.decrypt(row["password_encrypted"])}

    def account_check_result(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._credential_row(user_id)
        lease = clean_text(payload.get("lease_token"))
        if row["check_lease"] != lease or not _live(row["check_until"]):
            raise HTTPException(status_code=409, detail="验证领取已失效")
        now = _time_text()
        if payload.get("success") is True:
            self.store.execute(
                "UPDATE nas_credentials SET check_status='verified',check_error='',checked_at=?,check_lease=NULL,check_until=NULL,updated_at=? WHERE user_id=?",
                (now, now, user_id),
            )
        else:
            message = _safe_error(clean_text(payload.get("error_code"), "protocol")[:40], payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else None)
            self.store.execute(
                "UPDATE nas_credentials SET check_status='failed',check_error=?,checked_at=?,check_lease=NULL,check_until=NULL,updated_at=? WHERE user_id=?",
                (message, now, now, user_id),
            )
        return self.user_account(user_id)

    def _credential_row(self, user_id: str):
        row = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (user_id,)).fetchone()
        if not row or not row["account"] or not row["password_encrypted"]:
            raise HTTPException(status_code=422, detail="该人员尚未配置192账号")
        return row

    def _validate_nas_path(self, path: str) -> str:
        path = clean_text(path, "NAS目录")[:500].replace("\\", "/")
        parts = path.strip("/").split("/")
        if not path.startswith("/") or path == "/" or "//" in path or any(part in {"", ".", ".."} for part in parts):
            raise HTTPException(status_code=422, detail="NAS目录必须是共享目录下的绝对路径，不能包含..")
        return path.rstrip("/")

    def _validate_folder_name(self, folder_name: str) -> str:
        name = clean_text(folder_name, "目录名称")[:160].strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise HTTPException(status_code=422, detail="目录名称不能为空，且不能包含路径分隔符")
        if any(ord(char) < 32 for char in name):
            raise HTTPException(status_code=422, detail="目录名称不能包含控制字符")
        return name

    def _target_name(self, file_row) -> str:
        sha = (file_row["sha256"] or "")[:12]
        return clean_filename(f"{file_row['id']}-{sha}-{file_row['name']}")

    def _file_transfer_metadata(self, file_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT sha256,blob_path FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        path = (self.store.data_dir / row["blob_path"]).resolve()
        try:
            path.relative_to(self.store.data_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="本地附件路径无效") from exc
        if not path.is_file():
            raise HTTPException(status_code=503, detail="本地附件不可用")
        return {"size": path.stat().st_size, "sha256": row["sha256"]}

    def _job_account(self, row, allow_account_change: bool = False):
        user = self.store.execute("SELECT * FROM identity_users WHERE id=? AND active=1", (row["owner_user_id"],)).fetchone()
        account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (row["owner_user_id"],)).fetchone() if user else None
        if not user or not account or not account["password_encrypted"] or account["check_status"] == "failed":
            return user, None
        if not self._user_can_access_file(user, row["file_id"]):
            return user, None
        if not allow_account_change and row["owner_nas_account_snapshot"] and account["account"] != row["owner_nas_account_snapshot"]:
            return user, None
        return user, account

    def _directory_job_account(self, row):
        user = self.store.execute("SELECT * FROM identity_users WHERE id=? AND active=1", (row["requested_by"],)).fetchone()
        account = self.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (row["requested_by"],)).fetchone() if user else None
        if not user or not account or not account["password_encrypted"] or account["check_status"] == "failed":
            return None
        if account["account"] != row["nas_account_snapshot"] or int(account["revision"] or 0) != int(row["credential_revision"] or 0):
            return None
        return account

    def _directory_job_actor_authorized(self, row) -> bool:
        user = self.store.execute("SELECT * FROM identity_users WHERE id=? AND active=1", (row["requested_by"],)).fetchone()
        if not user:
            return False
        try:
            public = self._public_user_from_row(user)
            if public.get("must_change_password"):
                return False
            AccessPolicy(self.store, public).require("manage_users")
            return True
        except HTTPException:
            return False

    def _record_snapshot(self, record) -> str:
        return json_dumps({
            "id": record["id"],
            "business_code": record["business_code"],
            "registrant": record["registrant"],
            "project_category": record["project_category"],
            "product": record["product"],
            "module": record["module"],
            "type": record["type"],
            "edit_revision": int(record["edit_revision"] or 0),
        })

    def _mapping_snapshot(self, record_id: str) -> str:
        row = self.store.execute("SELECT * FROM nas_directory_mappings WHERE record_id=?", (record_id,)).fetchone()
        if not row:
            return ""
        return json_dumps({
            "id": row["id"],
            "nas_path": row["nas_path"],
            "active": int(row["active"] or 0),
            "updated_at": row["updated_at"] or "",
        })

    def _directory_job_snapshots_current(self, row) -> bool:
        record = self.store.execute("SELECT * FROM records WHERE id=?", (row["record_id"],)).fetchone()
        if not record:
            return False
        return row["record_snapshot"] == self._record_snapshot(record) and row["mapping_snapshot"] == self._mapping_snapshot(row["record_id"])

    def _public_user_from_row(self, row) -> dict[str, Any]:
        return self.identity._public_user(row)

    def _user_can_access_file(self, user_row, file_id: str) -> bool:
        try:
            AccessPolicy(self.store, self._public_user_from_row(user_row)).file(file_id)
            return True
        except HTTPException:
            return False

    def _owned_job(self, file_id: str, lease_token: str):
        row = self.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        if not row or row["lease_token"] != lease_token or not _live(row["lease_until"]):
            raise HTTPException(status_code=409, detail="领取已失效")
        return row

    def _owned_directory_job(self, job_id: str, lease_token: str):
        row = self.store.execute("SELECT * FROM nas_directory_creation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["lease_token"] != lease_token or not _live(row["lease_until"]):
            raise HTTPException(status_code=409, detail="目录创建领取已失效")
        return row

    def _block_job(self, job_id: str, message: str) -> None:
        self.store.execute("UPDATE nas_sync_jobs SET status='blocked',last_error=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?", (message, _time_text(), job_id))

    def _fail_directory_job(self, job_id: str, message: str) -> None:
        self.store.execute(
            "UPDATE nas_directory_creation_jobs SET status='failed',last_error=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
            (message, _time_text(), job_id),
        )

    def _refresh_blocked_jobs_for_record(self, record_id: str) -> None:
        rows = self.store.execute("SELECT * FROM nas_sync_jobs WHERE record_id=? AND status='blocked' AND last_error LIKE '缺少NAS目录映射%'", (record_id,)).fetchall()
        for row in rows:
            try:
                self.retry_job(row["file_id"], {}, {"id": "system", "display_name": "系统"})
            except HTTPException:
                continue

    def _touch_agent(self) -> None:
        now = _time_text()
        self.store.execute("INSERT OR REPLACE INTO nas_agents(id,last_seen_at) VALUES('primary',?)", (now,))

    def _mapping_payload(self, row) -> dict[str, Any]:
        return {
            "id": row["id"], "record_id": row["record_id"], "nas_path": row["nas_path"], "active": bool(row["active"]),
            "record_no": row["business_code"], "registrant": row["registrant"], "project_category": row["project_category"],
            "product": row["product"], "module": row["module"], "updated_at": row["updated_at"],
        }

    def _job_payload(self, row) -> dict[str, Any]:
        return {
            "id": row["id"], "file_id": row["file_id"], "record_id": row["record_id"], "owner_user_id": row["owner_user_id"],
            "owner_display_name": row["owner_display_name"], "account": row["owner_nas_account_snapshot"],
            "target_path": row["target_path"], "target_name": row["target_name"], "status": row["status"],
            "attempts": int(row["attempts"] or 0), "last_error": row["last_error"] or "", "synced_at": row["synced_at"] or "",
            "updated_at": row["updated_at"],
        }

    def _directory_job_payload(self, row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "record_id": row["record_id"],
            "parent_path": row["parent_path"],
            "folder_name": row["folder_name"],
            "nas_path": row["nas_path"],
            "status": row["status"],
            "last_error": row["last_error"] or "",
            "requested_by": row["requested_by"],
            "requested_display_name": row["requested_display_name"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"] or "",
        }

    def _require_service_configured(self) -> None:
        if not storage_service_configured():
            raise HTTPException(status_code=503, detail="请先配置内网HTTPS读取地址和同步服务令牌")

    def _audit(self, actor: dict[str, Any], action: str, target_id: str, detail: dict[str, Any]) -> None:
        self.store.execute(
            """INSERT INTO identity_audit(id,created_at,actor_user_id,actor_display_name,action,target_user_id,target_display_name,detail)
               VALUES(?,?,?,?,?,?,?,?)""",
            (f"AUD-{uuid.uuid4().hex}", _time_text(), actor.get("id"), actor.get("display_name"), action, target_id, target_id, json_dumps({k: v for k, v in detail.items() if "password" not in k})),
        )


def _require_worker() -> None:
    token = _sync_token()
    if not token:
        raise HTTPException(status_code=503, detail="同步服务令牌未配置")


def _check_worker(request: Request) -> None:
    _require_worker()
    if not is_storage_sync_request(request):
        raise HTTPException(status_code=401, detail="同步身份无效")


def install_nas(app: FastAPI, store, identity, config=None) -> NASService:
    """Install NAS routes. Call before the SPA catch-all route is registered."""
    service = NASService(store, identity, config)
    app.state.nas = service

    def current_access(request: Request, permission: str | None = None, allow_password_change: bool = False) -> AccessPolicy:
        if config is not None and getattr(config, "auth_mode", "local") != "local":
            raise HTTPException(status_code=503, detail="企业身份未配置，拒绝执行受控操作")
        user = identity.current(request.cookies.get(COOKIE_NAME, ""))
        if user.get("must_change_password") and not allow_password_change:
            raise HTTPException(status_code=403, detail={"error": "请先修改初始密码，再使用资料功能", "code": "PASSWORD_CHANGE_REQUIRED"})
        access = AccessPolicy(store, user)
        if permission:
            access.require(permission)
        return access

    def admin_user(request: Request) -> dict[str, Any]:
        return current_access(request, "manage_users").user

    def read_tx(func):
        with store.lock, store.conn:
            return func()

    def write_tx(func):
        with store.lock:
            with store.conn.immediate():
                return func()

    @app.get("/api/storage-status")
    def storage_status(request: Request):
        def call():
            current_access(request, "read", allow_password_change=True)
            data = service.overview()
            return {k: data[k] for k in ("configured", "online", "last_seen_at", "read_base_url")}
        return read_tx(call)

    @app.get("/api/admin/storage-sync")
    def admin_storage_sync(request: Request):
        return read_tx(lambda: (admin_user(request), service.overview())[1])

    @app.get("/api/users/{user_id:path}/nas-account")
    def get_account(user_id: str, request: Request):
        return read_tx(lambda: (admin_user(request), service.user_account(user_id))[1])

    @app.put("/api/users/{user_id:path}/nas-account")
    async def put_account(user_id: str, request: Request, payload: dict = Body(...)):
        def call():
            actor = admin_user(request)
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="JSON请求体必须是对象")
            return service.save_account(user_id, payload.get("account"), payload.get("password"), actor)
        return write_tx(call)

    @app.delete("/api/users/{user_id:path}/nas-account")
    def delete_account(user_id: str, request: Request):
        return write_tx(lambda: service.clear_account(user_id, admin_user(request)))

    @app.post("/api/users/{user_id:path}/nas-account/check")
    def check_account(user_id: str, request: Request):
        return write_tx(lambda: service.request_account_check(user_id, admin_user(request)))

    @app.get("/api/nas/directory-mappings")
    def list_mappings(request: Request):
        return read_tx(lambda: (admin_user(request), {"mappings": service.list_mappings()})[1])

    @app.post("/api/nas/directory-mappings")
    async def save_mapping(request: Request, payload: dict = Body(...)):
        return write_tx(lambda: {"mapping": service.upsert_mapping(clean_text(payload.get("record_id")), payload.get("nas_path"), admin_user(request))})

    @app.delete("/api/nas/directory-mappings/{record_id:path}")
    def disable_mapping(record_id: str, request: Request):
        return write_tx(lambda: service.delete_mapping(record_id, admin_user(request)))

    @app.get("/api/nas/directory-creation-jobs")
    def list_directory_creation_jobs(request: Request):
        return read_tx(lambda: (admin_user(request), {"jobs": service.list_directory_creation_jobs()})[1])

    @app.post("/api/nas/directory-creation-jobs")
    async def create_directory_job(request: Request, payload: dict = Body(...)):
        def call():
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="JSON请求体必须是对象")
            actor = admin_user(request)
            return {"job": service.request_directory_creation(clean_text(payload.get("record_id")), payload.get("parent_path"), payload.get("folder_name"), actor)}
        return write_tx(call)

    @app.post("/api/admin/storage-sync/files/{file_id:path}/retry")
    async def retry(file_id: str, request: Request, payload: dict = Body(default_factory=dict)):
        return write_tx(lambda: {"job": service.retry_job(file_id, payload or {}, admin_user(request))})

    @app.post("/api/nas/files/{file_id:path}/read-ticket")
    async def read_ticket(file_id: str, request: Request, payload: dict = Body(default_factory=dict)):
        def call():
            action = clean_text((payload or {}).get("action"), "download")
            access = current_access(request, "preview" if action == "preview" else "download")
            access.file(file_id)
            return service.create_read_ticket(file_id, access.user, action)
        return write_tx(call)

    @app.post("/api/storage-sync/account-checks/claim")
    async def claim_account_checks(request: Request, payload: dict = Body(default_factory=dict)):
        return write_tx(lambda: (_check_worker(request), service.claim_account_checks((payload or {}).get("limit", 1)))[1])

    @app.post("/api/storage-sync/account-checks/{user_id:path}/credentials")
    async def account_credentials(user_id: str, request: Request, response: Response, payload: dict = Body(...)):
        response.headers["Cache-Control"] = "no-store"
        return read_tx(lambda: (_check_worker(request), service.account_check_credentials(user_id, clean_text(payload.get("lease_token"))))[1])

    @app.post("/api/storage-sync/account-checks/{user_id:path}/result")
    async def account_result(user_id: str, request: Request, payload: dict = Body(...)):
        return write_tx(lambda: (_check_worker(request), service.account_check_result(user_id, payload))[1])

    @app.post("/api/storage-sync/directory-creation-jobs/claim")
    async def claim_directory_creation_jobs(request: Request, payload: dict = Body(default_factory=dict)):
        return write_tx(lambda: (_check_worker(request), service.claim_directory_creation_jobs((payload or {}).get("limit", 1)))[1])

    @app.post("/api/storage-sync/directory-creation-jobs/{job_id:path}/credentials")
    async def directory_creation_credentials(job_id: str, request: Request, response: Response, payload: dict = Body(...)):
        response.headers["Cache-Control"] = "no-store"
        result = write_tx(lambda: (_check_worker(request), service.directory_creation_credentials(job_id, clean_text(payload.get("lease_token"))))[1])
        if result.get("error"):
            raise HTTPException(status_code=409, detail=result["detail"])
        return result

    @app.post("/api/storage-sync/directory-creation-jobs/{job_id:path}/result")
    async def directory_creation_result(job_id: str, request: Request, payload: dict = Body(...)):
        return write_tx(lambda: (_check_worker(request), service.directory_creation_result(job_id, payload))[1])

    @app.post("/api/storage-sync/claim")
    async def claim_jobs(request: Request, payload: dict = Body(default_factory=dict)):
        return write_tx(lambda: (_check_worker(request), service.claim_jobs((payload or {}).get("limit", 2)))[1])

    @app.post("/api/storage-sync/files/{file_id:path}/credentials")
    async def file_credentials(file_id: str, request: Request, response: Response, payload: dict = Body(...)):
        response.headers["Cache-Control"] = "no-store"
        return write_tx(lambda: (_check_worker(request), service.file_credentials(file_id, clean_text(payload.get("lease_token"))))[1])

    @app.get("/api/storage-sync/files/{file_id:path}/content")
    def file_content(file_id: str, request: Request):
        return read_tx(lambda: (_check_worker(request), service.file_content_response(file_id, clean_text(request.headers.get("x-sync-lease"))))[1])

    @app.post("/api/storage-sync/files/{file_id:path}/heartbeat")
    async def file_heartbeat(file_id: str, request: Request, payload: dict = Body(...)):
        return write_tx(lambda: (_check_worker(request), service.heartbeat(file_id, clean_text(payload.get("lease_token"))))[1])

    @app.post("/api/storage-sync/files/{file_id:path}/result")
    async def file_result(file_id: str, request: Request, payload: dict = Body(...)):
        return write_tx(lambda: (_check_worker(request), service.result(file_id, payload))[1])

    @app.post("/api/storage-sync/read/authorize")
    async def authorize_read(request: Request, response: Response, payload: dict = Body(...)):
        response.headers["Cache-Control"] = "no-store"
        return write_tx(lambda: (_check_worker(request), service.authorize_read(clean_text(payload.get("ticket"))))[1])

    return service
