from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections import defaultdict, deque
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from backend.access import AccessPolicy
from backend.mail_title import apply_mail_title_evidence, classify_mail_subject, extract_mail_title
from backend.transfer_manifest import TransferManifestArchive
from backend.service import MAX_BODY, MAX_FILE_BYTES, clean_filename, json_dumps, json_loads, now_iso

MAIL_MACHINE_ROUTES = {
    ("POST", "/api/integrations/mail/attachments"),
    ("POST", "/api/integrations/mail/batches"),
    ("POST", "/api/integrations/mail/transfer-manifests"),
}
MAIL_RECEIPT_PREFIX = "/api/integrations/mail/receipts/"
MAIL_MAX_FILE_BYTES = min(MAX_FILE_BYTES, 50 * 1024 * 1024)
CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,78}[A-Za-z0-9]$")
MAILBOX_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")
OPAQUE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")
CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _parse_time(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(_dt.timezone.utc).replace(microsecond=0)


def _time_iso(value: _dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fingerprint(value: dict[str, Any]) -> str:
    material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _clean_required(value: str, field: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是文本")
    text = value.replace("\x00", "").strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    if len(text) > max_len:
        raise ValueError(f"{field}不能超过{max_len}个字符")
    return text


def _owner_from_identity(identity, owner_user_id: str, *, inactive_status: int = status.HTTP_400_BAD_REQUEST) -> dict[str, Any]:
    for user in identity.list_users():
        if user.get("id") == owner_user_id:
            if not user.get("active") or user.get("role") not in {"admin", "document_control"}:
                raise HTTPException(status_code=inactive_status, detail="owner_user_id必须指向启用的管理员或文控人员")
            return user
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="owner_user_id不存在")


def is_mail_ingest_request(request: Request) -> bool:
    method = request.method.upper()
    path = request.url.path
    if (method, path) in MAIL_MACHINE_ROUTES:
        return True
    return method == "GET" and path.startswith(MAIL_RECEIPT_PREFIX) and len(path) > len(MAIL_RECEIPT_PREFIX)


class MailClientCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=3, max_length=80)
    owner_user_id: str = Field(min_length=1, max_length=80)
    mailbox: str = Field(min_length=6, max_length=254)
    expires_at: _dt.datetime

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        text = _clean_required(value, "name", max_len=80)
        if not CLIENT_NAME_RE.fullmatch(text):
            raise ValueError("name只能包含字母、数字、空格、点、下划线或短横线，且首尾必须为字母或数字")
        return text

    @field_validator("owner_user_id")
    @classmethod
    def validate_owner_user_id(cls, value: str) -> str:
        text = _clean_required(value, "owner_user_id", max_len=80)
        if "/" in text or "\\" in text:
            raise ValueError("owner_user_id不能包含路径分隔符")
        return text

    @field_validator("mailbox")
    @classmethod
    def validate_mailbox(cls, value: str) -> str:
        text = _clean_required(value, "mailbox", max_len=254).lower()
        if not MAILBOX_RE.fullmatch(text):
            raise ValueError("mailbox必须是合法邮箱地址")
        return text

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: _dt.datetime) -> _dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at必须包含时区")
        expires = value.astimezone(_dt.timezone.utc).replace(microsecond=0)
        now = _utc_now()
        if expires <= now:
            raise ValueError("expires_at必须是未来时间")
        try:
            limit = now.replace(year=now.year + 10)
        except ValueError:  # February 29 becomes February 28 in a non-leap year.
            limit = now.replace(year=now.year + 10, day=28)
        if expires > limit:
            raise ValueError("expires_at不能超过10年")
        return expires


class MailClientResponse(BaseModel):
    id: str
    name: str
    owner_user_id: str
    owner: dict[str, Any]
    mailbox: str
    expires_at: str
    revoked_at: Optional[str] = None
    created_at: str


class MailClientCreateResponse(MailClientResponse):
    bearer_token: str


class MailAttachmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(min_length=1, max_length=192)
    filename: str = Field(min_length=1, max_length=240)
    content_base64: str = Field(min_length=1)
    size: int = Field(gt=0, le=MAIL_MAX_FILE_BYTES)
    content_type: Optional[str] = Field(default=None, max_length=255)
    sha256: Optional[str] = Field(default=None, min_length=64, max_length=64)

    @field_validator("size", mode="before")
    @classmethod
    def validate_size(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("size必须是整数")
        return value

    @field_validator("attachment_id")
    @classmethod
    def validate_attachment_id(cls, value: str) -> str:
        text = _clean_required(value, "attachment_id", max_len=192)
        if not OPAQUE_TEXT_RE.fullmatch(text):
            raise ValueError("attachment_id包含非法字符")
        return text

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        text = _clean_required(value, "filename", max_len=240)
        if "/" in text or "\\" in text:
            raise ValueError("filename不能包含路径分隔符")
        safe = clean_filename(text)
        if safe != text:
            raise ValueError("filename包含非法字符")
        return text

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = _clean_required(value, "content_type", max_len=255).lower()
        if not CONTENT_TYPE_RE.fullmatch(text):
            raise ValueError("content_type格式不合法")
        return text

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip().lower()
        if not SHA256_RE.fullmatch(text):
            raise ValueError("sha256必须是64位小写十六进制")
        return text


class MailAttachmentIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mailbox: str = Field(min_length=6, max_length=254)
    message_id: str = Field(min_length=1, max_length=255)
    subject: str = Field(max_length=500)
    sender: str = Field(min_length=1, max_length=320)
    received_at: _dt.datetime
    attachment: MailAttachmentPayload
    biz_keyword: Optional[str] = Field(default=None, max_length=120)
    test_case: Optional[str] = Field(default=None, max_length=120)

    @field_validator("mailbox")
    @classmethod
    def validate_mailbox(cls, value: str) -> str:
        text = _clean_required(value, "mailbox", max_len=254).lower()
        if not MAILBOX_RE.fullmatch(text):
            raise ValueError("mailbox必须是合法邮箱地址")
        return text

    @field_validator("message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        text = _clean_required(value, "message_id", max_len=255)
        if not OPAQUE_TEXT_RE.fullmatch(text):
            raise ValueError("message_id包含非法字符")
        return text

    @field_validator("subject", "sender", "biz_keyword", "test_case")
    @classmethod
    def validate_text(cls, value: Optional[str], info) -> Optional[str]:
        if value is None:
            return None
        limits = {"subject": 500, "sender": 320, "biz_keyword": 120, "test_case": 120}
        required = info.field_name == "sender"
        text = value.replace("\x00", "").strip()
        if info.field_name == "subject":
            required = False
        if required and not text:
            raise ValueError(f"{info.field_name}不能为空")
        if len(text) > limits[info.field_name]:
            raise ValueError(f"{info.field_name}不能超过{limits[info.field_name]}个字符")
        return text

    @field_validator("received_at")
    @classmethod
    def validate_received_at(cls, value: _dt.datetime) -> _dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_at必须包含时区")
        return value.astimezone(_dt.timezone.utc).replace(microsecond=0)

    @model_validator(mode="after")
    def validate_attachment_bytes(self):
        try:
            decoded = base64.b64decode(self.attachment.content_base64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError("content_base64不是合法base64") from exc
        if not decoded:
            raise ValueError("附件内容不能为空")
        if len(decoded) != self.attachment.size:
            raise ValueError("size与附件内容长度不一致")
        if len(decoded) > MAIL_MAX_FILE_BYTES:
            raise ValueError(f"附件超过{MAIL_MAX_FILE_BYTES}字节限制")
        actual_sha = hashlib.sha256(decoded).hexdigest()
        if self.attachment.sha256 and not hmac.compare_digest(self.attachment.sha256, actual_sha):
            raise ValueError("sha256与附件内容不一致")
        self.__dict__["_decoded"] = decoded
        self.__dict__["_sha256"] = actual_sha
        return self

    @property
    def decoded(self) -> bytes:
        return self.__dict__["_decoded"]

    @property
    def sha256(self) -> str:
        return self.__dict__["_sha256"]


class MailReceiptResponse(BaseModel):
    receipt_id: str
    status: str
    batch_id: str
    item_id: str
    classification: str
    project_category: str
    product: str
    module: str
    version: str
    reason: str
    sha256: str
    created_at: str
    updated_at: str


class MailIngestService:
    def __init__(self, store, identity):
        self.store = store
        self.identity = identity

    def create_client(self, payload: MailClientCreateRequest, actor: dict[str, Any]) -> dict[str, Any]:
        owner = _owner_from_identity(self.identity, payload.owner_user_id)
        raw_token = secrets.token_urlsafe(32)
        token_hash = _hash_token(raw_token)
        client_id = f"MAILCLIENT-{uuid.uuid4().hex}"
        now = now_iso()
        with self.store.lock:
            with self.store.conn.immediate():
                self.store.execute(
                    """INSERT INTO mail_clients(
                           id,name,owner_user_id,mailbox,token_hash,expires_at,revoked_at,
                           created_at,created_by_user_id,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (client_id, payload.name, owner["id"], payload.mailbox, token_hash, _time_iso(payload.expires_at), None, now, actor["id"], now),
                )
                self.store.add_log(
                    "邮件接入客户端",
                    payload.name,
                    f"创建邮箱接入：{payload.mailbox}，归属 {owner['display_name']} [{owner['id']}]，未记录明文密钥。",
                    client_id,
                    "成功",
                    user=f"{actor['display_name']} [{actor['id']}]",
                )
        return self._client_response(client_id) | {"bearer_token": raw_token}

    def list_clients(self) -> list[dict[str, Any]]:
        rows = self.store.execute("SELECT * FROM mail_clients ORDER BY created_at DESC,id DESC").fetchall()
        return [self._client_response(row["id"], row=row) for row in rows]

    def revoke_client(self, client_id: str, actor: dict[str, Any]) -> dict[str, Any]:
        if not client_id or "/" in client_id or "\\" in client_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件接入客户端不存在")
        now = now_iso()
        with self.store.lock:
            with self.store.conn.immediate():
                row = self.store.execute("SELECT * FROM mail_clients WHERE id=?", (client_id,)).fetchone()
                if not row:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件接入客户端不存在")
                if not row["revoked_at"]:
                    self.store.execute("UPDATE mail_clients SET revoked_at=?,updated_at=? WHERE id=?", (now, now, client_id))
                    self.store.add_log(
                        "邮件接入客户端",
                        row["name"],
                        f"撤销邮箱接入：{row['mailbox']}，未记录明文密钥。",
                        client_id,
                        "成功",
                        user=f"{actor['display_name']} [{actor['id']}]",
                    )
        return self._client_response(client_id)

    def _client_from_authorization(self, authorization: str) -> dict[str, Any]:
        if not authorization.startswith("Bearer ") or authorization.count(" ") != 1:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少邮件接入Bearer令牌")
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少邮件接入Bearer令牌")
        token_hash = _hash_token(token)
        row = self.store.execute("SELECT * FROM mail_clients WHERE token_hash=?", (token_hash,)).fetchone()
        if not row or not hmac.compare_digest(str(row["token_hash"]), token_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮件接入令牌无效")
        if row["revoked_at"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮件接入令牌已撤销")
        expires = _parse_time(row["expires_at"])
        if not expires or expires <= _utc_now():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮件接入令牌已过期")
        owner = _owner_from_identity(self.identity, row["owner_user_id"], inactive_status=status.HTTP_403_FORBIDDEN)
        return dict(row) | {"owner": owner}

    def authenticate_client(self, request: Request) -> dict[str, Any]:
        authorization = request.headers.get("authorization") or ""
        with self.store.lock, self.store.conn:
            return self._client_from_authorization(authorization)

    def ingest_attachment(self, authorization: str, payload_data: Any) -> tuple[int, dict[str, Any]]:
        code, result = self._ingest_group(authorization, [payload_data])
        return code, result["receipts"][0]

    def ingest_batch(self, authorization: str, payload_data: Any) -> tuple[int, dict[str, Any]]:
        # Authenticate before reporting payload validation details.
        with self.store.lock:
            self._client_from_authorization(authorization)
        if not isinstance(payload_data, dict):
            raise HTTPException(422, "邮件批次必须是对象")
        attachments = payload_data.get("attachments")
        if not isinstance(attachments, list) or not 1 <= len(attachments) <= 200:
            raise HTTPException(422, "同一邮件批次需要1–200个附件")
        if "attachment" in payload_data:
            raise HTTPException(422, "批次请使用attachments字段")
        base = {k: v for k, v in payload_data.items() if k != "attachments"}
        return self._ingest_group(authorization, [dict(base, attachment=a) for a in attachments])

    def _ingest_group(self, authorization, payload_data):
        staged_paths = []
        with self.store.lock:
            try:
                with self.store.conn.immediate():
                    client = self._client_from_authorization(authorization)
                    try:
                        payloads = [p if isinstance(p, MailAttachmentIngestRequest) else MailAttachmentIngestRequest.model_validate(p) for p in payload_data]
                    except ValidationError as exc:
                        raise HTTPException(422, {"error": "邮件附件请求体不合法", "errors": [
                            {"field": ".".join(str(p) for p in e["loc"]), "type": e["type"], "message": e["msg"]}
                            for e in exc.errors(include_input=False, include_context=False)]}) from exc
                    if any(p.mailbox != client["mailbox"] for p in payloads):
                        raise HTTPException(403, "mailbox不属于当前邮件接入客户端")
                    if sum(p.attachment.size for p in payloads) > MAIL_MAX_FILE_BYTES:
                        raise HTTPException(413, "邮件批次附件总量不能超过50MB；请减少附件后重试")
                    keys = [(p.mailbox, p.message_id, p.attachment.attachment_id) for p in payloads]
                    if len(keys) != len(set(keys)):
                        raise HTTPException(422, "同批附件attachment_id不能重复")
                    existing = []
                    fingerprints = [_fingerprint(self._normalized_payload(p)) for p in payloads]
                    for key, fingerprint in zip(keys, fingerprints):
                        row = self.store.execute("SELECT * FROM mail_receipts WHERE mailbox=? AND message_id=? AND attachment_id=?", key).fetchone()
                        if row and (row["client_id"] != client["id"] or not hmac.compare_digest(row["payload_fingerprint"], fingerprint)):
                            raise HTTPException(409, "同一邮件附件已有不同内容或属于其他客户端，拒绝覆盖")
                        existing.append(row)
                    if any(existing):
                        if not all(existing) or len({r["batch_id"] for r in existing}) != 1:
                            raise HTTPException(409, "该组附件部分已接入其他批次，请人工核对，不能拆散重建")
                        return 200, {"batch_id": existing[0]["batch_id"], "receipts": [self._receipt_response(r["id"], client) for r in existing]}
                    owner_policy = AccessPolicy(self.store, client["owner"])
                    title_hints = defaultdict(deque)
                    for payload in payloads:
                        title_hints[clean_filename(payload.attachment.filename)].append(extract_mail_title(payload.subject))

                    def validate_with_mail_title(item: dict[str, Any]) -> None:
                        title = title_hints[item.get("fileName", "")].popleft() if title_hints[item.get("fileName", "")] else {}
                        updated, _title_evidence = apply_mail_title_evidence(item, title)
                        if updated is not item:
                            item.clear()
                            item.update(updated)
                        owner_policy.validate_upload(item)

                    files = [{"name": p.attachment.filename, "data": p.attachment.content_base64} for p in payloads]
                    with self.store.acting_as(f"{client['owner']['display_name']} [{client['owner']['id']}]"):
                        result = self.store.analyze(files, validate_item=validate_with_mail_title)
                    items = list(result.get("items", []))
                    staged_paths = [i.get("metadata", {}).get("blob_path") for i in items if i.get("metadata", {}).get("blob_path")]
                    if len(items) != len(payloads):
                        raise HTTPException(500, "邮件附件解析结果异常")
                    batch_id = result["batch_id"]
                    receipts = []
                    for payload, fingerprint in zip(payloads, fingerprints):
                        item = next((i for i in items if i["fileName"] == payload.attachment.filename and i["sha256"] == payload.sha256), None)
                        if item is None:
                            raise HTTPException(500, "邮件附件解析对应关系异常")
                        items.remove(item)
                        item_id = item["id"]
                        metadata = item.get("metadata") or {}
                        title_evidence = metadata.get("mail_title") if isinstance(metadata, dict) else {}
                        email_source = {
                            "kind": "mail_ingest",
                            "mail_case": classify_mail_subject(payload.subject),
                            "client_id": client["id"],
                            "mailbox": payload.mailbox,
                            "message_id": payload.message_id,
                            "attachment_id": payload.attachment.attachment_id,
                            "sender": payload.sender,
                            "subject": payload.subject,
                            "received_at": _time_iso(payload.received_at),
                            "content_type": payload.attachment.content_type or "",
                            "biz_keyword": payload.biz_keyword or "",
                            "test_case": payload.test_case or "",
                        }
                        metadata = metadata | {"email_source": email_source}
                        now = now_iso()
                        receipt_id = f"MAILRCPT-{uuid.uuid4().hex}"
                        if email_source["mail_case"] == "ecn":
                            metadata = self._force_ecn_review(item_id, batch_id, item | {"metadata": metadata}, now)
                        elif title_evidence and title_evidence.get("conflicts"):
                            metadata = self._force_title_conflict(item_id, batch_id, item | {"metadata": metadata}, title_evidence, now)
                        self.store.execute("UPDATE batches SET owner_user_id=?,updated_at=? WHERE id=?", (client["owner_user_id"], now, batch_id))
                        self.store.execute("UPDATE batch_items SET metadata=?,updated_at=? WHERE id=?", (json_dumps(metadata), now, item_id))
                        self.store.execute(
                            """INSERT INTO mail_receipts(
                                   id,client_id,mailbox,message_id,attachment_id,payload_fingerprint,
                                   batch_id,batch_item_id,sha256,created_at,updated_at
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (receipt_id, client["id"], payload.mailbox, payload.message_id, payload.attachment.attachment_id, fingerprint, batch_id, item_id, payload.sha256, now, now),
                        )
                        self.store.add_log(
                            "邮件附件接入",
                            payload.attachment.filename,
                            f"邮件 {payload.message_id} 附件 {payload.attachment.attachment_id} 已创建核对批次 {batch_id}。",
                            receipt_id,
                            "成功",
                            user=f"{client['owner']['display_name']} [{client['owner']['id']}]")
                        receipts.append(self._receipt_response(receipt_id, client))
                    self._refresh_batch_summary(batch_id)
                    return 201, {"batch_id": batch_id, "receipts": receipts}
            except Exception:
                if staged_paths:
                    self.store.cleanup_blobs(staged_paths)
                raise

    def receipt(self, authorization: str, receipt_id: str) -> dict[str, Any]:
        if not receipt_id or "/" in receipt_id or "\\" in receipt_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件接入回执不存在")
        with self.store.lock, self.store.conn:
            client = self._client_from_authorization(authorization)
            return self._receipt_response(receipt_id, client)

    def _client_response(self, client_id: str, row=None) -> dict[str, Any]:
        row = row or self.store.execute("SELECT * FROM mail_clients WHERE id=?", (client_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件接入客户端不存在")
        owner = next((user for user in self.identity.list_users() if user.get("id") == row["owner_user_id"]), None)
        owner_payload = owner or {"id": row["owner_user_id"], "active": False, "scopes": []}
        return {
            "id": row["id"],
            "name": row["name"],
            "owner_user_id": row["owner_user_id"],
            "owner": owner_payload,
            "mailbox": row["mailbox"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "created_at": row["created_at"],
        }

    def _normalized_payload(self, payload: MailAttachmentIngestRequest) -> dict[str, Any]:
        return {
            "mailbox": payload.mailbox,
            "message_id": payload.message_id,
            "subject": payload.subject,
            "sender": payload.sender,
            "received_at": _time_iso(payload.received_at),
            "biz_keyword": payload.biz_keyword or "",
            "test_case": payload.test_case or "",
            "attachment": {
                "attachment_id": payload.attachment.attachment_id,
                "filename": payload.attachment.filename,
                "size": payload.attachment.size,
                "content_type": payload.attachment.content_type or "",
                "sha256": payload.sha256,
            },
        }

    def _force_ecn_review(self, item_id: str, batch_id: str, item: dict[str, Any], now: str) -> dict[str, Any]:
        note = "ECN 邮件属于变更批次，请人工核对关联 BOM 和全部附件；不能按 BOM 首次发放自动归档。"
        metadata = dict(item.get("metadata") or {})
        metadata["review_pending"] = True
        metadata["review_note"] = note
        self.store.execute(
            "UPDATE batch_items SET status='conflict',recommendation=?,metadata=?,updated_at=? WHERE id=? AND batch_id=?",
            (note, json_dumps(metadata), now, item_id, batch_id),
        )
        existing = self.store.execute("SELECT id FROM exceptions WHERE item_id=? AND status='open'", (item_id,)).fetchone()
        if existing:
            self.store.execute("UPDATE exceptions SET detail=? WHERE id=?", (note, existing["id"]))
        else:
            self.store.create_exception_for_item(batch_id, item_id, item.get("fileName") or "", "conflict", note,
                                                 item.get("recordId") or "", item.get("code") or "")
        return metadata

    def _force_title_conflict(self, item_id: str, batch_id: str, item: dict[str, Any], title_evidence: dict[str, Any], now: str) -> dict[str, Any]:
        conflicts = title_evidence.get("conflicts") or []
        if not conflicts:
            return item.get("metadata") or {}
        status_value = "conflict"
        record_id = item.get("recordId") or ""
        current = item.get("current") or "—"
        labels = {"registrant": "公司主体", "project_category": "项目类别", "product": "产品型号", "module": "DMR子模块"}
        parts = [f"{labels.get(row.get('field'), row.get('field'))}：文件为{row.get('existing')}，邮件标题为{row.get('mail_title')}" for row in conflicts]
        recommendation = "邮件标题与文件解析结果不一致，需人工确认：" + "；".join(parts)
        metadata = dict(item.get("metadata") or {})
        metadata["review_pending"] = True
        metadata["review_note"] = recommendation
        self.store.execute(
            """UPDATE batch_items
                  SET record_id=?,current_version=?,status=?,recommendation=?,
                      metadata=?,updated_at=?
                WHERE id=? AND batch_id=?""",
            (
                record_id,
                current,
                status_value,
                recommendation,
                json_dumps(metadata),
                now,
                item_id,
                batch_id,
            ),
        )
        existing = self.store.execute("SELECT id FROM exceptions WHERE item_id=? AND status='open'", (item_id,)).fetchone()
        if existing:
            self.store.execute("UPDATE exceptions SET detail=?,record_id=?,record_no=? WHERE id=?", (recommendation, record_id or "", item.get("code") or "", existing["id"]))
        else:
            self.store.create_exception_for_item(batch_id, item_id, item.get("fileName") or "", status_value, recommendation, record_id or "", item.get("code") or "")
        return metadata

    def _refresh_batch_summary(self, batch_id: str) -> None:
        rows = self.store.execute("SELECT status FROM batch_items WHERE batch_id=?", (batch_id,)).fetchall()
        summary = {
            "total": len(rows),
            "matched": sum(1 for row in rows if row["status"] == "matched"),
            "conflict": sum(1 for row in rows if row["status"] == "conflict"),
            "unmatched": sum(1 for row in rows if row["status"] == "unmatched"),
            "duplicate": sum(1 for row in rows if row["status"] == "duplicate"),
        }
        self.store.execute("UPDATE batches SET summary=?,updated_at=? WHERE id=?", (json_dumps(summary), now_iso(), batch_id))

    def _receipt_response(self, receipt_id: str, client: dict[str, Any]) -> dict[str, Any]:
        receipt = self.store.execute("SELECT * FROM mail_receipts WHERE id=?", (receipt_id,)).fetchone()
        if not receipt or receipt["client_id"] != client["id"]:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件接入回执不存在")
        owner = _owner_from_identity(self.identity, client["owner_user_id"], inactive_status=status.HTTP_403_FORBIDDEN)
        access = AccessPolicy(self.store, owner)
        access.batch(receipt["batch_id"])
        item = self.store.execute("SELECT * FROM batch_items WHERE id=? AND batch_id=?", (receipt["batch_item_id"], receipt["batch_id"])).fetchone()
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="邮件接入批次项目不存在")
        status_value = self._receipt_status(item["status"])
        return {
            "receipt_id": receipt["id"],
            "status": status_value,
            "batch_id": receipt["batch_id"],
            "item_id": receipt["batch_item_id"],
            "classification": item["status"],
            "project_category": item["project_category"] or "待确认",
            "product": item["product"] or "",
            "module": item["module"] or "",
            "version": item["version"] or item["detected_version"] or "",
            "reason": item["result_message"] or item["recommendation"] or "",
            "sha256": receipt["sha256"],
            "created_at": receipt["created_at"],
            "updated_at": max(filter(None, (item["updated_at"], receipt["updated_at"],
                self.store.execute("SELECT updated_at FROM batches WHERE id=?", (receipt["batch_id"],)).fetchone()["updated_at"]))),
        }

    @staticmethod
    def _receipt_status(item_status: str) -> str:
        if item_status in {"unmatched", "conflict"}:
            return "needs_review"
        if item_status == "matched":
            return "awaiting_confirmation"
        if item_status == "committed":
            return "archived"
        if item_status in {"duplicate", "skipped"}:
            return "skipped"
        if item_status == "rejected":
            return "failed"
        return "needs_review"


def install_mail_ingest(app: FastAPI, store, identity, policy) -> MailIngestService:
    service = MailIngestService(store, identity)
    transfer_archive = TransferManifestArchive(store)
    app.state.mail_ingest = service
    app.state.transfer_manifest_archive = transfer_archive
    bearer_scheme = HTTPBearer(auto_error=False)

    def admin_user(request: Request) -> dict[str, Any]:
        return policy(request, "manage_users").user

    def bearer_authorization(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
        if credentials is None:
            return ""
        return f"Bearer {credentials.credentials}"

    @app.post("/api/integrations/mail/clients", response_model=MailClientCreateResponse, status_code=201)
    async def create_mail_client(request: Request, payload: MailClientCreateRequest = Body(...)):
        actor = admin_user(request)
        return await run_in_threadpool(lambda: service.create_client(payload, actor))

    @app.get("/api/integrations/mail/clients", response_model=list[MailClientResponse])
    async def list_mail_clients(request: Request):
        admin_user(request)
        return await run_in_threadpool(service.list_clients)

    @app.delete("/api/integrations/mail/clients/{client_id:path}", response_model=MailClientResponse)
    async def delete_mail_client(client_id: str, request: Request):
        actor = admin_user(request)
        return await run_in_threadpool(lambda: service.revoke_client(client_id, actor))

    @app.post("/api/integrations/mail/attachments", response_model=MailReceiptResponse)
    async def ingest_mail_attachment(
        payload: dict[str, Any] = Body(...),
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ):
        authorization = bearer_authorization(credentials)
        code, body = await run_in_threadpool(lambda: service.ingest_attachment(authorization, payload))
        from fastapi.responses import JSONResponse
        return JSONResponse(body, status_code=code)

    @app.post("/api/integrations/mail/batches")
    async def ingest_mail_batch(payload: dict[str, Any] = Body(...),
                                credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
        code, body = await run_in_threadpool(lambda: service.ingest_batch(bearer_authorization(credentials), payload))
        from fastapi.responses import JSONResponse
        return JSONResponse(body, status_code=code)

    @app.post("/api/integrations/mail/transfer-manifests")
    async def ingest_transfer_manifest(payload: dict[str, Any] = Body(...), stage_only: bool = False,
                                       credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
        def process():
            authorization = bearer_authorization(credentials)
            with store.lock, store.conn:
                client = service._client_from_authorization(authorization)
            try:
                validated = MailAttachmentIngestRequest.model_validate(payload)
            except ValidationError as exc:
                raise HTTPException(422, {"error": "邮件附件请求体不合法", "errors": [
                    {"field": ".".join(str(p) for p in item["loc"]), "type": item["type"], "message": item["msg"]}
                    for item in exc.errors(include_input=False, include_context=False)]}) from exc
            if validated.mailbox != client["mailbox"]:
                raise HTTPException(403, "mailbox不属于当前邮件接入客户端")
            return transfer_archive.ingest(client, validated, _fingerprint(service._normalized_payload(validated)), stage_only=stage_only)
        code, body = await run_in_threadpool(process)
        from fastapi.responses import JSONResponse
        return JSONResponse(body, status_code=code)

    @app.get("/api/integrations/mail/transfer-manifests")
    async def list_transfer_manifests(request: Request, limit: int = 100):
        policy(request, "audit")
        if limit < 1 or limit > 200:
            raise HTTPException(422, "limit须在1到200之间")
        def read_rows():
            with store.lock, store.conn:
                rows = store.execute(
                    """SELECT id,filename,file_date,target_path,status,last_error,created_at,updated_at
                       FROM transfer_manifests ORDER BY updated_at DESC,id DESC LIMIT ?""", (limit,)
                ).fetchall()
            from backend.transfer_manifest import network_path_for
            return {"items": [{**dict(row), "network_path": network_path_for(row["target_path"])} for row in rows]}
        return await run_in_threadpool(read_rows)

    @app.get("/api/integrations/mail/receipts/{receipt_id:path}", response_model=MailReceiptResponse)
    async def get_mail_receipt(
        receipt_id: str,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    ):
        authorization = bearer_authorization(credentials)
        return await run_in_threadpool(lambda: service.receipt(authorization, receipt_id))

    # Validation happens after authentication in the service, but publish the same
    # Pydantic contract in OpenAPI rather than an uninformative arbitrary object.
    original_openapi = app.openapi
    def mail_openapi():
        schema = original_openapi()
        model = MailAttachmentIngestRequest.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        schemas.update(model.pop("$defs", {}))
        schemas["MailAttachmentIngestRequest"] = model
        operation = schema["paths"]["/api/integrations/mail/attachments"]["post"]
        operation["requestBody"] = {"required": True, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/MailAttachmentIngestRequest"}}}}
        operation["responses"]["201"] = {"description": "Attachment accepted for review",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/MailReceiptResponse"}}}}
        return schema
    app.openapi = mail_openapi
    return service
