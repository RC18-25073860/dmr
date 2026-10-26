from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8920
LOCAL_HOSTS = ("127.0.0.1", "localhost")
LOCAL_ORIGINS = ("http://127.0.0.1", "http://localhost")


@dataclass(frozen=True)
class AppConfig:
    root: Path
    data_dir: Path
    database_url: str | None
    auth_mode: str
    static_root: Path
    local_user: str = "local-user"
    local_roles: str = "admin,document_control,reader,uploader"
    environment: str = "local"
    bind_host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    trusted_origins: tuple[str, ...] = LOCAL_ORIGINS
    public_origin: str | None = None
    cookie_secure: bool = False
    setup_allowed: bool = True


def _split_csv(raw: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(f"{name}必须是true或false")


def _normalize_host(raw: str) -> str:
    value = raw.strip().lower()
    if not value or value == "null" or "*" in value or "/" in value or "@" in value:
        raise RuntimeError("DMR_ALLOWED_HOSTS只能填写明确的host或host:port，不能使用通配符")
    if value.startswith("[") and "]" in value:
        return value
    if value.count(":") > 1:
        raise RuntimeError("DMR_ALLOWED_HOSTS中的IPv6地址需使用[::1]:端口格式")
    return value


def _normalize_origin(raw: str) -> str:
    value = raw.strip()
    if not value or value.lower() == "null" or "*" in value:
        raise RuntimeError("DMR_TRUSTED_ORIGINS只能填写完整明确的http(s)来源，不能使用通配符")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("DMR_TRUSTED_ORIGINS端口格式不正确") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise RuntimeError("DMR_TRUSTED_ORIGINS必须是完整来源，例如https://dmr.example.com")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _default_hosts(port: int) -> tuple[str, ...]:
    return LOCAL_HOSTS + tuple(f"{host}:{port}" for host in LOCAL_HOSTS)


def _default_origins(port: int) -> tuple[str, ...]:
    return LOCAL_ORIGINS + tuple(f"http://{host}:{port}" for host in LOCAL_HOSTS)


def validate_deployment_config(config: AppConfig, *, explicit_hosts: bool = True, explicit_origins: bool = True, explicit_bind_host: bool = True) -> None:
    environment = (config.environment or "local").strip().lower()
    allowed_hosts = tuple(_normalize_host(host) for host in config.allowed_hosts)
    trusted_origins = tuple(_normalize_origin(origin) for origin in config.trusted_origins)
    if not allowed_hosts:
        raise RuntimeError("DMR_ALLOWED_HOSTS不能为空")
    if not trusted_origins:
        raise RuntimeError("DMR_TRUSTED_ORIGINS不能为空")
    if config.public_origin:
        public_origin = _normalize_origin(config.public_origin)
        if public_origin not in trusted_origins:
            raise RuntimeError("DMR_PUBLIC_ORIGIN必须包含在DMR_TRUSTED_ORIGINS中")
    if environment in {"prod", "production"}:
        if not config.public_origin or not _normalize_origin(config.public_origin).startswith("https://"):
            raise RuntimeError("生产环境必须配置https的DMR_PUBLIC_ORIGIN")
        if any(not origin.startswith("https://") for origin in trusted_origins):
            raise RuntimeError("生产环境DMR_TRUSTED_ORIGINS只能使用https来源")
        if not config.cookie_secure:
            raise RuntimeError("生产环境必须启用DMR_COOKIE_SECURE=true")
        if config.setup_allowed:
            raise RuntimeError("生产环境必须关闭首次管理员初始化：DMR_SETUP_ALLOWED=false")
        if not explicit_hosts or not explicit_origins:
            raise RuntimeError("生产环境必须显式配置DMR_ALLOWED_HOSTS和DMR_TRUSTED_ORIGINS")
        if config.bind_host == "0.0.0.0" and not explicit_bind_host:
            raise RuntimeError("监听0.0.0.0必须显式配置DMR_BIND_HOST")


def resolve_data_dir(raw: str | None = None) -> Path:
    raw_value = raw or os.environ.get("DMR_DATA_DIR", str(ROOT / "data"))
    data_dir = Path(raw_value)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir = data_dir.resolve()
    try:
        data_dir.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError(f"DMR_DATA_DIR必须位于项目目录内: {ROOT}") from exc
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "files").mkdir(exist_ok=True)
    return data_dir


def load_config(data_dir: Path | None = None) -> AppConfig:
    resolved_data_dir = data_dir.resolve() if data_dir else resolve_data_dir()
    try:
        resolved_data_dir.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError(f"数据目录必须位于项目目录内: {ROOT}") from exc
    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    (resolved_data_dir / "files").mkdir(exist_ok=True)
    port = int(os.environ.get("DMR_PORT", DEFAULT_PORT))
    allowed_hosts_raw = os.environ.get("DMR_ALLOWED_HOSTS")
    trusted_origins_raw = os.environ.get("DMR_TRUSTED_ORIGINS")
    environment = os.environ.get("DMR_ENV", "local").strip().lower() or "local"
    cookie_secure = _env_bool("DMR_COOKIE_SECURE", environment in {"prod", "production"})
    setup_allowed = _env_bool("DMR_SETUP_ALLOWED", environment not in {"prod", "production"})
    config = AppConfig(
        root=ROOT,
        data_dir=resolved_data_dir,
        database_url=None if data_dir else os.environ.get("DATABASE_URL"),
        auth_mode=os.environ.get("DMR_AUTH_MODE", "local").strip().lower() or "local",
        static_root=ROOT / "frontend" / "dist",
        local_user=os.environ.get("DMR_LOCAL_USER", "local-user"),
        local_roles=os.environ.get("DMR_LOCAL_ROLES", "admin,document_control,reader,uploader"),
        environment=environment,
        bind_host=os.environ.get("DMR_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=port,
        allowed_hosts=tuple(_normalize_host(host) for host in (_split_csv(allowed_hosts_raw) or _default_hosts(port))),
        trusted_origins=tuple(_normalize_origin(origin) for origin in (_split_csv(trusted_origins_raw) or _default_origins(port))),
        public_origin=os.environ.get("DMR_PUBLIC_ORIGIN") or None,
        cookie_secure=cookie_secure,
        setup_allowed=setup_allowed,
    )
    validate_deployment_config(
        config,
        explicit_hosts=allowed_hosts_raw is not None,
        explicit_origins=trusted_origins_raw is not None,
        explicit_bind_host="DMR_BIND_HOST" in os.environ,
    )
    return config
