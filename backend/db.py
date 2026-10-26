from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DMR_TABLES = {"records", "files", "batches", "batch_items", "exceptions", "notifications", "logs", "settings"}
IDENTITY_TABLES = {"identity_users", "identity_scopes", "identity_sessions", "identity_audit", "identity_notification_reads"}
BASE_REVISION = "20260921_0001"
CURRENT_REVISION = "20261026_0009"
ROOT = Path(__file__).resolve().parents[1]


class MappingResult:
    def __init__(self, result):
        self.rowcount = result.rowcount
        self._rows = result.mappings().all() if result.returns_rows else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        row = self.fetchone()
        if row is None:
            return None
        return next(iter(row.values()))


_PARAM_RE = re.compile(r"\?")


def _named_sql(sql: str, params: Iterable[Any] | dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if isinstance(params, dict):
        return sql, params
    values = list(params or [])
    index = -1

    def repl(_match):
        nonlocal index
        index += 1
        return f":p{index}"

    converted = _PARAM_RE.sub(repl, sql, count=len(values))
    return converted, {f"p{i}": value for i, value in enumerate(values)}


class Database:
    """SQLAlchemy unit-of-work helper with per-operation sessions."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        self._local = threading.local()

    @contextmanager
    def session_scope(self, immediate: bool = False):
        if getattr(self._local, "session", None) is not None:
            yield self._local.session
            return
        session = self.session_factory()
        self._local.session = session
        try:
            if immediate and self.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            else:
                session.begin()
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self._local.session = None
            session.close()

    def __enter__(self):
        stack = getattr(self._local, "context_stack", None)
        if stack is None:
            stack = []
            self._local.context_stack = stack
        ctx = self.session_scope()
        stack.append(ctx)
        ctx.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        stack = self._local.context_stack
        ctx = stack.pop()
        return ctx.__exit__(exc_type, exc, tb)

    def immediate(self):
        return self.session_scope(immediate=True)

    def execute(self, sql: str, params: Iterable[Any] | dict[str, Any] = ()):  # noqa: ANN401
        named_sql, named_params = _named_sql(sql, params)
        session: Session | None = getattr(self._local, "session", None)
        if session is not None:
            return MappingResult(session.execute(text(named_sql), named_params))
        with self.session_scope() as temp_session:
            return MappingResult(temp_session.execute(text(named_sql), named_params))

    def close(self) -> None:
        self.engine.dispose()


def _validate_sqlite_url(url: str, data_dir: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"sqlite", "sqlite+pysqlite"}:
        raise RuntimeError("当前本地版仅启用SQLite；企业DATABASE_URL需目标环境验证后再开启")
    if parsed.path in {"", "/:memory:"}:
        return
    db_path = Path(unquote(parsed.path)).resolve()
    try:
        db_path.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError(f"SQLite DATABASE_URL必须位于920项目内: {ROOT}") from exc
    try:
        db_path.parent.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise RuntimeError(f"SQLite DATABASE_URL必须位于DMR数据目录内: {data_dir}") from exc


def database_url_for(data_dir: Path, database_url: str | None = None) -> str:
    if database_url:
        _validate_sqlite_url(database_url, data_dir)
        return database_url
    return f"sqlite:///{data_dir / 'dmr.sqlite3'}"


def create_dmr_engine(data_dir: Path, database_url: str | None = None) -> Engine:
    url = database_url_for(data_dir, database_url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.close()
    return engine


def _alembic_config(engine: Engine) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    return cfg


EXPECTED_COLUMNS = {
    "records": {"id", "business_code", "name", "registrant", "type", "project_category", "project_category_source", "product", "module", "version", "previous", "file_name", "date", "controlled", "source", "owner", "size", "source_type", "linked_bom", "metadata", "created_at", "updated_at", "edit_revision"},
    "files": {"id", "record_id", "name", "version", "status", "date", "source", "size", "type", "storage", "sha256", "blob_path", "metadata", "linked_bom", "link_status", "created_at"},
    "batches": {"id", "status", "created_at", "updated_at", "summary", "owner_user_id"},
    "batch_items": {"id", "batch_id", "file_name", "staging_file_id", "record_id", "context_record_id", "code", "registrant", "type", "project_category", "project_category_source", "product", "module", "version", "linked_bom", "notes", "metadata", "current_version", "detected_version", "status", "recommendation", "sha256", "size", "result_message", "created_at", "updated_at", "edit_revision"},
    "exceptions": {"id", "title", "detail", "type", "record_id", "item_id", "batch_id", "record_no", "recommendation", "action", "status", "created_at", "resolved_at"},
    "notifications": {"id", "time", "title", "detail", "audience", "product", "read", "created_at"},
    "logs": {"id", "time", "user", "action", "object", "change", "source", "result", "created_at"},
    "settings": {"key", "value"},
    "transfer_manifests": {"id", "client_id", "mailbox", "message_id", "attachment_id", "payload_fingerprint", "filename", "file_date", "sha256", "blob_path", "size", "target_path", "status", "last_error", "created_at", "updated_at"},
    "identity_users": {"id", "username", "username_normalized", "display_name", "role", "active", "password_hash", "password_changed_at", "must_change_password", "can_download", "can_history", "identity_source", "failed_login_count", "locked_until", "created_at", "updated_at", "edit_revision"},
    "identity_scopes": {"id", "user_id", "registrant", "project_category", "product", "created_at"},
    "identity_sessions": {"id", "user_id", "token_hash", "expires_at", "created_at", "last_seen_at", "revoked_at"},
    "identity_audit": {"id", "created_at", "actor_user_id", "actor_display_name", "action", "target_user_id", "target_display_name", "detail"},
    "identity_notification_reads": {"user_id", "notification_id", "read_at"},
}
LEGACY_EXPECTED_COLUMNS = {
    table: {column for column in columns if column not in {"project_category", "project_category_source", "owner_user_id", "edit_revision"}}
    for table, columns in EXPECTED_COLUMNS.items()
    if table not in IDENTITY_TABLES and table != "transfer_manifests"
}


def _validate_existing_schema(engine: Engine, expected_columns: dict[str, set[str]] = EXPECTED_COLUMNS) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing_tables = DMR_TABLES - tables
    if missing_tables:
        raise RuntimeError(f"DMR数据库表不完整，缺少: {', '.join(sorted(missing_tables))}")
    for table, expected in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing_columns = expected - actual
        if missing_columns:
            raise RuntimeError(f"DMR数据库表{table}缺少列: {', '.join(sorted(missing_columns))}")
    file_fks = {(fk["constrained_columns"][0], fk["referred_table"]) for fk in inspector.get_foreign_keys("files") if fk.get("constrained_columns")}
    item_fks = {(fk["constrained_columns"][0], fk["referred_table"]) for fk in inspector.get_foreign_keys("batch_items") if fk.get("constrained_columns")}
    if ("record_id", "records") not in file_fks:
        raise RuntimeError("DMR数据库files.record_id缺少records外键")
    if ("batch_id", "batches") not in item_fks:
        raise RuntimeError("DMR数据库batch_items.batch_id缺少batches外键")


def _run_alembic(engine: Engine, op: str, revision: str | None = None) -> None:
    cfg = _alembic_config(engine)
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        if op == "upgrade":
            command.upgrade(cfg, "head")
        elif op == "stamp":
            command.stamp(cfg, revision or CURRENT_REVISION)
        else:
            raise ValueError(op)


def init_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if DMR_TABLES & tables:
        if "alembic_version" in tables:
            _run_alembic(engine, "upgrade")
            _validate_existing_schema(engine)
            return
        _validate_existing_schema(engine, LEGACY_EXPECTED_COLUMNS)
        _run_alembic(engine, "stamp", BASE_REVISION)
        _run_alembic(engine, "upgrade")
        _validate_existing_schema(engine)
        return
    if "alembic_version" in tables:
        _run_alembic(engine, "upgrade")
        return
    _run_alembic(engine, "upgrade")
