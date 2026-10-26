from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from backend.config import AppConfig


@dataclass(frozen=True)
class Principal:
    user_id: str
    display_name: str
    roles: frozenset[str]
    mode: str

    def can(self, permission: str) -> bool:
        if "admin" in self.roles:
            return True
        if permission in {"read", "preview", "download"} and "reader" in self.roles:
            return True
        if permission in {"read", "preview", "download", "analyze", "review"} and "uploader" in self.roles:
            return True
        if permission in {"read", "preview", "download", "analyze", "review", "commit", "sync", "resolve", "notify", "classify"} and "document_control" in self.roles:
            return True
        return False


class AuthContext:
    def __init__(self, mode: str = "local"):
        self.mode = mode

    def principal(self, configured_user: str = "local-user", configured_roles: str = "admin,document_control,reader") -> Principal:
        if self.mode != "local":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="企业身份未配置，拒绝执行受控操作")
        role_set = frozenset(r.strip() for r in configured_roles.split(",") if r.strip())
        return Principal(user_id=configured_user, display_name=configured_user or "本地用户", roles=role_set, mode="local")

    def require(self, permission: str, principal: Principal) -> None:
        if not principal.can(permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前本地角色无权执行该操作")


def _origin_from_request(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is None:
        return None
    if origin.strip().lower() == "null":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许")
    try:
        parsed = urlsplit(origin.strip())
        _ = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _normalized_host(request: Request) -> str:
    host = request.headers.get("host", "").strip().lower()
    if not host or "/" in host or "@" in host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Host不允许")
    return host


def check_request(request: Request, config: AppConfig, *, allow_missing_origin: bool = False) -> None:
    host = _normalized_host(request)
    allowed_hosts = {item.lower() for item in config.allowed_hosts}
    if host not in allowed_hosts:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Host不允许")

    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="跨站请求不允许")

    origin = _origin_from_request(request)
    unsafe_method = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    environment = (config.environment or "local").strip().lower()
    if origin is None:
        if environment in {"prod", "production"} and unsafe_method and not allow_missing_origin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="生产环境必须提供Origin")
        return

    trusted_origins = {item.lower() for item in config.trusted_origins}
    if origin not in trusted_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许")
    if environment not in {"prod", "production"} and urlsplit(origin).netloc.lower() != host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许")


def check_loopback_request(request: Request, config: AppConfig | None = None) -> None:
    if config is not None:
        check_request(request, config)
        return

    host = request.headers.get("host", "")
    allowed_hosts = {"127.0.0.1", "localhost"}
    host_name = host.split(":", 1)[0]
    if host_name not in allowed_hosts:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅允许本机访问")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != host_name:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许")
        try:
            request_port = int(host.rsplit(":", 1)[1]) if ":" in host else 80
            origin_port = parsed.port or 80
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许") from exc
        if origin_port != request_port:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin不允许")
