"""Archive mailed DMR transfer manifests by the date in their original filename.

These forms are evidence of a transfer, not controlled DMR module files.  They
therefore have their own receipt table and never enter BOM/version matching.
"""
from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import io
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException

from backend.service import now_iso


ARCHIVE_ROOT_ENV = "DMR_TRANSFER_ARCHIVE_MOUNT_ROOT"
ARCHIVE_ROOT_FILE = Path(__file__).resolve().parents[1] / ".runtime" / "transfer-archive-root.txt"
NETWORK_ROOT = r"\\nas.example.local\文档中心\DMR转移清单"
FILENAME = re.compile(r"^(?:ECN-\d+[:：]\s*)?DMR文件转移清单\s*[（(][A-Za-z][A-Za-z0-9]*-*(20\d{6})(?:\d{2})?(?:-?[^/\\（）()]*)?[）)][^/\\]*\.xlsx$", re.I)


def looks_like_transfer_manifest(filename: str) -> bool:
    """Route transfer forms before ordinary controlled-file field extraction."""
    return bool(re.match(r"^(?:ECN-\d+[:：]\s*)?DMR文件转移清单", filename.strip(), re.I))


def destination_for(filename: str) -> tuple[str, str]:
    """Return ISO transfer date and path relative to the configured share root."""
    if re.match(r"^ECN-\d+[:：]", filename.strip(), re.I):
        raise HTTPException(422, "ECN 转移清单由现有人工流程处理，平台不自动归档")
    match = FILENAME.fullmatch(filename)
    if not match:
        raise HTTPException(422, "转移清单文件名不符合约定格式，无法自动归档")
    try:
        file_date = dt.datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError as exc:
        raise HTTPException(422, "转移清单文件名中的日期无效") from exc
    return file_date.isoformat(), f"Y-{file_date:%Y}/DMR-{file_date:%Y-%m}/{filename}"


def configured_archive_root() -> str:
    """Environment override or host-local path file; never a shared code default."""
    if ARCHIVE_ROOT_ENV in os.environ:
        return os.environ[ARCHIVE_ROOT_ENV].strip()
    try:
        return ARCHIVE_ROOT_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def network_path_for(relative_path: str) -> str:
    root = os.environ.get("DMR_TRANSFER_ARCHIVE_NETWORK_ROOT", NETWORK_ROOT).rstrip("\\/")
    return root + "\\" + relative_path.replace("/", "\\")


