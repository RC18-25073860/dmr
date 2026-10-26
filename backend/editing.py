"""Opaque optimistic-edit tokens. Authorization is always checked separately."""
from __future__ import annotations
import hashlib
import hmac
import json
from fastapi import HTTPException


def edit_token(kind: str, identity: str, snapshot) -> str:
    raw = json.dumps([kind, identity, snapshot], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def require_edit_token(expected, current: str):
    if not isinstance(expected, str) or not expected:
        raise HTTPException(428, "缺少编辑凭据，请刷新并重新打开后保存")
    if not expected.isascii() or not hmac.compare_digest(expected, current):
        raise HTTPException(409, "内容已被其他人更新，请刷新并重新核对；本次修改尚未保存")
