#!/usr/bin/env python3
"""Sync matching Lark mail attachments into DMR mail ingest.

The script keeps secrets outside source code, downloads attachments into a
temporary directory only, expands archives safely, and records each successfully
uploaded child file so interrupted runs can retry the remaining files.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import fcntl
import gzip
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_FILE = ROOT / "processed.json"
DEFAULT_CONFIG_FILE = ROOT / "mail_sync_config.json"

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_FILES = 200
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".gz")
TRANSFER_MANIFEST_PREFIX = "DMR文件转移清单"
EXCLUDED_MAIL_SUBJECT = re.compile(r"(?<![A-Za-z])(?:ECN|ENC)(?![A-Za-z])", re.I)


class MailSyncError(RuntimeError):
    pass


class CliError(MailSyncError):
    pass


class DownloadError(MailSyncError):
    pass


class ArchiveSafetyError(MailSyncError):
    pass


@dataclasses.dataclass(frozen=True)
class MailSyncConfig:
    dmr_base: str = "http://localhost:8920"
    dmr_token: str = ""
    mailbox: str = "operator@example.com"
    folder: str = "SENT"
    include_keywords: tuple[str, ...] = ("建筑",)
    transfer_manifest_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    state_file: Path = DEFAULT_STATE_FILE
    delivery_mode: str = "batch"  # batch | legacy
    batch_grouping: str = "message"  # message | attachment
    intake_only: bool = False  # transfer manifests stay on the platform; no 192 write
    dry_run: bool = False
    triage_max: int = 400
    request_timeout: int = 60
    download_timeout: int = 120
    max_file_bytes: int = MAX_FILE_BYTES
    max_batch_bytes: int = MAX_BATCH_BYTES
    max_archive_files: int = MAX_ARCHIVE_FILES
    max_archive_bytes: int = MAX_ARCHIVE_BYTES

    @property
    def ingest_attachments_url(self) -> str:
        return self.dmr_base.rstrip("/") + "/api/integrations/mail/attachments"

    @property
    def ingest_batches_url(self) -> str:
        return self.dmr_base.rstrip("/") + "/api/integrations/mail/batches"

    @property
    def ingest_transfer_manifest_url(self) -> str:
        return self.dmr_base.rstrip("/") + "/api/integrations/mail/transfer-manifests"

    @classmethod
    def from_sources(cls, args: argparse.Namespace) -> "MailSyncConfig":
        raw: dict[str, Any] = {}
        config_file = Path(args.config or os.environ.get("DMR_MAIL_CONFIG") or DEFAULT_CONFIG_FILE)
        if config_file.exists():
            with config_file.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise MailSyncError("config file must contain a JSON object")
            raw.update(loaded)

        def pick(name: str, env_name: str, default: Any = None) -> Any:
            return os.environ.get(env_name, raw.get(name, default))

        def keywords(value: Any) -> tuple[str, ...]:
            if value is None:
                return ()
            if isinstance(value, str):
                parts = value.replace("；", ",").replace("，", ",").split(",")
            elif isinstance(value, list):
                parts = value
            else:
                raise MailSyncError("keyword fields must be string or list")
            cleaned = tuple(str(part).strip() for part in parts if str(part).strip())
            return cleaned

        state_value = args.state_file or pick("state_file", "DMR_MAIL_STATE_FILE", str(DEFAULT_STATE_FILE))
        include_value = args.keyword or pick("include_keywords", "DMR_MAIL_KEYWORDS", raw.get("biz_keyword", "建筑"))
        exclude_value = args.exclude_keyword or pick("exclude_keywords", "DMR_MAIL_EXCLUDE_KEYWORDS", "")
        mode = str(args.delivery_mode or pick("delivery_mode", "DMR_MAIL_DELIVERY_MODE", "batch")).strip().lower()
        grouping = str(args.batch_grouping or pick("batch_grouping", "DMR_MAIL_BATCH_GROUPING", "message")).strip().lower()
        if mode not in {"batch", "legacy"}:
            raise MailSyncError("delivery_mode must be batch or legacy")
        if grouping not in {"message", "attachment"}:
            raise MailSyncError("batch_grouping must be message or attachment")

        token = str(args.token or pick("dmr_token", "DMR_MAIL_TOKEN", "")).strip()
        config = cls(
            dmr_base=str(args.dmr_base or pick("dmr_base", "DMR_MAIL_BASE_URL", "http://localhost:8920")).strip(),
            dmr_token=token,
            mailbox=str(args.mailbox or pick("mailbox", "DMR_MAILBOX", "operator@example.com")).strip().lower(),
            folder=str(args.folder or pick("folder", "DMR_MAIL_FOLDER", "SENT")).strip(),
            include_keywords=keywords(include_value) or ("建筑",),
            transfer_manifest_keywords=keywords(getattr(args, "transfer_keyword", None) or pick("transfer_manifest_keywords", "DMR_MAIL_TRANSFER_KEYWORDS", "")),
            exclude_keywords=keywords(exclude_value),
            state_file=Path(state_value),
            delivery_mode=mode,
            batch_grouping=grouping,
            intake_only=bool(getattr(args, "intake_only", False) or str(pick("intake_only", "DMR_MAIL_INTAKE_ONLY", False)).lower() in {"true", "1", "yes"}),
            dry_run=bool(args.dry_run),
            triage_max=int(pick("triage_max", "DMR_MAIL_TRIAGE_MAX", 400)),
            request_timeout=int(pick("request_timeout", "DMR_MAIL_REQUEST_TIMEOUT", 60)),
            download_timeout=int(pick("download_timeout", "DMR_MAIL_DOWNLOAD_TIMEOUT", 120)),
            max_file_bytes=int(pick("max_file_bytes", "DMR_MAIL_MAX_FILE_BYTES", MAX_FILE_BYTES)),
            max_batch_bytes=int(pick("max_batch_bytes", "DMR_MAIL_MAX_BATCH_BYTES", MAX_BATCH_BYTES)),
            max_archive_files=int(pick("max_archive_files", "DMR_MAIL_MAX_ARCHIVE_FILES", MAX_ARCHIVE_FILES)),
            max_archive_bytes=int(pick("max_archive_bytes", "DMR_MAIL_MAX_ARCHIVE_BYTES", MAX_ARCHIVE_BYTES)),
        )
        if not config.dry_run and not config.dmr_token:
            raise MailSyncError("DMR_MAIL_TOKEN is required unless --dry-run is used")
        return config


@dataclasses.dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(self, argv: list[str]) -> CliResult:
        env = os.environ.copy()
        env.setdefault("LARKSUITE_CLI_NO_UPDATE_NOTIFIER", "1")
        env.setdefault("LARKSUITE_CLI_NO_SKILLS_NOTIFIER", "1")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise CliError("lark-cli command timed out after 120 seconds") from exc
        return CliResult(proc.returncode, proc.stdout, proc.stderr)


class Downloader:
    def download(self, url: str, dest: Path, timeout: int) -> None:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise DownloadError(f"download returned HTTP {status}")
                with dest.open("wb") as fh:
                    shutil.copyfileobj(response, fh)
        except urllib.error.HTTPError as exc:
            raise DownloadError(f"download returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise DownloadError(f"download failed: {exc.reason}") from exc


class DmrUploader:
    def __init__(self, config: MailSyncConfig):
        self.config = config

    def post_json(self, url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.config.dmr_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise MailSyncError(f"DMR HTTP {exc.code}: {body[:300]}") from exc
        except urllib.error.URLError as exc:
            raise MailSyncError(f"DMR request failed: {exc.reason}") from exc


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.data: dict[str, Any] = {"done": [], "files": {}}
        self._lock_fh = None

    def __enter__(self) -> "StateStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = self.lock_path.open("a+")
        fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.data = self._load()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._lock_fh:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            self._lock_fh.close()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"done": [], "files": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MailSyncError(f"state file is not valid JSON: {self.path}") from exc
        if not isinstance(loaded, dict):
            raise MailSyncError("state file must contain a JSON object")
        done = loaded.get("done", [])
        files = loaded.get("files", {})
        if not isinstance(done, list):
            done = []
        if not isinstance(files, dict):
            files = {}
        return dict(loaded) | {"done": sorted({str(item) for item in done}), "files": files}

    def save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    @property
    def done(self) -> set[str]:
        return set(self.data.get("done", []))

    @property
    def files(self) -> dict[str, Any]:
        return self.data.setdefault("files", {})

    def is_source_done(self, key: str) -> bool:
        return key in self.done

    def is_file_done(self, key: str) -> bool:
        return self.files.get(key, {}).get("status") == "uploaded"

    def mark_file_done(self, key: str, receipt: dict[str, Any]) -> None:
        self.files[key] = {
            "status": "uploaded",
            "receipt_id": receipt.get("receipt_id", ""),
            "batch_id": receipt.get("batch_id", ""),
            "item_id": receipt.get("item_id", ""),
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def mark_source_done(self, key: str) -> None:
        done = self.done
        done.add(key)
        self.data["done"] = sorted(done)


@dataclasses.dataclass(frozen=True)
class ExpandedFile:
    source_key: str
    file_key: str
    attachment_id: str
    filename: str
    path: Path
    size: int
    sha256: str
    content_type: str


@dataclasses.dataclass
class SyncStats:
    considered_messages: int = 0
    skipped_messages: int = 0
    downloaded_attachments: int = 0
    planned_files: int = 0
    skipped_done_files: int = 0
    uploaded_files: int = 0
    failed_files: int = 0
    dry_run_files: int = 0
    failed_groups: int = 0

    def failed(self) -> bool:
        return self.failed_files > 0 or self.failed_groups > 0


def parse_json_envelope(result: CliResult) -> dict[str, Any]:
    text = result.stdout if result.returncode == 0 else (result.stderr or result.stdout)
    if result.returncode != 0:
        detail = _json_message(text) or text.strip()[:300]
        raise CliError(f"lark-cli failed with exit {result.returncode}: {detail}")
    payload = _first_json(text)
    if not isinstance(payload, dict):
        raise CliError("lark-cli did not return a JSON object")
    if payload.get("ok") is False:
        raise CliError("lark-cli returned ok:false: " + _json_message(text))
    return payload


def _json_message(text: str) -> str:
    with contextlib.suppress(Exception):
        obj = _first_json(text)
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("hint") or err)
            return str(obj.get("message") or obj.get("detail") or "")
    return ""


def _first_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        with contextlib.suppress(json.JSONDecodeError):
            obj, _ = decoder.raw_decode(text[index:])
            if isinstance(obj, dict) and obj.get("saved_path"):
                saved_path = Path(obj["saved_path"])
                if saved_path.exists():
                    return json.loads(saved_path.read_text(encoding="utf-8"))
            return obj
    raise CliError("no JSON object found in lark-cli output")


def messages_from_triage(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    messages = data.get("messages") or data.get("items") or payload.get("messages") or []
    return [item for item in messages if isinstance(item, dict)]


def next_page_token_from_triage(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    token = data.get("next_page_token") or data.get("page_token") or payload.get("next_page_token") or ""
    return str(token).strip()


def has_more_from_triage(payload: dict[str, Any]) -> bool:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    value = data.get("has_more", payload.get("has_more", False))
    return value is True or str(value).strip().lower() == "true"


def message_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    messages = data.get("messages") or payload.get("messages") or []
    return [item for item in messages if isinstance(item, dict)]


def subject_matches(subject: str, includes: Iterable[str], excludes: Iterable[str]) -> bool:
    return (not EXCLUDED_MAIL_SUBJECT.search(subject or "")
            and any(keyword in subject for keyword in includes)
            and not any(keyword in subject for keyword in excludes))


def internal_date_to_iso(value: Any) -> str:
    with contextlib.suppress(Exception):
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_name(name: str) -> str:
    text = Path(name or "unnamed").name.strip() or "unnamed"
    return text.replace("\x00", "")


def is_archive(path: Path) -> bool:
    lower = path.name.lower()
    return lower.endswith(ARCHIVE_SUFFIXES)


def safe_extract(path: Path, dest: Path, config: MailSyncConfig) -> list[Path]:
    if not is_archive(path):
        return [path]
    if zipfile.is_zipfile(path):
        return _extract_zip(path, dest, config)
    if tarfile.is_tarfile(path):
        return _extract_tar(path, dest, config)
    if path.name.lower().endswith(".gz") and not path.name.lower().endswith((".tar.gz", ".tgz")):
        return _extract_gzip(path, dest, config)
    return [path]


def _safe_output_path(dest: Path, member_name: str) -> Path:
    if not member_name or "\x00" in member_name:
        raise ArchiveSafetyError("archive member has an invalid name")
    target = (dest / member_name).resolve()
    root = dest.resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ArchiveSafetyError(f"archive member escapes target directory: {member_name}")
    return target


def _extract_zip(path: Path, dest: Path, config: MailSyncConfig) -> list[Path]:
    out_dir = dest / (path.stem + "_unzipped")
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > config.max_archive_files:
            raise ArchiveSafetyError(f"archive contains more than {config.max_archive_files} files")
        for info in infos:
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ArchiveSafetyError(f"archive contains a symlink: {info.filename}")
            total += info.file_size
            if info.file_size > config.max_file_bytes or total > config.max_archive_bytes:
                raise ArchiveSafetyError("archive exceeds configured size limits")
            target = _safe_output_path(out_dir, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    return extracted or [path]


def _extract_tar(path: Path, dest: Path, config: MailSyncConfig) -> list[Path]:
    out_dir = dest / (path.name.replace(".", "_") + "_untarred")
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        files = [member for member in members if member.isfile()]
        if len(files) > config.max_archive_files:
            raise ArchiveSafetyError(f"archive contains more than {config.max_archive_files} files")
        for member in members:
            if member.isdir():
                continue
            if member.issym() or member.islnk() or not member.isfile():
                raise ArchiveSafetyError(f"archive contains unsupported member: {member.name}")
            total += member.size
            if member.size > config.max_file_bytes or total > config.max_archive_bytes:
                raise ArchiveSafetyError("archive exceeds configured size limits")
            target = _safe_output_path(out_dir, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            src = archive.extractfile(member)
            if src is None:
                raise ArchiveSafetyError(f"archive member cannot be read: {member.name}")
            with src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    return extracted or [path]


def _extract_gzip(path: Path, dest: Path, config: MailSyncConfig) -> list[Path]:
    if path.stat().st_size > config.max_archive_bytes:
        raise ArchiveSafetyError("gzip file exceeds configured size limits")
    target_name = path.name[:-3] or path.stem or "decompressed"
    target = _safe_output_path(dest / (path.stem + "_gunzipped"), target_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with gzip.open(path, "rb") as src, target.open("wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > config.max_archive_bytes or total > config.max_file_bytes:
                raise ArchiveSafetyError("gzip content exceeds configured size limits")
            dst.write(chunk)
    return [target]


def attachment_payload(file: ExpandedFile) -> dict[str, Any]:
    raw = file.path.read_bytes()
    return {
        "attachment_id": file.attachment_id,
        "filename": file.filename,
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "size": len(raw),
        "content_type": file.content_type,
        "sha256": file.sha256,
    }


class MailSync:
    def __init__(
        self,
        config: MailSyncConfig,
        runner: CommandRunner | None = None,
        downloader: Downloader | None = None,
        uploader: DmrUploader | None = None,
        log=None,
    ):
        self.config = config
        self.runner = runner or CommandRunner()
        self.downloader = downloader or Downloader()
        self.uploader = uploader or DmrUploader(config)
        self.log = log or (lambda message: print(message, flush=True))

    def run(self) -> SyncStats:
        with StateStore(self.config.state_file) as state:
            with tempfile.TemporaryDirectory(prefix="dmr_mail_") as tmp:
                stats = self._run_with_state(state, Path(tmp))
                if not self.config.dry_run:
                    state.save()
                return stats

    def _run_with_state(self, state: StateStore, tmpdir: Path) -> SyncStats:
        stats = SyncStats()
        messages = self._triage_messages()
        stats.considered_messages = len(messages)
        if not messages:
            self.log("No matching mail messages found.")
            return stats
        details = self._fetch_details(messages)
        groups: dict[str, dict[str, Any]] = {}
        for message in details:
            subject = message.get("subject") or ""
            all_keywords = (*self.config.include_keywords, *self.config.transfer_manifest_keywords)
            if not subject_matches(subject, all_keywords, self.config.exclude_keywords):
                stats.skipped_messages += 1
                continue
            self._collect_message_files(message, tmpdir, state, stats, groups)
        self._send_groups(groups, state, stats)
        return stats

    def _triage_messages(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        filter_json = json.dumps({"folder": self.config.folder}, ensure_ascii=False)
        all_keywords = tuple(dict.fromkeys((*self.config.include_keywords, *self.config.transfer_manifest_keywords)))
        for keyword in all_keywords:
            page_token = ""
            seen_page_tokens: set[str] = set()
            while True:
                argv = [
                    "lark-cli",
                    "mail",
                    "+triage",
                    "--as",
                    "user",
                    "--mailbox",
                    self.config.mailbox,
                    "--filter",
                    filter_json,
                    "--query",
                    keyword[:50],
                    "--max",
                    str(self.config.triage_max),
                    "--format",
                    "json",
                ]
                if page_token:
                    argv.extend(["--page-token", page_token])
                payload = parse_json_envelope(self.runner.run(argv))
                for message in messages_from_triage(payload):
                    mid = str(message.get("message_id") or "")
                    subject = str(message.get("subject") or "")
                    if mid and subject_matches(subject, all_keywords, self.config.exclude_keywords):
                        merged[mid] = message
                page_token = next_page_token_from_triage(payload)
                if page_token in seen_page_tokens:
                    raise CliError("lark-cli returned a repeated page token; refusing to truncate or loop")
                if not page_token:
                    if has_more_from_triage(payload):
                        raise CliError("lark-cli reported more mail pages but returned no page token")
                    break
                seen_page_tokens.add(page_token)
        return list(merged.values())

    def _fetch_details(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = ",".join(str(message["message_id"]) for message in messages if message.get("message_id"))
        if not ids:
            return []
        payload = parse_json_envelope(self.runner.run([
            "lark-cli",
            "mail",
            "+messages",
            "--as",
            "user",
            "--mailbox",
            self.config.mailbox,
            "--message-ids",
            ids,
            "--html=false",
            "--format",
            "json",
        ]))
        return message_details(payload)

    def _collect_message_files(
        self,
        message: dict[str, Any],
        tmpdir: Path,
        state: StateStore,
        stats: SyncStats,
        groups: dict[str, dict[str, Any]],
    ) -> None:
        message_id = str(message.get("message_id") or "")
        meta = {
            "mailbox": self.config.mailbox,
            "message_id": message_id,
            "subject": str(message.get("subject") or ""),
            "sender": sender_address(message, self.config.mailbox),
            "received_at": internal_date_to_iso(message.get("internal_date") or message.get("received_at") or 0),
            "biz_keyword": matched_keyword(str(message.get("subject") or ""), (*self.config.transfer_manifest_keywords, *self.config.include_keywords)),
        }
        for attachment in message.get("attachments", []) or []:
            if attachment.get("is_inline"):
                continue
            attachment_id = str(attachment.get("id") or attachment.get("attachment_id") or "")
            if not attachment_id:
                continue
            source_key = f"{message_id}:{attachment_id}"
            if state.is_source_done(source_key):
                continue
            filename = safe_name(str(attachment.get("filename") or "unnamed"))
            try:
                local_path = self._download_attachment(message_id, attachment_id, filename, tmpdir)
                stats.downloaded_attachments += 1
                extracted = safe_extract(local_path, tmpdir, self.config)
                files = [self._expanded_file(source_key, attachment_id, path, local_path, filename) for path in extracted]
            except MailSyncError as exc:
                stats.failed_files += 1
                self.log(f"FAILED download/extract {redact_id(source_key)} {filename}: {exc}")
                group_key = message_id if self.config.batch_grouping == "message" else source_key
                group = groups.setdefault(group_key, {"meta": meta, "source_keys": set(), "files": [], "blocked_errors": []})
                group["source_keys"].add(source_key)
                group["blocked_errors"].append(f"{filename}: {exc}")
                continue
            group_key = message_id if self.config.batch_grouping == "message" else source_key
            group = groups.setdefault(group_key, {"meta": meta, "source_keys": set(), "files": [], "blocked_errors": []})
            group["source_keys"].add(source_key)
            group["files"].extend(files)
            stats.planned_files += len(files)

    def _download_attachment(self, message_id: str, attachment_id: str, filename: str, tmpdir: Path) -> Path:
        params = {"user_mailbox_id": "me", "message_id": message_id, "attachment_ids": [attachment_id]}
        payload = parse_json_envelope(self.runner.run([
            "lark-cli",
            "mail",
            "user_mailbox.message.attachments",
            "download_url",
            "--as",
            "user",
            "--params",
            json.dumps(params, ensure_ascii=False),
            "--format",
            "json",
        ]))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        urls = data.get("download_urls") or []
        if not urls:
            raise DownloadError("no download URL returned")
        download_url = urls[0].get("download_url") if isinstance(urls[0], dict) else urls[0]
        if not isinstance(download_url, str) or not download_url:
            raise DownloadError("download URL is empty")
        dest = tmpdir / f"{hashlib.sha256((message_id + attachment_id).encode()).hexdigest()}_{filename}"
        self.downloader.download(download_url, dest, self.config.download_timeout)
        if not dest.exists() or dest.stat().st_size <= 0:
            raise DownloadError("downloaded file is empty")
        if dest.stat().st_size > self.config.max_file_bytes and not is_archive(dest):
            raise DownloadError("downloaded file exceeds configured size limit")
        return dest

    def _expanded_file(self, source_key: str, attachment_id: str, path: Path, parent_path: Path, original_name: str) -> ExpandedFile:
        size = path.stat().st_size
        if size <= 0:
            raise DownloadError("empty child file")
        if size > self.config.max_file_bytes:
            raise DownloadError("child file exceeds configured size limit")
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = original_name if path == parent_path else str(path.resolve().relative_to(parent_path.parent.resolve())).replace(os.sep, "/")
        child_attachment_id = f"{attachment_id}/{relative}"
        file_key = f"{source_key}:{relative}:{sha256}"
        return ExpandedFile(
            source_key=source_key,
            file_key=file_key,
            attachment_id=child_attachment_id,
            filename=original_name if path == parent_path else safe_name(path.name),
            path=path,
            size=size,
            sha256=sha256,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )

    def _send_groups(self, groups: dict[str, dict[str, Any]], state: StateStore, stats: SyncStats) -> None:
        for group_key, group in groups.items():
            blocked_errors = group.get("blocked_errors") or []
            if blocked_errors:
                stats.failed_groups += 1
                self.log(f"FAILED group {redact_id(group_key)}: waiting to retry all related attachment(s): {'; '.join(blocked_errors)}")
                continue
            files = [file for file in group["files"] if not state.is_file_done(file.file_key)]
            stats.skipped_done_files += len(group["files"]) - len(files)
            if not files:
                for source_key in group["source_keys"]:
                    state.mark_source_done(source_key)
                continue
            if len(files) > self.config.max_archive_files:
                stats.failed_groups += 1
                self.log(f"FAILED group {redact_id(group_key)}: more than {self.config.max_archive_files} files")
                continue
            total_size = sum(file.size for file in files)
            if total_size > self.config.max_batch_bytes:
                stats.failed_groups += 1
                self.log(f"FAILED group {redact_id(group_key)}: total size exceeds batch limit")
                continue
            if self.config.dry_run:
                stats.dry_run_files += len(files)
                self.log(f"DRY-RUN group {redact_id(group_key)}: {len(files)} file(s), {total_size} bytes")
                continue
            # Route even malformed transfer forms to the dedicated validator.
            # They must never silently become ordinary controlled DMR files.
            manifests = [file for file in files if TRANSFER_MANIFEST_PREFIX in file.filename]
            ordinary = [file for file in files if file not in manifests]
            for file in manifests:
                try:
                    self._send_transfer_manifest(group["meta"], file, state, stats)
                except MailSyncError as exc:
                    stats.failed_files += 1
                    self.log(f"FAILED transfer manifest {file.filename}: {exc}")
            if ordinary:
                try:
                    if self.config.delivery_mode == "legacy":
                        self._send_legacy_files(group["meta"], ordinary, state, stats)
                    else:
                        self._send_batch(group["meta"], ordinary, state, stats)
                except MailSyncError as exc:
                    stats.failed_groups += 1
                    self.log(f"FAILED group {redact_id(group_key)}: {exc}")
            source_keys = set(group["source_keys"])
            for source_key in source_keys:
                source_files = [file for file in group["files"] if file.source_key == source_key]
                if source_files and all(state.is_file_done(file.file_key) for file in source_files):
                    state.mark_source_done(source_key)
            state.save()

    def _send_batch(self, meta: dict[str, Any], files: list[ExpandedFile], state: StateStore, stats: SyncStats) -> None:
        payload = dict(meta)
        payload["attachments"] = [attachment_payload(file) for file in files]
        status, body = self.uploader.post_json(self.config.ingest_batches_url, payload)
        receipts = body.get("receipts") if isinstance(body, dict) else None
        if status not in (200, 201) or not isinstance(receipts, list) or len(receipts) != len(files):
            raise MailSyncError("DMR batch response did not contain one receipt per file")
        for file, receipt in zip(files, receipts):
            state.mark_file_done(file.file_key, receipt if isinstance(receipt, dict) else {})
            stats.uploaded_files += 1
            self.log(f"UPLOADED {file.filename}: {redact_receipt(receipt)}")

    def _send_transfer_manifest(self, meta: dict[str, Any], file: ExpandedFile, state: StateStore, stats: SyncStats) -> None:
        payload = dict(meta)
        payload["attachment"] = attachment_payload(file)
        url = self.config.ingest_transfer_manifest_url
        if self.config.intake_only:
            url += "?stage_only=true"
        status, body = self.uploader.post_json(url, payload)
        expected = {"staged", "archived"} if self.config.intake_only else {"archived"}
        if status not in (200, 201, 202) or not isinstance(body, dict) or body.get("archive_status") not in expected:
            raise MailSyncError("转移清单未成功暂存" if self.config.intake_only else "192归档尚未成功，保留待重试")
        state.mark_file_done(file.file_key, body)
        state.save()
        stats.uploaded_files += 1
        self.log(f"{'STAGED' if self.config.intake_only else 'ARCHIVED'} {file.filename}: {redact_receipt(body)}")

    def _send_legacy_files(self, meta: dict[str, Any], files: list[ExpandedFile], state: StateStore, stats: SyncStats) -> None:
        for file in files:
            payload = dict(meta)
            payload["attachment"] = attachment_payload(file)
            status, body = self.uploader.post_json(self.config.ingest_attachments_url, payload)
            if status not in (200, 201) or not isinstance(body, dict):
                stats.failed_files += 1
                continue
            state.mark_file_done(file.file_key, body)
            state.save()
            stats.uploaded_files += 1
            self.log(f"UPLOADED {file.filename}: {redact_receipt(body)}")


def sender_address(message: dict[str, Any], fallback: str) -> str:
    head_from = message.get("head_from")
    if isinstance(head_from, dict):
        return str(head_from.get("mail_address") or head_from.get("email") or fallback)
    return str(message.get("sender") or fallback)


def matched_keyword(subject: str, keywords: Iterable[str]) -> str:
    return next((keyword for keyword in keywords if keyword in subject), "")


def redact_id(value: Any) -> str:
    text = str(value)
    if len(text) <= 12:
        return text
    return text[:6] + "…" + text[-4:]


def redact_receipt(receipt: Any) -> str:
    if not isinstance(receipt, dict):
        return "receipt=<invalid>"
    receipt_id = redact_id(receipt.get("receipt_id", ""))
    batch_id = redact_id(receipt.get("batch_id", ""))
    return f"receipt={receipt_id} status={receipt.get('status', '')} batch={batch_id}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Optional JSON config file. Defaults to DMR_MAIL_CONFIG, then scripts/mail_sync_config.json when present.")
    parser.add_argument("--state-file", help="State file path. Defaults to scripts/processed.json.")
    parser.add_argument("--dmr-base", help="DMR base URL. Env: DMR_MAIL_BASE_URL.")
    parser.add_argument("--token", help="DMR mail bearer token. Prefer env DMR_MAIL_TOKEN.")
    parser.add_argument("--mailbox", help="Mailbox address. Env: DMR_MAILBOX.")
    parser.add_argument("--folder", help="Mail folder exact filter. Default SENT.")
    parser.add_argument("--keyword", action="append", help="Include keyword. Repeatable; env/json may use comma list.")
    parser.add_argument("--transfer-keyword", action="append", help="Transfer-manifest mail keyword; disabled until configured. Env: DMR_MAIL_TRANSFER_KEYWORDS.")
    parser.add_argument("--exclude-keyword", action="append", help="Exclude keyword. Repeatable; env/json may use comma list.")
    parser.add_argument("--delivery-mode", choices=["batch", "legacy"], help="Default batch. legacy uses /attachments.")
    parser.add_argument("--batch-grouping", choices=["message", "attachment"], help="Default message.")
    parser.add_argument("--intake-only", action="store_true", help="Stage transfer manifests on the platform without writing to 192.")
    parser.add_argument("--dry-run", action="store_true", help="Discover, download, expand and validate groups without uploading or saving state.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = MailSyncConfig.from_sources(args)
        started = time.time()
        stats = MailSync(config).run()
        duration = time.time() - started
        print(
            "Summary: "
            f"messages={stats.considered_messages} planned={stats.planned_files} "
            f"uploaded={stats.uploaded_files} dry_run={stats.dry_run_files} "
            f"failed_files={stats.failed_files} failed_groups={stats.failed_groups} "
            f"skipped_done={stats.skipped_done_files} seconds={duration:.1f}",
            flush=True,
        )
        return 1 if stats.failed() else 0
    except BlockingIOError:
        print("Another mail_sync run is already active; exiting without changes.", file=sys.stderr)
        return 2
    except MailSyncError as exc:
        print(f"mail_sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