def validate_workbook(raw: bytes) -> None:
    """Check XLSX container structure without extracting any worksheet fields."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as book:
            names = set(book.namelist())
            if "xl/workbook.xml" not in names or not any(
                name.startswith("xl/worksheets/") and name.endswith(".xml") for name in names
            ):
                raise ValueError("缺少电子表格工作表")
    except (zipfile.BadZipFile, ValueError) as exc:
        raise HTTPException(422, f"附件不是可识别的DMR文件转移清单：{exc}") from exc


class TransferManifestArchive:
    def __init__(self, store):
        self.store = store

    def ingest(self, client, payload, fingerprint: str, *, stage_only: bool = False) -> tuple[int, dict]:
        filename = payload.attachment.filename
        file_date, relative = destination_for(filename)
        validate_workbook(payload.decoded)
        key = (payload.mailbox, payload.message_id, payload.attachment.attachment_id)
        with self.store.lock:
            with self.store.conn.immediate():
                row = self.store.execute(
                    "SELECT * FROM transfer_manifests WHERE mailbox=? AND message_id=? AND attachment_id=?", key
                ).fetchone()
                if row and (row["client_id"] != client["id"] or row["payload_fingerprint"] != fingerprint):
                    raise HTTPException(409, "同一邮件附件已有不同内容或属于其他客户端，拒绝覆盖")
                if not row:
                    _, blob_path, size = self.store.store_blob(payload.decoded, filename)
                    receipt_id = f"TRANSFER-{uuid.uuid4().hex}"
                    now = now_iso()
                    try:
                        self.store.execute(
                            """INSERT INTO transfer_manifests
                            (id,client_id,mailbox,message_id,attachment_id,payload_fingerprint,filename,file_date,
                             sha256,blob_path,size,target_path,status,last_error,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (receipt_id, client["id"], *key, fingerprint, filename, file_date,
                             payload.sha256, blob_path, size, relative, "pending", "", now, now),
                        )
                    except Exception:
                        self.store.cleanup_blobs([blob_path])
                        raise
                    row = self.store.execute("SELECT * FROM transfer_manifests WHERE id=?", (receipt_id,)).fetchone()
        if stage_only and row["status"] != "archived":
            if row["status"] == "pending":
                self._set_staged(row["id"])
            return 202, self._response_by_id(row["id"]) | {"staged_only": True}
        return self._archive(row)

    def ingest_web(self, user: dict, filename: str, raw: bytes) -> tuple[int, dict]:
        """Archive a browser upload using the mail flow's collision rules."""
        file_date, relative = destination_for(filename)
        validate_workbook(raw)
        sha = hashlib.sha256(raw).hexdigest()
        key = ("web-upload@local.dmr", "web-" + hashlib.sha256(
            (filename + "\0" + sha).encode("utf-8")).hexdigest(), "file")
        with self.store.lock:
            with self.store.conn.immediate():
                row = self.store.execute(
                    "SELECT * FROM transfer_manifests WHERE mailbox=? AND message_id=? AND attachment_id=?", key
                ).fetchone()
                if not row:
                    _, blob_path, size = self.store.store_blob(raw, filename)
                    receipt_id = f"TRANSFER-{uuid.uuid4().hex}"
                    now = now_iso()
                    try:
                        self.store.execute(
                            """INSERT INTO transfer_manifests
                            (id,client_id,mailbox,message_id,attachment_id,payload_fingerprint,filename,file_date,
                             sha256,blob_path,size,target_path,status,last_error,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (receipt_id, "web:" + user["id"], *key, sha, filename, file_date,
                             sha, blob_path, size, relative, "pending", "", now, now),
                        )
                    except Exception:
                        self.store.cleanup_blobs([blob_path])
                        raise
                    self.store.add_log("转移清单上传", filename, "网页提交；按文件名日期归档", receipt_id,
                                       user=f"{user['display_name']} [{user['id']}]")
                    row = self.store.execute("SELECT * FROM transfer_manifests WHERE id=?", (receipt_id,)).fetchone()
        return self._archive(row)

    def _archive(self, row) -> tuple[int, dict]:
        receipt_id = row["id"]
        lock_path = self.store.data_dir / f"transfer-{receipt_id}.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.store.lock, self.store.conn:
                row = self.store.execute("SELECT * FROM transfer_manifests WHERE id=?", (receipt_id,)).fetchone()
            if row["status"] == "archived":
                return 200, self._response(row) | {"already_exists": True}
            root_value = configured_archive_root()
            if not root_value:
                self._set_error(receipt_id, "未配置转移清单192归档根目录")
                raise HTTPException(503, "转移清单已保存在平台，192归档目录尚未配置；下次邮件任务会重试")
            root = Path(root_value).expanduser().resolve()
            if not root.is_dir():
                self._set_error(receipt_id, "192归档根目录不可访问")
                raise HTTPException(503, "转移清单已保存在平台，192归档目录不可访问；下次邮件任务会重试")
            target = root / row["target_path"]
            if not target.resolve().is_relative_to(root):
                raise HTTPException(409, "归档目标路径无效")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                raw = self.store.read_blob(row["blob_path"])
                if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                    raise OSError("平台保存的附件校验失败")
                if target.exists():
                    if target.is_file() and self._hash_file(target) == row["sha256"]:
                        self._set_archived(receipt_id)
                        return 200, self._response_by_id(receipt_id) | {"already_exists": True}
                    raise FileExistsError("目标目录已有同名但内容不同的文件，未覆盖")
                fd, temp_path = tempfile.mkstemp(prefix=".dmr-transfer-", suffix=".tmp", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(raw)
                        out.flush()
                        os.fsync(out.fileno())
                    try:
                        os.link(temp_path, target)
                    except OSError as exc:
                        if exc.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}:
                            raise
                        # SMB mounts may not implement hard links.  Exclusive
                        # creation still guarantees an existing file is never
                        # overwritten, and the final hash is verified below.
                        exclusive = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        try:
                            with os.fdopen(exclusive, "wb") as out, open(temp_path, "rb") as src:
                                shutil.copyfileobj(src, out)
                                out.flush()
                                os.fsync(out.fileno())
                        except Exception:
                            target.unlink(missing_ok=True)
                            raise
                    if self._hash_file(target) != row["sha256"]:
                        raise OSError("192归档文件校验失败")
                finally:
                    Path(temp_path).unlink(missing_ok=True)
                self._set_archived(receipt_id)
                return 201, self._response_by_id(receipt_id)
            except (OSError, ValueError) as exc:
                message = str(exc)[:240]
                if isinstance(exc, FileExistsError):
                    self._set_conflict(receipt_id, message)
                    raise HTTPException(409, {
                        "error": "192目标目录已有同名但内容不同的文件，请人工核对；原文件未覆盖，新文件保存在平台。",
                        "receipt_id": receipt_id,
                        "archive_status": "needs_review",
                        "network_path": network_path_for(row["target_path"]),
                        "file_date": row["file_date"],
                        "existing_file_unchanged": True,
                    }) from exc
                self._set_error(receipt_id, message)
                raise HTTPException(503, f"转移清单已保存在平台，192归档未完成：{message}") from exc

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _set_error(self, receipt_id: str, message: str) -> None:
        with self.store.lock, self.store.conn.immediate():
            self.store.execute("UPDATE transfer_manifests SET status='pending',last_error=?,updated_at=? WHERE id=?",
                               (message, now_iso(), receipt_id))

    def _set_staged(self, receipt_id: str) -> None:
        with self.store.lock, self.store.conn.immediate():
            self.store.execute("UPDATE transfer_manifests SET status='staged',last_error='',updated_at=? WHERE id=? AND status='pending'",
                               (now_iso(), receipt_id))
            row = self.store.execute("SELECT filename FROM transfer_manifests WHERE id=?", (receipt_id,)).fetchone()
            self.store.add_log("转移清单暂存", row["filename"], "原件已保存到平台；尚未写入192", receipt_id)

    def _set_conflict(self, receipt_id: str, message: str) -> None:
        with self.store.lock, self.store.conn.immediate():
            self.store.execute("UPDATE transfer_manifests SET status='needs_review',last_error=?,updated_at=? WHERE id=?",
                               (message, now_iso(), receipt_id))
            row = self.store.execute("SELECT filename,target_path FROM transfer_manifests WHERE id=?", (receipt_id,)).fetchone()
            self.store.add_log("转移清单待人工核对", row["filename"],
                               f"同名文件内容不同，未覆盖：{row['target_path']}", receipt_id, "需要处理")

    def _set_archived(self, receipt_id: str) -> None:
        with self.store.lock, self.store.conn.immediate():
            self.store.execute("UPDATE transfer_manifests SET status='archived',last_error='',updated_at=? WHERE id=?",
                               (now_iso(), receipt_id))
            row = self.store.execute("SELECT filename,target_path FROM transfer_manifests WHERE id=?", (receipt_id,)).fetchone()
            self.store.add_log("转移清单归档", row["filename"], row["target_path"], receipt_id)

    def _response_by_id(self, receipt_id: str) -> dict:
        with self.store.lock, self.store.conn:
            row = self.store.execute("SELECT * FROM transfer_manifests WHERE id=?", (receipt_id,)).fetchone()
        return self._response(row)

    @staticmethod
    def _response(row) -> dict:
        return {"receipt_id": row["id"], "status": row["status"], "archive_status": row["status"],
                "file_date": row["file_date"], "target_path": row["target_path"],
                "network_path": network_path_for(row["target_path"]), "sha256": row["sha256"]}
