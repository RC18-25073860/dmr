from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import re
import secrets
import uuid
from typing import Any

from fastapi import HTTPException, status

ROLE_VALUES = {"admin", "document_control", "reader", "uploader"}
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.@-]{2,63}$")
TOKEN_TTL_HOURS = 12
LOCK_AFTER_FAILURES = 5
LOCK_MINUTES = 15


class IdentityError(ValueError):
    pass


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_time(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise IdentityError("用户名必须是文本")
    normalized = username.strip().lower()
    if not USERNAME_RE.fullmatch(normalized):
        raise IdentityError("用户名需为3-64位字母、数字、点、下划线、短横线或@")
    return normalized


def _clean_text(value: Any, field: str, *, required: bool = True, max_len: int = 120) -> str:
    if not isinstance(value, str):
        raise IdentityError(f"{field}必须是文本")
    text = value.replace("\x00", "").strip()
    if required and not text:
        raise IdentityError(f"{field}不能为空")
    if len(text) > max_len:
        raise IdentityError(f"{field}不能超过{max_len}个字符")
    return text


def _strict_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise IdentityError(f"{field}必须是布尔值")
    return value


def _validate_role(value: Any) -> str:
    if not isinstance(value, str) or value not in ROLE_VALUES:
        raise IdentityError("角色不合法")
    return value


def _validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 10:
        raise IdentityError("密码至少10位")
    if len(password) > 256:
        raise IdentityError("密码不能超过256位")
    if "\x00" in password:
        raise IdentityError("密码包含非法字符")
    return password


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2**15, 8, 3
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(
        n,
        r,
        p,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(key).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_text, r_text, p_text, salt_text, key_text = encoded.split("$", 5)
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
        expected = base64.b64decode(key_text.encode("ascii"), validate=True)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


class IdentityService:
    """Local identity service backed by the existing DMR store connection."""

    def __init__(self, store):
        self.store = store

    def status(self) -> dict[str, Any]:
        row = self.store.execute("SELECT COUNT(*) AS count FROM identity_users").fetchone()
        count = int(row["count"] if row else 0)
        return {"setup_required": count == 0, "identity_source": "local", "user_count": count}

    def setup_required(self) -> bool:
        return bool(self.status()["setup_required"])

    def setup(self, display_name: str, username: str, password: str) -> tuple[str, dict[str, Any]]:
        normalized = _normalize_username(username)
        display = _clean_text(display_name, "显示名称")
        _validate_password(password)
        with self.store.lock:
            with self.store.conn.immediate():
                row = self.store.execute("SELECT COUNT(*) AS count FROM identity_users").fetchone()
                if int(row["count"] if row else 0) != 0:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="本地身份已初始化")
                now = _now_iso()
                user_id = f"USER-{uuid.uuid4().hex}"
                self.store.execute(
                    """INSERT INTO identity_users(
                           id,username,username_normalized,display_name,role,active,password_hash,
                           password_changed_at,must_change_password,can_download,can_history,identity_source,
                           failed_login_count,locked_until,created_at,updated_at,edit_revision
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (user_id, username.strip(), normalized, display, "admin", 1, _hash_password(password), now, 0, 1, 1, "local", 0, None, now, now, 0),
                )
                self._insert_scope(user_id, {"registrant": "*", "project_category": "*", "product": "*"}, now)
                self._audit(None, "setup", user_id, display, {"role": "admin", "identity_source": "local"}, now)
                raw_token = self._create_session(user_id, now)
                return raw_token, self._public_user_from_id(user_id)

    def login(self, username: str, password: str) -> tuple[str, dict[str, Any]]:
        try:
            normalized = _normalize_username(username)
        except IdentityError:
            normalized = ""
        if not isinstance(password, str):
            password = ""
        login_error: HTTPException | None = None
        result: tuple[str, dict[str, Any]] | None = None
        with self.store.lock:
            with self.store.conn.immediate():
                row = self.store.execute("SELECT * FROM identity_users WHERE username_normalized=?", (normalized,)).fetchone()
                now = _now_iso()
                failure_reason = "invalid_credentials"
                if not row or not row["active"]:
                    failure_reason = "unknown_user" if not row else "inactive"
                    login_error = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
                else:
                    locked_until = _parse_time(row["locked_until"])
                    if locked_until and locked_until > _now():
                        failure_reason = "locked"
                        login_error = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录失败次数过多，请稍后再试")
                    else:
                        now = _now_iso()
                        if not _verify_password(password, row["password_hash"]):
                            failures = int(row["failed_login_count"] or 0) + 1
                            lock_value = None
                            if failures >= LOCK_AFTER_FAILURES:
                                lock_value = (_now() + _dt.timedelta(minutes=LOCK_MINUTES)).isoformat()
                            self.store.execute(
                                "UPDATE identity_users SET failed_login_count=?,locked_until=?,updated_at=? WHERE id=?",
                                (failures, lock_value, now, row["id"]),
                            )
                            login_error = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
                        else:
                            self.store.execute(
                                "UPDATE identity_users SET failed_login_count=0,locked_until=NULL,updated_at=? WHERE id=?",
                                (now, row["id"]),
                            )
                            raw_token = self._create_session(row["id"], now)
                            self._audit(dict(row), "login", row["id"], row["display_name"], {"result": "success"}, now)
                            result = (raw_token, self._public_user_from_id(row["id"]))
                if login_error is not None:
                    self._audit(
                        {"display_name": "未认证访问者"}, "login",
                        row["id"] if row else "", row["display_name"] if row else normalized,
                        {"result": "failure", "reason": failure_reason, "attempted_username": normalized}, now,
                    )
        if login_error is not None:
            raise login_error
        if result is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
        return result

    def logout(self, raw_token: str) -> None:
        token_hash = _hash_token(raw_token or "")
        now = _now_iso()
        with self.store.lock, self.store.conn.immediate():
            row = self.store.execute("SELECT * FROM identity_sessions WHERE token_hash=? AND revoked_at IS NULL", (token_hash,)).fetchone()
            if row:
                user = self.store.execute("SELECT * FROM identity_users WHERE id=?", (row["user_id"],)).fetchone()
                expires_at = _parse_time(row["expires_at"])
                actor = dict(user) if user and user["active"] and expires_at and expires_at > _now() else {"display_name": "未认证访问者"}
                self.store.execute("UPDATE identity_sessions SET revoked_at=? WHERE id=?", (now, row["id"]))
                self._audit(actor, "logout", row["user_id"], user["display_name"] if user else "", {"result": "success"}, now)

    def change_password(self, raw_token: str, current_password: str, new_password: str) -> dict[str, Any]:
        _validate_password(current_password if isinstance(current_password, str) else "")
        _validate_password(new_password if isinstance(new_password, str) else "")
        if current_password == new_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同")
        token_hash = _hash_token(raw_token or "")
        with self.store.lock:
            with self.store.conn.immediate():
                session = self.store.execute("SELECT * FROM identity_sessions WHERE token_hash=?", (token_hash,)).fetchone()
                if not session or session["revoked_at"]:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
                expires_at = _parse_time(session["expires_at"])
                if not expires_at or expires_at <= _now():
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
                row = self.store.execute("SELECT * FROM identity_users WHERE id=?", (session["user_id"],)).fetchone()
                if not row or not row["active"]:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
                changed_at = _parse_time(row["password_changed_at"])
                created_at = _parse_time(session["created_at"])
                if changed_at and created_at and created_at < changed_at:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
                if not _verify_password(current_password, row["password_hash"]):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前密码不正确")
                if _verify_password(new_password, row["password_hash"]):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同")
                now = _now_iso()
                self.store.execute(
                    """UPDATE identity_users
                       SET password_hash=?,password_changed_at=?,must_change_password=0,
                           failed_login_count=0,locked_until=NULL,updated_at=?,edit_revision=edit_revision+1
                       WHERE id=?""",
                    (_hash_password(new_password), now, now, row["id"]),
                )
                self.store.execute("UPDATE identity_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, row["id"]))
                user = self._public_user_from_id(row["id"])
                self._audit(user, "change_password", row["id"], user["display_name"], {"self_service": True, "sessions_revoked": "all"}, now)
                return user

    def current(self, raw_token: str) -> dict[str, Any]:
        token_hash = _hash_token(raw_token or "")
        with self.store.lock, self.store.conn:
            session = self.store.execute("SELECT * FROM identity_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if not session or session["revoked_at"]:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
            expires_at = _parse_time(session["expires_at"])
            if not expires_at or expires_at <= _now():
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
            user = self.store.execute("SELECT * FROM identity_users WHERE id=?", (session["user_id"],)).fetchone()
            if not user or not user["active"]:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
            changed_at = _parse_time(user["password_changed_at"])
            created_at = _parse_time(session["created_at"])
            if changed_at and created_at and created_at < changed_at:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
            self.store.execute("UPDATE identity_sessions SET last_seen_at=? WHERE id=?", (_now_iso(), session["id"]))
            return self._public_user(user)

    def list_users(self) -> list[dict[str, Any]]:
        rows = self.store.execute("SELECT * FROM identity_users ORDER BY created_at,id").fetchall()
        return [self._public_user(row) for row in rows]

    def create_user(self, payload: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(actor)
        data = self._validate_create_payload(payload)
        with self.store.lock:
            with self.store.conn.immediate():
                if self.store.execute("SELECT id FROM identity_users WHERE username_normalized=?", (data["username_normalized"],)).fetchone():
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")
                now = _now_iso()
                user_id = f"USER-{uuid.uuid4().hex}"
                self.store.execute(
                    """INSERT INTO identity_users(
                           id,username,username_normalized,display_name,role,active,password_hash,
                           password_changed_at,must_change_password,can_download,can_history,identity_source,
                           failed_login_count,locked_until,created_at,updated_at,edit_revision
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        user_id,
                        data["username"],
                        data["username_normalized"],
                        data["display_name"],
                        data["role"],
                        1 if data["active"] else 0,
                        _hash_password(data["password"]),
                        now,
                        1,
                        1 if data["can_download"] else 0,
                        1 if data["can_history"] else 0,
                        "local",
                        0,
                        None,
                        now,
                        now,
                        0,
                    ),
                )
                self._replace_scopes(user_id, data["scopes"], now)
                self._audit(actor, "create_user", user_id, data["display_name"], self._audit_user_payload(data), now)
                return self._public_user_from_id(user_id)

    def update_user(self, user_id: str, payload: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(actor)
        if not isinstance(user_id, str) or not user_id:
            raise IdentityError("用户ID不能为空")
        data = self._validate_update_payload(payload)
        expected_edit_token = data.pop("expected_edit_token")
        with self.store.lock:
            with self.store.conn.immediate():
                row = self.store.execute("SELECT * FROM identity_users WHERE id=?", (user_id,)).fetchone()
                if not row:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
                current_edit_token = self._user_edit_token(row)
                if not hmac.compare_digest(expected_edit_token.encode("utf-8"), current_edit_token.encode("utf-8")):
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="人员权限已被其他人修改，请刷新后再提交")
                target_role = data.get("role", row["role"])
                target_active = data.get("active", bool(row["active"]))
                if row["role"] == "admin" and (target_role != "admin" or not target_active):
                    self._ensure_another_active_admin(row["id"])
                now = _now_iso()
                sets: list[str] = []
                params: list[Any] = []
                for key, column in (("display_name", "display_name"), ("role", "role")):
                    if key in data:
                        sets.append(f"{column}=?")
                        params.append(data[key])
                for key, column in (("active", "active"), ("can_download", "can_download"), ("can_history", "can_history")):
                    if key in data:
                        sets.append(f"{column}=?")
                        params.append(1 if data[key] else 0)
                if "password" in data:
                    sets.extend(["password_hash=?", "password_changed_at=?", "must_change_password=1", "failed_login_count=0", "locked_until=NULL"])
                    params.extend([_hash_password(data["password"]), now])
                if sets:
                    sets.extend(["updated_at=?", "edit_revision=edit_revision+1"])
                    params.extend([now, row["id"]])
                    self.store.execute(f"UPDATE identity_users SET {', '.join(sets)} WHERE id=?", params)
                if "scopes" in data:
                    self._replace_scopes(row["id"], data["scopes"], now)
                    if not sets:
                        self.store.execute("UPDATE identity_users SET updated_at=?,edit_revision=edit_revision+1 WHERE id=?", (now, row["id"]))
                if "password" in data or ("active" in data and not data["active"]):
                    self.store.execute("UPDATE identity_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, row["id"]))
                target = self._public_user_from_id(row["id"])
                self._audit(actor, "update_user", row["id"], target["display_name"], self._audit_user_payload(data), now)
                return target

    def audit(self) -> list[dict[str, Any]]:
        rows = self.store.execute("SELECT * FROM identity_audit ORDER BY created_at DESC,id DESC").fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "actor": {"id": row["actor_user_id"], "display_name": row["actor_display_name"]},
                "action": row["action"],
                "target": {"id": row["target_user_id"], "display_name": row["target_display_name"]},
                "detail": _load_json(row["detail"], {}),
            }
            for row in rows
        ]

    def allows_scope(self, user: dict[str, Any], registrant: str, project_category: str, product: str) -> bool:
        if user.get("role") == "admin":
            return True
        scopes = user.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            return False
        target = {
            "registrant": registrant or "",
            "project_category": project_category or "",
            "product": product or "",
        }
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            if all(scope.get(key) == "*" or scope.get(key) == target[key] for key in ("registrant", "project_category", "product")):
                return True
        return False


    def mark_notifications_read(self, user: dict[str, Any]) -> dict[str, Any]:
        user_id = self._require_public_user(user)
        now = _now_iso()
        with self.store.lock, self.store.conn:
            rows = self.store.execute("SELECT id FROM notifications ORDER BY created_at DESC LIMIT 200").fetchall()
            for row in rows:
                self.store.execute(
                    "INSERT OR IGNORE INTO identity_notification_reads(user_id,notification_id,read_at) VALUES(?,?,?)",
                    (user_id, row["id"], now),
                )
            return {"ok": True, "read": len(rows), "read_at": now}

    def read_notification_ids(self, user: dict[str, Any]) -> set[str]:
        user_id = self._require_public_user(user)
        rows = self.store.execute("SELECT notification_id FROM identity_notification_reads WHERE user_id=?", (user_id,)).fetchall()
        return {row["notification_id"] for row in rows}


    def _require_public_user(self, user: dict[str, Any]) -> str:
        if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not user["id"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
        row = self.store.execute("SELECT id FROM identity_users WHERE id=? AND active=1", (user["id"],)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
        return row["id"]

    def _validate_create_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_payload(payload, {"username", "display_name", "password", "role", "active", "scopes", "can_download", "can_history"}, set())
        username = _clean_text(payload["username"], "用户名", max_len=64)
        normalized = _normalize_username(username)
        return {
            "username": username,
            "username_normalized": normalized,
            "display_name": _clean_text(payload["display_name"], "显示名称"),
            "password": _validate_password(payload["password"]),
            "role": _validate_role(payload["role"]),
            "active": _strict_bool(payload["active"], "active"),
            "scopes": self._validate_scopes(payload["scopes"]),
            "can_download": _strict_bool(payload["can_download"], "can_download"),
            "can_history": _strict_bool(payload["can_history"], "can_history"),
        }

    def _validate_update_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"expected_edit_token", "display_name", "password", "role", "active", "scopes", "can_download", "can_history"}
        self._require_payload(payload, set(), allowed)
        if "expected_edit_token" not in payload:
            raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="缺少人员权限编辑校验码，请刷新后再提交")
        data: dict[str, Any] = {}
        token = _clean_text(payload["expected_edit_token"], "expected_edit_token", max_len=64)
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise HTTPException(status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail="人员权限编辑校验码无效，请刷新后再提交")
        data["expected_edit_token"] = token
        if "display_name" in payload:
            data["display_name"] = _clean_text(payload["display_name"], "显示名称")
        if "password" in payload:
            data["password"] = _validate_password(payload["password"])
        if "role" in payload:
            data["role"] = _validate_role(payload["role"])
        for key in ("active", "can_download", "can_history"):
            if key in payload:
                data[key] = _strict_bool(payload[key], key)
        if "scopes" in payload:
            data["scopes"] = self._validate_scopes(payload["scopes"])
        if len(data) == 1:
            raise IdentityError("未提供可更新字段")
        return data

    def _require_payload(self, payload: dict[str, Any], required: set[str], optional: set[str]) -> None:
        if not isinstance(payload, dict):
            raise IdentityError("请求体必须是对象")
        allowed = required | optional
        unknown = set(payload) - allowed
        missing = required - set(payload)
        if unknown:
            raise IdentityError(f"不支持的字段: {', '.join(sorted(unknown))}")
        if missing:
            raise IdentityError(f"缺少字段: {', '.join(sorted(missing))}")

    def _validate_scopes(self, scopes: Any) -> list[dict[str, str]]:
        if not isinstance(scopes, list):
            raise IdentityError("scopes必须是数组")
        if len(scopes) > 100:
            raise IdentityError("scopes不能超过100条")
        clean: list[dict[str, str]] = []
        for item in scopes:
            if not isinstance(item, dict):
                raise IdentityError("scope必须是对象")
            keys = {"registrant", "project_category", "product"}
            if set(item) != keys:
                raise IdentityError("scope字段必须包含registrant、project_category、product")
            clean.append({key: self._scope_value(item[key], key) for key in ("registrant", "project_category", "product")})
        return clean

    def _scope_value(self, value: Any, field: str) -> str:
        text = _clean_text(value, field, max_len=120)
        if field == "project_category" and text not in {"*", "医疗", "家电", "待确认"}:
            raise IdentityError("project_category只能是*、医疗、家电或待确认")
        return "*" if text == "*" else text

    def _create_session(self, user_id: str, now: str) -> str:
        raw = secrets.token_urlsafe(32)
        expires_at = (_now() + _dt.timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
        self.store.execute(
            "INSERT INTO identity_sessions(id,user_id,token_hash,expires_at,created_at,last_seen_at,revoked_at) VALUES(?,?,?,?,?,?,NULL)",
            (f"SESS-{uuid.uuid4().hex}", user_id, _hash_token(raw), expires_at, now, now),
        )
        return raw

    def _replace_scopes(self, user_id: str, scopes: list[dict[str, str]], now: str) -> None:
        self.store.execute("DELETE FROM identity_scopes WHERE user_id=?", (user_id,))
        for scope in scopes:
            self._insert_scope(user_id, scope, now)

    def _insert_scope(self, user_id: str, scope: dict[str, str], now: str) -> None:
        self.store.execute(
            "INSERT INTO identity_scopes(id,user_id,registrant,project_category,product,created_at) VALUES(?,?,?,?,?,?)",
            (f"SCOPE-{uuid.uuid4().hex}", user_id, scope["registrant"], scope["project_category"], scope["product"], now),
        )

    def _public_user_from_id(self, user_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT * FROM identity_users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
        return self._public_user(row)

    def _public_user(self, row) -> dict[str, Any]:
        scopes = self._scopes_for_user(row["id"])
        if row["role"] == "admin":
            scopes = [{"registrant": "*", "project_category": "*", "product": "*"}]
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "active": bool(row["active"]),
            "scopes": scopes,
            "can_download": bool(row["can_download"]),
            "can_history": bool(row["can_history"]),
            "identity_source": row["identity_source"],
            "must_change_password": bool(row["must_change_password"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "edit_token": self._user_edit_token(row),
        }

    def _scopes_for_user(self, user_id: str) -> list[dict[str, str]]:
        return [
            {"registrant": s["registrant"], "project_category": s["project_category"], "product": s["product"]}
            for s in self.store.execute("SELECT registrant,project_category,product FROM identity_scopes WHERE user_id=? ORDER BY created_at,id", (user_id,)).fetchall()
        ]

    def _user_edit_token(self, row) -> str:
        scopes = self._scopes_for_user(row["id"])
        if row["role"] == "admin":
            scopes = [{"registrant": "*", "project_category": "*", "product": "*"}]
        material = {
            "id": row["id"],
            "username": row["username"],
            "username_normalized": row["username_normalized"],
            "display_name": row["display_name"],
            "role": row["role"],
            "active": bool(row["active"]),
            "scopes": scopes,
            "can_download": bool(row["can_download"]),
            "can_history": bool(row["can_history"]),
            "identity_source": row["identity_source"],
            "must_change_password": bool(row["must_change_password"]),
            "password_changed_at": row["password_changed_at"],
            "password_hash": row["password_hash"],
            "edit_revision": int(row["edit_revision"] or 0),
            "created_at": row["created_at"],
        }
        return hashlib.sha256(_json_dump(material).encode("utf-8")).hexdigest()

    def _require_admin(self, actor: dict[str, Any]) -> None:
        if not isinstance(actor, dict) or actor.get("role") != "admin" or actor.get("active") is not True:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")

    def _ensure_another_active_admin(self, current_user_id: str) -> None:
        row = self.store.execute(
            "SELECT COUNT(*) AS count FROM identity_users WHERE role='admin' AND active=1 AND id!=?",
            (current_user_id,),
        ).fetchone()
        if int(row["count"] if row else 0) < 1:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="不能停用或降级最后一个管理员")

    def _audit(self, actor: dict[str, Any] | None, action: str, target_id: str, target_display: str, detail: dict[str, Any], now: str) -> None:
        actor_id = actor.get("id") if isinstance(actor, dict) else None
        actor_display = actor.get("display_name") if isinstance(actor, dict) else "系统初始化"
        self.store.execute(
            """INSERT INTO identity_audit(
                   id,created_at,actor_user_id,actor_display_name,action,target_user_id,target_display_name,detail
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (f"AUD-{uuid.uuid4().hex}", now, actor_id, actor_display, action, target_id, target_display, _json_dump(detail)),
        )

    def _audit_user_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        safe = {key: value for key, value in data.items() if key not in {"password", "password_hash", "token", "raw_token"}}
        if "username_normalized" in safe:
            del safe["username_normalized"]
        return safe
