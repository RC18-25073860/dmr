"""Read-only, directory-level Feishu catalog refresh.

The refresher lists folders and sheet files only. It never reads sheet cells and
never writes cloud content. Existing sheet grids are carried forward as a cache
so locator features keep their last known coordinates until a separate full
snapshot job refreshes them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import tempfile
import re
import subprocess
import threading
import time
from typing import Any

from backend.feishu_catalog import CATALOG_FILE, _validate_snapshot, load_snapshot


ROOT_TOKEN = os.environ.get("DMR_FEISHU_ROOT_TOKEN", "").strip() or "ExampleRootFolderToken000001"
HOST = os.environ.get("DMR_FEISHU_HOST", "https://example.feishu.cn").rstrip("/")
REFRESH_STATUS_FILE = "feishu_catalog_sync.json"
REFRESH_TTL_SECONDS = 60
READ_COMMAND = ("drive", "files", "list")
DEFAULT_DIRECTORY_REFRESH_TIMEOUT_SECONDS = 110
DEFAULT_CLI_CALL_TIMEOUT_SECONDS = 30


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def natural_order(item: dict[str, Any]) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", "/".join(item["path"]))]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temp.replace(path)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


class CatalogDirectoryReader:
    def folder_children(self, folder: dict[str, Any], *, deadline_at: float | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError


class LarkCatalogDirectoryReader(CatalogDirectoryReader):
    def __init__(self, command: str | None = None, timeout: int = DEFAULT_CLI_CALL_TIMEOUT_SECONDS):
        self.command = command or os.environ.get("LARK_CLI") or "lark-cli"
        self.timeout = timeout

    def call(self, *args: str) -> dict[str, Any]:
        if tuple(args[:3]) != READ_COMMAND:
            raise ValueError("Only Feishu drive files list reads are allowed")
        result = subprocess.run(
            [self.command, *args, "--as", "user", "--format", "json"],
            env=dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1"),
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if result.returncode:
            raise RuntimeError("飞书目录读取失败，请检查服务账号授权和网络；原快照已保留")
        try:
            obj = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError("飞书目录读取返回无效结果；原快照已保留") from exc
        if obj.get("ok") is not True:
            raise RuntimeError("飞书目录读取未确认成功；原快照已保留")
        data = obj.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("飞书目录读取结果格式异常；原快照已保留")
        return data

    def folder_children(self, folder: dict[str, Any], *, deadline_at: float | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = None
        seen_pages: set[str] = set()
        while True:
            if deadline_at is not None and time.monotonic() > deadline_at:
                raise RuntimeError("飞书目录读取超时；原快照已保留")
            params = {"folder_token": folder["token"], "page_size": 200}
            if page:
                params["page_token"] = page
            data = self.call("drive", "files", "list", "--params", json.dumps(params, ensure_ascii=False))
            files = data.get("files")
            if not isinstance(files, list):
                raise RuntimeError("飞书目录读取结果缺少文件列表；原快照已保留")
            rows.extend(files)
            if not data.get("has_more"):
                return rows
            page = data.get("next_page_token")
            if not isinstance(page, str) or not page or page in seen_pages:
                raise RuntimeError("飞书目录分页不完整；原快照已保留")
            seen_pages.add(page)


def build_directory_snapshot(
    reader: CatalogDirectoryReader,
    old_snapshot: dict[str, Any] | None = None,
    *,
    deadline_at: float | None = None,
    max_workers: int = 3,
) -> dict[str, Any]:
    root = {"token": ROOT_TOKEN, "parent_token": None, "name": "DMR", "path": ["DMR"]}
    folders = [root]
    queue = [root]
    seen = {ROOT_TOKEN}
    docs: list[dict[str, Any]] = []
    old_docs = {doc["token"]: doc for doc in (old_snapshot or {}).get("documents", [])}
    worker_count = max(1, min(max_workers, 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        while queue:
            if deadline_at is not None and time.monotonic() > deadline_at:
                raise RuntimeError("飞书目录读取超时；原快照已保留")
            batch = queue
            queue = []
            child_lists = pool.map(lambda folder: reader.folder_children(folder, deadline_at=deadline_at), batch)
            for parent, children in zip(batch, child_lists):
                if deadline_at is not None and time.monotonic() > deadline_at:
                    raise RuntimeError("飞书目录读取超时；原快照已保留")
                for item in children:
                    token = item.get("token")
                    name = item.get("name")
                    kind = item.get("type")
                    if not isinstance(token, str) or not isinstance(name, str) or not name.strip():
                        raise RuntimeError("飞书目录条目格式异常；原快照已保留")
                    if token in seen:
                        raise RuntimeError("飞书目录存在重复或循环 token；原快照已保留")
                    seen.add(token)
                    base = {"token": token, "parent_token": parent["token"], "name": name, "path": parent["path"] + [name]}
                    if kind == "folder":
                        folders.append(base)
                        queue.append(base)
                    elif kind == "sheet":
                        cached = old_docs.get(token, {})
                        docs.append(
                            {
                                **base,
                                "type": "sheet",
                                "url": f"{HOST}/sheets/{token}",
                                "revision": cached.get("revision", ""),
                                "read_at": cached.get("read_at", ""),
                                "sheets": cached.get("sheets", []),
                            }
                        )
                    else:
                        raise RuntimeError(f"不支持的飞书目录类型：{kind or '未知'}")
    snapshot = {
        "schema_version": 1,
        "root_token": ROOT_TOKEN,
        "root_url": f"{HOST}/drive/folder/{ROOT_TOKEN}",
        "fetched_at": now_iso(),
        "writeback_enabled": False,
        "folders": sorted(folders, key=natural_order),
        "documents": sorted(docs, key=natural_order),
    }
    _validate_snapshot(snapshot)
    return snapshot


class CatalogRefreshService:
    def __init__(
        self,
        data_dir: Path,
        *,
        reader: CatalogDirectoryReader | None = None,
        ttl_seconds: int = REFRESH_TTL_SECONDS,
        total_timeout_seconds: int = DEFAULT_DIRECTORY_REFRESH_TIMEOUT_SECONDS,
        max_workers: int = 3,
    ):
        self.data_dir = Path(data_dir)
        self.snapshot_path = self.data_dir / CATALOG_FILE
        self.status_path = self.data_dir / REFRESH_STATUS_FILE
        self.lock_path = self.snapshot_path.with_suffix(".lock")
        self.reader = reader or LarkCatalogDirectoryReader()
        self.ttl_seconds = ttl_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.max_workers = max_workers
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        cache_note = {"live": False, "message": "表格单元格沿用上次缓存，实时刷新只更新目录和清单链接。"}
        try:
            state = json.loads(self.status_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return {
                    "status": state.get("status", "unknown"),
                    "checked_at": state.get("checked_at", ""),
                    "started_at": state.get("started_at", ""),
                    "message": state.get("message", ""),
                    "running": self.running(),
                    "directory_only": True,
                    "sheets_live": False,
                    "cell_cache": cache_note,
                    "ttl_seconds": self.ttl_seconds,
                }
        except (OSError, ValueError):
            pass
        return {
            "status": "unknown",
            "checked_at": "",
            "started_at": "",
            "message": "尚未记录目录同步任务状态",
            "running": self.running(),
            "directory_only": True,
            "sheets_live": False,
            "cell_cache": cache_note,
            "ttl_seconds": self.ttl_seconds,
        }

    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def ensure_fresh(self, *, force: bool = False, wait: bool = False, wait_timeout: float = 30) -> dict[str, Any]:
        with self._lock:
            if self.running():
                thread = self._thread
                started = False
            elif not force and self._within_ttl():
                return self.status() | {"started": False, "reason": "ttl"}
            else:
                self._write_status("running", "正在只读刷新飞书目录；不会读取单元格或写入云端。", started_at=now_iso(), checked_at="")
                thread = threading.Thread(target=self._run_guarded, name="dmr-catalog-refresh", daemon=True)
                self._thread = thread
                thread.start()
                started = True
        if wait and thread:
            thread.join(wait_timeout)
        return self.status() | {"started": started}

    def _within_ttl(self) -> bool:
        state = self.status()
        if state.get("status") != "success":
            return False
        checked = _parse_time(state.get("checked_at"))
        if checked is None:
            return False
        return (datetime.now(timezone.utc) - checked).total_seconds() < self.ttl_seconds

    def _run_guarded(self) -> None:
        try:
            self.refresh_now()
        except Exception:
            self._write_status("failed", "目录同步失败，保留上次成功快照；请检查飞书授权、网络和目录结构。", checked_at=now_iso())

    def refresh_now(self) -> dict[str, Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("另一个清单同步任务正在运行；原快照已保留") from exc
            old = None
            try:
                old = load_snapshot(self.snapshot_path)
            except Exception:
                old = None
            deadline_at = time.monotonic() + self.total_timeout_seconds
            snapshot = build_directory_snapshot(self.reader, old, deadline_at=deadline_at, max_workers=self.max_workers)
            _atomic_json(self.snapshot_path, snapshot)
        message = f"只读目录同步完成；表格单元格沿用上次缓存，未实时读取 {len(snapshot['documents'])} 份清单内容。"
        self._write_status("success", message, checked_at=snapshot["fetched_at"])
        return snapshot

    def _write_status(self, status: str, message: str, *, started_at: str = "", checked_at: str = "") -> None:
        previous = self.status()
        payload = {
            "status": status,
            "started_at": started_at or previous.get("started_at", ""),
            "checked_at": checked_at or previous.get("checked_at", ""),
            "message": message,
            "directory_only": True,
            "sheets_live": False,
            "cell_cache": {"live": False, "message": "表格单元格沿用上次缓存，实时刷新只更新目录和清单链接。"},
            "ttl_seconds": self.ttl_seconds,
        }
        _atomic_json(self.status_path, payload)
