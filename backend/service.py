#!/usr/bin/env python3
"""DMR business service backed by SQLAlchemy and local file storage."""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import html
import importlib
import json
import posixpath
import re
import shutil
import sys
import threading
from contextlib import contextmanager
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8920
MAX_BODY = 80 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 200
MAX_PREVIEW_ROWS = 50
MAX_ZIP_MEMBERS = 1200
MAX_XML_BYTES = 12 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 48 * 1024 * 1024
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
STATIC_ALLOWLIST = {
    "/": "DMR文件管理-可分享Demo.html",
    "/DMR文件管理-可分享Demo.html": "DMR文件管理-可分享Demo.html",
    "/local-app.js": "local-app.js",
}


def now_iso() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def today_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).replace("\x00", "").strip()
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text or default


def clean_filename(name: str) -> str:
    base = posixpath.basename(str(name).replace("\\", "/"))
    base = clean_text(base, "unnamed.bin")
    return base[:240] or "unnamed.bin"


def safe_json_loads(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"JSON解析失败: {exc}") from exc


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def size_label(num: int) -> str:
    if num >= 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB"
    if num >= 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num} B"


def file_type(name: str) -> str:
    ext = Path(name).suffix.lower().strip(".")
    return {"xlsx": "XLS", "xls": "XLS", "zip": "ZIP", "docx": "DOC", "doc": "DOC", "pdf": "PDF", "hex": "HEX"}.get(ext, ext.upper() or "FILE")


def module_from_filename(name: str) -> str:
    upper = name.upper()
    ext = Path(name).suffix.lower()
    if "BOM" in upper or ext in {".xlsx", ".xls"} and re.match(r"^\d", Path(name).name):
        return "BOM"
    if ext == ".hex" or "SOFT" in upper or "软件" in name:
        return "软件"
    if "检验" in name or "INS" in upper:
        return "检验文件"
    if "工艺" in name or "WI" in upper:
        return "工艺文件"
    if "规格" in name or "承认" in name or "SPEC" in upper:
        return "承认书&规格书"
    if "包装" in name or "PKG" in upper:
        return "包装图纸"
    if "结构" in name or "STR" in upper:
        return "结构图纸"
    return "待确认"


def fallback_parse_file(data: bytes, filename: str) -> dict:
    stem = Path(filename).stem
    version = extract_version(filename)
    code = ""
    if "_V" in stem.upper():
        code = re.split(r"[_-]V\d", stem, maxsplit=1, flags=re.IGNORECASE)[0]
    if not code:
        m = re.match(r"([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+){0,4})", stem)
        code = m.group(1) if m else stem[:40]
    registrant = "待确认"
    m = re.search(r"【([^】]+)】", filename)
    if m:
        registrant = m.group(1)
    product = "待确认"
    m = re.search(r"_V[^-]*-([^-_（(【\\[]+)", filename, re.IGNORECASE)
    if m:
        product = clean_text(m.group(1), "待确认")
    else:
        p = re.match(r"([A-Za-z]{1,4}\d{1,3}[A-Za-z]?)", stem)
        if p:
            product = p.group(1)
    module = module_from_filename(filename)
    notes = []
    if module == "待确认":
        notes.append("文件模块无法从文件名可靠识别，需人工确认。")
    if product == "待确认":
        notes.append("产品项目无法从文件名可靠识别，需人工确认。")
    metadata = {"parser": "fallback", "size": len(data)}
    if any(token in filename for token in ("医疗", "治疗仪")):
        metadata["project_category_suggestion"] = "文件名含医疗/治疗仪字样，仅作为人工核对线索"
    return {
        "code": clean_text(code, stem[:40]),
        "registrant": registrant,
        "product": product,
        "module": module,
        "type": file_type(filename),
        "project_category": "待确认",
        "project_category_source": "未提供",
        "version": version,
        "linked_bom": "",
        "notes": "；".join(notes),
        "metadata": metadata,
    }


def load_parsers():
    try:
        sys.path.insert(0, str(ROOT))
        return importlib.import_module("parsers")
    except Exception:
        return None


def parse_file(data: bytes, filename: str) -> dict:
    parser = load_parsers()
    if parser and hasattr(parser, "parse_file"):
        try:
            parsed = parser.parse_file(data, filename)
            if isinstance(parsed, dict):
                parsed.setdefault("code", "")
                parsed.setdefault("registrant", "")
                parsed.setdefault("product", "")
                parsed.setdefault("module", "")
                parsed.setdefault("type", "")
                parsed.setdefault("project_category", "待确认")
                parsed.setdefault("project_category_source", "未提供")
                parsed.setdefault("version", "")
                parsed.setdefault("linked_bom", "")
                parsed.setdefault("notes", [])
                if not isinstance(parsed.get("metadata"), dict):
                    parsed["metadata"] = {"raw_metadata": parsed.get("metadata")}
                return parsed
        except Exception as exc:  # noqa: BLE001
            parsed = fallback_parse_file(data, filename)
            parsed["notes"] = clean_text(f"{parsed.get('notes','')}；解析器异常，已使用保守识别: {exc}", "解析器异常，已使用保守识别。")
            return parsed
    return fallback_parse_file(data, filename)


def enrich_items(items: list[dict]) -> list[dict]:
    parser = load_parsers()
    if parser and hasattr(parser, "enrich_batch"):
        try:
            enriched = parser.enrich_batch(items)
            if isinstance(enriched, list):
                return enriched
        except Exception:
            return items
    return items


def version_key(version: str):
    text = clean_text(version).upper()
    parts = []
    for chunk in re.findall(r"[A-Z]+|\d+", text):
        if chunk.isdigit():
            parts.append((1, int(chunk)))
        else:
            value = 0
            for char in chunk:
                value = value * 26 + (ord(char) - 64 if "A" <= char <= "Z" else 0)
            parts.append((0, value))
    return parts or [(0, 0)]


def compare_versions(left: str, right: str) -> int:
    parser = load_parsers()
    if parser and hasattr(parser, "compare_versions"):
        try:
            result = parser.compare_versions(left, right)
            if result is None:
                return compare_same_prefix_versions(left, right)
            if result < 0:
                return -1
            if result > 0:
                return 1
            return 0
        except Exception:
            return compare_same_prefix_versions(left, right)
    a = version_key(left)
    b = version_key(right)
    max_len = max(len(a), len(b))
    a += [(1, 0)] * (max_len - len(a))
    b += [(1, 0)] * (max_len - len(b))
    return (a > b) - (a < b)


def compare_same_prefix_versions(left: str, right: str):
    left = clean_text(left).upper()
    right = clean_text(right).upper()
    lm = re.fullmatch(r"([A-Z]+)(\d+(?:\.\d+)*)", left)
    rm = re.fullmatch(r"([A-Z]+)(\d+(?:\.\d+)*)", right)
    if not lm or not rm or lm.group(1) != rm.group(1):
        return None
    lnums = tuple(int(part) for part in lm.group(2).split("."))
    rnums = tuple(int(part) for part in rm.group(2).split("."))
    width = max(len(lnums), len(rnums))
    lnums += (0,) * (width - len(lnums))
    rnums += (0,) * (width - len(rnums))
    return (lnums > rnums) - (lnums < rnums)


def normalize_notes(value) -> str:
    if isinstance(value, list):
        return "；".join(clean_text(item) for item in value if clean_text(item))
    return clean_text(value)


PROJECT_CATEGORIES = {"医疗", "家电", "待确认"}


def normalize_project_category(value, default: str = "待确认") -> str:
    text = clean_text(value, default)
    if text in PROJECT_CATEGORIES:
        return text
    aliases = {"医疗项目": "医疗", "医疗器械": "医疗", "家电项目": "家电", "小家电": "家电"}
    if text in aliases:
        return aliases[text]
    raise ValueError("project_category只能是医疗、家电或待确认")


def confirmed_project_category(value) -> str:
    text = clean_text(value, "待确认")
    return text if text in {"医疗", "家电"} else ""


def safe_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(BytesIO(data))
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError("zip文件条目过多")
    total_xml = 0
    for info in infos:
        name = info.filename
        norm = posixpath.normpath(name)
        if norm.startswith("../") or norm.startswith("/") or "\x00" in name:
            raise ValueError("zip内含不安全路径")
        if name.endswith(".xml"):
            total_xml += info.file_size
            if info.file_size > MAX_XML_BYTES:
                raise ValueError("xml文件过大")
    if total_xml > MAX_TOTAL_XML_BYTES:
        raise ValueError("xml总体积过大")
    return zf


def read_zip_xml(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError("xml文件过大")
    data = zf.read(name)
    head = data[:512].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ValueError("xml包含不允许的DOCTYPE/ENTITY")
    return data


def extract_version(filename: str) -> str:
    matches = re.findall(r"(?:^|[_\-\s])([A-Z]?\d+(?:\.\d+){1,}|[A-Z]\d{2,}|V\d+(?:\.\d+)*)", filename, flags=re.IGNORECASE)
    if matches:
        value = matches[-1].upper()
        return value if value.startswith(("V", "A", "B", "C", "D", "E", "F")) else f"V{value}" if "." in value else value
    return "待确认"


def incomplete_fingerprint(item: dict, sha: str) -> str:
    return sha256_bytes(json_dumps({k: clean_text(item.get(k)) for k in ("code", "registrant", "product", "module", "version", "project_category")} | {"sha256": sha}).encode())


def record_identity(item: dict) -> tuple[str, str, str, str]:
    return (
        clean_text(item.get("registrant"), "待确认"),
        clean_text(item.get("product"), "待确认"),
        clean_text(item.get("module"), "待确认"),
        clean_text(item.get("code"), clean_text(item.get("no"), "")),
    )


def dated_bom_metadata(item: dict) -> dict:
    value = item.get("metadata", {})
    metadata = value if isinstance(value, dict) else json_loads(value, {})
    return metadata if metadata.get("version_rule") == "filename_date" else {}


def dated_bom_policy_error(item: dict) -> str:
    metadata = dated_bom_metadata(item)
    if not metadata:
        return ""
    date = str(metadata.get("version_date", ""))
    source_code = metadata.get("source_bom_code") or metadata.get("root_row", {}).get("父项编码")
    if not source_code or item.get("code") != source_code or item.get("module") != "BOM":
        return "日期版本资料的文件编号或模块与原件不一致，不能转为其他文件的新版本。"
    if not re.fullmatch(r"20\d{6}", date) or compare_versions(date, date) is None:
        return "文件名日期缺失或无效，需核对原件。"
    if clean_text(item.get("version")) != date:
        return f"此文件按文件名日期 {date} 管理版本，不能用人工输入的其他版本覆盖。"
    return ""


def highest_version_rows(rows: list[object]) -> list[object]:
    top: list[object] = []
    for row in rows:
        if not top:
            top = [row]
            continue
        cmp = compare_versions(row["version"], top[0]["version"])
        if cmp is None:
            continue
        if cmp > 0:
            top = [row]
        elif cmp == 0:
            top.append(row)
    return top


class DMRStore:
    def __init__(self, data_dir: Path, database_url: str | None = None):
        self.data_dir = data_dir
        self.db_path = data_dir / "dmr.sqlite3"
        self.files_dir = data_dir / "files"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._actor = threading.local()
        from backend.db import Database, create_dmr_engine

        self.engine = create_dmr_engine(self.data_dir, database_url)
        self.conn = Database(self.engine)
        self.init_db()

    def init_db(self) -> None:
        from backend.db import init_schema

        init_schema(self.engine)

    def close(self):
        self.conn.close()

    def execute(self, sql: str, params=()):
        return self.conn.execute(sql, params)

    @contextmanager
    def acting_as(self, user: str):
        previous = getattr(self._actor, "user", "本地用户")
        self._actor.user = user or "本地用户"
        try:
            yield
        finally:
            self._actor.user = previous

    def add_log(self, action: str, obj: str, change: str, source: str, result: str = "成功", user: str | None = None) -> None:
        actor = user or getattr(self._actor, "user", "本地用户")
        self.conn.execute(
            "INSERT INTO logs(id,time,user,action,object,change,source,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"LOG-{uuid.uuid4().hex}", today_text(), actor, action, obj, change, source, result, now_iso()),
        )

    def add_notification(self, title: str, detail: str, product: str = "all") -> None:
        self.conn.execute(
            "INSERT INTO notifications(id,time,title,detail,audience,product,read,created_at) VALUES(?,?,?,?,?,?,0,?)",
            (f"NOTICE-{uuid.uuid4().hex[:10]}", today_text(), title, detail, "工厂、体系、项目经理、工厂品质、DQA、SQE、NPI", product, now_iso()),
        )

    def store_blob(self, data: bytes, filename: str) -> tuple[str, str, int]:
        from backend.storage import store_blob

        return store_blob(self.files_dir, self.data_dir, data, filename)

    def read_blob(self, rel_path: str) -> bytes:
        from backend.storage import read_blob

        return read_blob(self.data_dir, rel_path)

    def cleanup_blobs(self, rel_paths: list[str]) -> None:
        for rel_path in rel_paths:
            path = (self.data_dir / rel_path).resolve()
            try:
                path.relative_to(self.data_dir)
            except ValueError:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def classification_edit_token(self, row) -> str:
        from backend.editing import edit_token
        records = [dict(r) for r in self.execute(
            "SELECT * FROM records WHERE registrant=? AND product=? ORDER BY id",
            (row["registrant"], row["product"])).fetchall()]
        pending = [dict(r) for r in self.execute(
            "SELECT * FROM batch_items WHERE registrant=? AND product=? AND status!='committed' ORDER BY id",
            (row["registrant"], row["product"])).fetchall()]
        return edit_token("classification", row["id"], [records, pending])

    def item_edit_token(self, row) -> str:
        from backend.editing import edit_token
        records = [dict(r) for r in self.execute(
            "SELECT * FROM records WHERE registrant=? AND product=? ORDER BY id",
            (row["registrant"], row["product"])).fetchall()]
        batch = self.execute("SELECT status FROM batches WHERE id=?", (row["batch_id"],)).fetchone()
        return edit_token("review", row["id"], [dict(row), records, batch["status"] if batch else None])

    def row_record(self, row: object) -> dict:
        return {
            "edit_token": self.classification_edit_token(row),
            "id": row["id"],
            "no": row["business_code"],
            "name": row["name"],
            "registrant": row["registrant"],
            "type": row["type"],
            "project_category": row["project_category"] or "待确认",
            "project_category_source": row["project_category_source"] or "",
            "product": row["product"],
            "module": row["module"],
            "version": row["version"],
            "previous": row["previous"] or "",
            "file": row["file_name"] or "",
            "date": row["date"] or "",
            "controlled": row["controlled"] or "",
            "source": row["source"] or "",
            "owner": row["owner"] or "本地用户",
            "size": row["size"] or "",
            "sourceType": row["source_type"] or "本地上传",
            "linked_bom": row["linked_bom"] or "",
            "metadata": json_loads(row["metadata"], {}),
        }

    def row_file(self, row: object) -> dict:
        record = self.execute("SELECT business_code,registrant,product,project_category FROM records WHERE id=?", (row["record_id"],)).fetchone()
        return {
            "id": row["id"],
            "recordId": row["record_id"],
            "recordNo": record["business_code"] if record else "",
            "name": row["name"],
            "version": row["version"],
            "status": row["status"],
            "date": row["date"] or "",
            "source": row["source"] or "",
            "size": row["size"] or "",
            "type": row["type"] or file_type(row["name"]),
            "storage": row["storage"] or "本地保存",
            "sha256": row["sha256"],
            "metadata": json_loads(row["metadata"], {}),
            "linked_bom": row["linked_bom"] or "",
            "link_status": self.link_status(row["linked_bom"], record["registrant"], record["product"], record["project_category"]) if record else (row["link_status"] or ""),
        }

    def row_exception(self, row: object) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "detail": row["detail"],
            "type": row["type"],
            "recordId": row["record_id"] or "",
            "itemId": row["item_id"] or "",
            "batchId": row["batch_id"] or "",
            "recordNo": row["record_no"] or "",
            "recommendation": row["recommendation"] or "",
            "action": row["action"] or "补充资料后重新校验",
            "status": row["status"],
        }

    def _append_product_source(self, recommendation: str, metadata: dict) -> str:
        source = metadata.get("product_source") if isinstance(metadata, dict) else ""
        if source and "解析型号来源：" not in recommendation:
            recommendation += " 解析型号来源：" + str(source) + "。"
        return recommendation

    def _allows_same_version_revision(self, item: dict) -> bool:
        return clean_text(item.get("module")) in {"工艺文件", "检验文件"}

    def _same_version_revision_confirmed(self, metadata: dict, item: dict, row: object, record: object, current_file: object) -> bool:
        revision = metadata.get("same_version_revision") if isinstance(metadata, dict) else None
        if not isinstance(revision, dict) or not self._allows_same_version_revision(item):
            return False
        return (
            clean_text(revision.get("record_id")) == record["id"]
            and clean_text(revision.get("base_version")) == record["version"]
            and clean_text(revision.get("base_file_id")) == current_file["id"]
            and clean_text(revision.get("base_sha256")) == current_file["sha256"]
            and clean_text(revision.get("new_sha256")) == row["sha256"]
            and bool(clean_text(revision.get("note")))
        )

    def row_item(self, row: object) -> dict:
        recommendation = row["recommendation"] or ""
        if recommendation.startswith("缺少必要字段："):
            for key, label in {"registrant":"公司主体", "product":"产品项目", "version":"版本", "module":"资料类别", "code":"文件编号／标识"}.items():
                recommendation = recommendation.replace(key, label)
            recommendation = recommendation.replace("缺少必要字段：", "尚未确认：")
        product_meta = json_loads(row["metadata"], {})
        recommendation = self._append_product_source(recommendation, product_meta)
        return {
            "edit_token": self.item_edit_token(row),
            "id": row["id"],
            "batch_id": row["batch_id"],
            "fileName": row["file_name"],
            "recordId": row["record_id"] or "",
            "contextRecordId": row["context_record_id"] or "",
            "code": row["code"] or "",
            "registrant": row["registrant"] or "",
            "type": row["type"] or "",
            "project_category": row["project_category"] or "待确认",
            "project_category_source": row["project_category_source"] or "",
            "product": row["product"] or "",
            "module": row["module"] or "",
            "version": row["version"] or "",
            "linked_bom": row["linked_bom"] or "",
            "notes": row["notes"] or "",
            "metadata": json_loads(row["metadata"], {}),
            "current": row["current_version"] or "—",
            "detected": row["detected_version"] or row["version"] or "待确认",
            "status": row["status"],
            "recommendation": recommendation,
            "sha256": row["sha256"],
            "size": row["size"],
            "result": row["result_message"] or "",
        }

    def state(self) -> dict:
        records = [self.row_record(r) for r in self.execute("SELECT * FROM records ORDER BY updated_at DESC, created_at DESC").fetchall()]
        files = [self.row_file(r) for r in self.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()]
        exceptions = [self.row_exception(r) for r in self.execute("SELECT * FROM exceptions ORDER BY created_at DESC").fetchall()]
        logs = [dict(r) for r in self.execute("SELECT time,user,action,object,change,source,result FROM logs ORDER BY created_at DESC,id DESC LIMIT 200").fetchall()]
        notifications = [dict(r) | {"read": bool(r["read"])} for r in self.execute("SELECT id,time,title,detail,audience,product,read FROM notifications ORDER BY created_at DESC LIMIT 200").fetchall()]
        # Retain unresolved work regardless of recency; the recent window is not an authorization boundary.
        batches = [dict(r) | {"summary": json_loads(r["summary"], {})} for r in self.execute("""
            SELECT * FROM batches WHERE id IN (
                SELECT id FROM batches ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT 100
            ) OR EXISTS (
                SELECT 1 FROM batch_items WHERE batch_id=batches.id
                AND status IN ('matched','unmatched','conflict','rejected')
            ) ORDER BY updated_at DESC, created_at DESC, id DESC
        """).fetchall()]
        latest = self.execute("SELECT value FROM settings WHERE key='latest_sync'").fetchone()
        latest_sync = json_loads(latest["value"], {}) if latest else {}
        return {
            "schemaVersion": 5,
            "records": records,
            "libraryFiles": files,
            "exceptions": exceptions,
            "operationLogs": logs,
            "notifications": notifications,
            "batches": batches,
            "sourceHealth": [
                {"name": "本地SQLite数据库", "detail": f"{len(records)}条记录 · {len(files)}份文件", "time": "本地"},
                {"name": "本地附件仓库", "detail": str(self.files_dir.relative_to(ROOT)), "time": "本地"},
                {"name": "外部系统", "detail": "飞书、邮箱、PLM未接入，仅执行本地检查", "time": "未接入"},
            ],
            "latestSyncChanges": latest_sync.get("changes", []),
            "latestSyncTime": latest_sync.get("time", ""),
            "latestSyncBatch": latest_sync.get("batch", ""),
            "latestSyncTrigger": latest_sync.get("trigger", "本地检查"),
            "latestConfirmed": bool(latest_sync.get("confirmed", True)),
            "latestConfirmedBy": latest_sync.get("confirmedBy", ""),
            "fileSequence": len(files) + 1,
            "todayUpdateCount": self.execute("SELECT COUNT(*) AS n FROM logs WHERE action IN (?,?) AND substr(time,1,10)=?", ("批量录入", "同步数据", now_iso()[:10])).fetchone()["n"],
        }

    def find_records_for_item(self, item: dict) -> list[object]:
        code = clean_text(item.get("code"))
        registrant, product, module, _ = record_identity(item)
        if not all([code, registrant, product, module]) or "待确认" in {registrant, product, module}:
            return []
        return self.execute(
            "SELECT * FROM records WHERE business_code=? AND registrant=? AND product=? AND module=?",
            (code, registrant, product, module),
        ).fetchall()

    def inherit_project_category(self, item: dict) -> dict:
        from backend.classification import apply_classification
        item = apply_classification(item, item.get("fileName") or item.get("file_name") or "")
        if "人工" in item.get("project_category_source", "") or item.get("metadata", {}).get("project_classification", {}).get("conflict"):
            return item
        category = normalize_project_category(item.get("project_category", "待确认"))
        item["project_category"] = category
        item["project_category_source"] = clean_text(item.get("project_category_source"), "未提供")
        if category != "待确认":
            return item
        registrant = clean_text(item.get("registrant"))
        product = clean_text(item.get("product"))
        if not registrant or not product or "待确认" in {registrant, product}:
            return item
        rows = self.execute(
            "SELECT DISTINCT project_category FROM records WHERE registrant=? AND product=? AND project_category IN ('医疗','家电')",
            (registrant, product),
        ).fetchall()
        values = {row["project_category"] for row in rows}
        if len(values) == 1:
            item["project_category"] = next(iter(values))
            item["project_category_source"] = "继承同主体同产品已确认类别"
        elif len(values) > 1:
            notes = normalize_notes(item.get("notes"))
            suffix = "同主体同产品已有医疗/家电两类记录，需人工确认项目类别。"
            item["notes"] = f"{notes}；{suffix}" if notes else suffix
        return item

    def category_conflict(self, item: dict, record: object) -> str:
        item_category = confirmed_project_category(item.get("project_category"))
        record_category = confirmed_project_category(record["project_category"])
        if item_category and record_category and item_category != record_category:
            return f"项目类别冲突：上传资料为{item_category}，现有记录为{record_category}，禁止自动归档。"
        return ""

    def same_project_category_conflict(self, item: dict) -> str:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = json_loads(metadata, {})
        if metadata.get("project_classification", {}).get("conflict"):
            return "医疗/家电分类依据冲突，需文控人工核对后再归档。"
        item_category = confirmed_project_category(item.get("project_category"))
        if not item_category:
            return ""
        registrant = clean_text(item.get("registrant"))
        product = clean_text(item.get("product"))
        if not registrant or not product or "待确认" in {registrant, product}:
            return ""
        rows = self.execute(
            "SELECT DISTINCT project_category FROM records WHERE registrant=? AND product=? AND project_category IN ('医疗','家电')",
            (registrant, product),
        ).fetchall()
        conflicts = sorted({row["project_category"] for row in rows if row["project_category"] != item_category})
        if conflicts:
            return f"同主体同产品项目类别冲突：现有记录为{'/'.join(conflicts)}，上传资料为{item_category}，禁止自动归档。"
        return ""

    def bom_category_conflict(self, item: dict) -> str:
        linked_bom = clean_text(item.get("linked_bom"))
        item_category = confirmed_project_category(item.get("project_category"))
        if not linked_bom or not item_category:
            return ""
        rows = self.execute(
            "SELECT project_category FROM records WHERE business_code=? AND module='BOM' AND registrant=? AND product=?",
            (linked_bom, clean_text(item.get("registrant")), clean_text(item.get("product"))),
        ).fetchall()
        confirmed = {row["project_category"] for row in rows if confirmed_project_category(row["project_category"])}
        if len(confirmed) == 1:
            bom_category = next(iter(confirmed))
            if bom_category != item_category:
                return f"BOM项目类别冲突：资料为{item_category}，关联BOM为{bom_category}，禁止自动关联。"
        elif len(confirmed) > 1:
            return "关联BOM存在多个已确认项目类别，需人工确认。"
        return ""

    def classify_item(self, item: dict, sha: str) -> tuple[str, str, str, str]:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("review_pending"):
            return "unmatched", "", "待补充：" + (metadata.get("review_note") or "尚未确认资料信息"), "—"
        if metadata.get("parse_error"):
            return "unmatched", "", f"解析失败：{metadata.get('parse_error')}，需人工提供真实字段后再提交。", "—"
        policy_error = dated_bom_policy_error(item)
        if policy_error:
            return "conflict", "", policy_error, "—"
        approval = metadata.get("incomplete_archive")
        if approval and approval.get("fingerprint") == incomplete_fingerprint(item, sha):
            conflict = self.bom_category_conflict(item) or self.same_project_category_conflict(item)
            if conflict:
                return "conflict", "", conflict, "—"
            if clean_text(item.get("version")) not in ("", "待确认") and compare_versions(clean_text(item.get("version")), clean_text(item.get("version"))) is None:
                return "conflict", "", "版本格式无法识别，请核对或保留为空。", "—"
            return "matched", "", "文控已允许信息不全归档；缺失项保留未提供，作为独立记录保存，不覆盖已有版本。", "—"
        required = ["code", "registrant", "product", "module", "version"]
        missing = [key for key in required if clean_text(item.get(key), "待确认") in {"", "待确认"}]
        if missing:
            detail = "尚未确认：" + "、".join({"code":"文件编号／标识","registrant":"公司主体","product":"产品项目","module":"资料类别","version":"版本"}[key] for key in missing) + "，可补充信息或保存待补充。"
            if "product" in missing and metadata.get("product_candidates"):
                detail += " 型号候选：" + "；".join(str(c.get("source", "")) + "：" + str(c.get("product", "")) for c in metadata["product_candidates"] if isinstance(c, dict)) + "，请人工确认。"
            if "version" in missing:
                candidates = metadata.get("version_candidates") or []
                versions = list(dict.fromkeys(str(candidate.get("version", "")) for candidate in candidates if isinstance(candidate, dict) and candidate.get("version")))
                if versions:
                    detail += " 原件已读取到版本：" + "、".join(versions[:8]) + "；请核对各版本含义后选择，系统未自动取最大值。"
            return "unmatched", "", detail, "—"
        if compare_versions(clean_text(item.get("version")), clean_text(item.get("version"))) is None:
            return "conflict", "", f"版本 {clean_text(item.get('version')) or '空'} 无法严格识别，需人工确认。", "—"
        bom_conflict = self.bom_category_conflict(item)
        if bom_conflict:
            return "conflict", "", bom_conflict, "—"
        project_conflict = self.same_project_category_conflict(item)
        if project_conflict:
            return "conflict", "", project_conflict, "—"
        rows = self.find_records_for_item(item)
        if len(rows) > 1:
            return "conflict", "", "找到多条可能记录，需人工选择。", "—"
        if not rows:
            return "matched", "", "未找到现有记录，将作为新受控文件归档。", "—"
        record = rows[0]
        conflict = self.category_conflict(item, record)
        if conflict:
            return "conflict", record["id"], conflict, record["version"]
        current_file = self.execute("SELECT * FROM files WHERE record_id=? AND status='当前有效' ORDER BY created_at DESC LIMIT 1", (record["id"],)).fetchone()
        current_version = record["version"]
        if dated_bom_metadata(item):
            archived = self.execute("SELECT sha256 FROM files WHERE record_id=? AND version=?", (record["id"], item["version"])).fetchall()
            if archived:
                if all(file["sha256"] == sha for file in archived):
                    return "duplicate", record["id"], "此日期版本的相同原件已保存，提交时跳过。", current_version
                return "conflict", record["id"], "同一文件编号、同一日期存在不同内容，需人工核对。", current_version
        cmp = compare_versions(clean_text(item.get("version")), current_version)
        if cmp is None:
            return "conflict", record["id"], f"版本 {clean_text(item.get('version')) or '空'} 无法与当前版本 {current_version} 严格比较，需人工确认。", current_version
        if cmp < 0:
            if dated_bom_metadata(item):
                return "matched", record["id"], f"日期早于当前 {current_version}，仅补充为历史版本，不覆盖当前文件。", current_version
            return "conflict", record["id"], f"低于当前版本 {current_version}，禁止覆盖。", current_version
        if cmp == 0:
            if current_file and current_file["sha256"] == sha:
                return "duplicate", record["id"], "同版本同文件，提交时将跳过不重复归档。", current_version
            return "conflict", record["id"], "同版本但文件内容不同，需人工判断，禁止直接覆盖。", current_version
        return "matched", record["id"], "版本高于当前有效版本，可提交归档。", current_version

    def analyze(self, files: list[dict], validate_item=None) -> dict:
        if len(files) > MAX_FILES:
            raise ValueError(f"单批最多{MAX_FILES}个文件")
        batch_id = f"UPLOAD-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        items_for_enrich = []
        stored = []
        staged_paths: list[str] = []
        try:
            with self.lock, self.conn:
                self.conn.execute("INSERT INTO batches(id,status,created_at,updated_at,summary) VALUES(?,?,?,?,?)", (batch_id, "analyzed", now_iso(), now_iso(), "{}"))
                for entry in files:
                    name = clean_filename(entry.get("name", "unnamed.bin"))
                    raw_data = entry.get("data", "")
                    if isinstance(raw_data, str) and "," in raw_data and raw_data.split(",", 1)[0].startswith("data:"):
                        raw_data = raw_data.split(",", 1)[1]
                    try:
                        data = base64.b64decode(raw_data, validate=True)
                    except (binascii.Error, TypeError) as exc:
                        raise ValueError(f"{name} 不是合法base64文件数据") from exc
                    if len(data) > MAX_FILE_BYTES:
                        raise ValueError(f"{name} 超过单文件大小限制")
                    sha = sha256_bytes(data)
                    blob_id, rel_path, size = self.store_blob(data, name)
                    staged_paths.append(rel_path)
                    parsed = parse_file(data, name)
                    parsed["fileName"] = name
                    parsed["sha256"] = sha
                    parsed["size"] = size
                    parsed["_blob_path"] = rel_path
                    parsed["_staging_file_id"] = blob_id
                    items_for_enrich.append(parsed)
                enriched = enrich_items(items_for_enrich)
                for parsed in enriched:
                    item_id = str(uuid.uuid4())
                    parsed["code"] = clean_text(parsed.get("code"))
                    parsed["registrant"] = clean_text(parsed.get("registrant"))
                    parsed["product"] = clean_text(parsed.get("product"))
                    parsed["module"] = clean_text(parsed.get("module"))
                    parsed["type"] = clean_text(parsed.get("type"), "待确认")
                    parsed["project_category"] = normalize_project_category(parsed.get("project_category", "待确认"))
                    parsed["project_category_source"] = clean_text(parsed.get("project_category_source"), "未提供")
                    parsed["version"] = clean_text(parsed.get("version"))
                    parsed["linked_bom"] = clean_text(parsed.get("linked_bom"))
                    parsed["notes"] = normalize_notes(parsed.get("notes"))
                    parsed = self.inherit_project_category(parsed)
                    if validate_item is not None:
                        validate_item(parsed)
                    metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
                    status, record_id, recommendation, current = self.classify_item(parsed, parsed["sha256"])
                    self.conn.execute(
                        """INSERT INTO batch_items(id,batch_id,file_name,staging_file_id,record_id,code,registrant,type,project_category,project_category_source,product,module,version,linked_bom,notes,metadata,current_version,detected_version,status,recommendation,sha256,size,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            item_id,
                            batch_id,
                            parsed["fileName"],
                            parsed["_staging_file_id"],
                            record_id,
                            parsed["code"],
                            parsed["registrant"],
                            parsed["type"],
                            parsed["project_category"],
                            parsed["project_category_source"],
                            parsed["product"],
                            parsed["module"],
                            parsed["version"],
                            parsed["linked_bom"],
                            parsed["notes"],
                            json_dumps(metadata | {"blob_path": parsed["_blob_path"]}),
                            current,
                            parsed["version"],
                            status,
                            recommendation,
                            parsed["sha256"],
                            parsed["size"],
                            now_iso(),
                            now_iso(),
                        ),
                    )
                    if status in {"conflict", "unmatched"}:
                        self.create_exception_for_item(batch_id, item_id, parsed["fileName"], status, recommendation, record_id, parsed.get("code", ""))
                    row = self.execute("SELECT * FROM batch_items WHERE id=?", (item_id,)).fetchone()
                    stored.append(self.row_item(row))
                for dup in self.same_batch_conflicts(batch_id):
                    self.conn.execute(
                        "UPDATE batch_items SET status='conflict',recommendation=?,updated_at=? WHERE id=?",
                        ("同批次同一记录同一版本存在不同文件内容，需人工确认后再归档。", now_iso(), dup["id"]),
                    )
                    self.create_exception_for_item(batch_id, dup["id"], dup["file_name"], "conflict", "同批次同一记录同一版本存在不同文件内容，需人工确认后再归档。", dup["record_id"], dup["code"])
                stored = [self.row_item(r) for r in self.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY created_at,id", (batch_id,)).fetchall()]
                summary = {"total": len(stored), "matched": sum(1 for i in stored if i["status"] == "matched"), "conflict": sum(1 for i in stored if i["status"] == "conflict"), "unmatched": sum(1 for i in stored if i["status"] == "unmatched"), "duplicate": sum(1 for i in stored if i["status"] == "duplicate")}
                self.conn.execute("UPDATE batches SET summary=?,updated_at=? WHERE id=?", (json_dumps(summary), now_iso(), batch_id))
                self.add_log("批量分析", f"{batch_id} · {len(stored)}份", f"完成本地解析；可提交{summary['matched']}份，异常{summary['conflict'] + summary['unmatched']}份，重复{summary['duplicate']}份", batch_id, "完成")
            return {"batch_id": batch_id, "items": stored, "summary": summary, "latestSyncBatch": batch_id}
        except Exception:
            self.cleanup_blobs(staged_paths)
            raise

    def same_batch_conflicts(self, batch_id: str) -> list[object]:
        rows = self.execute("SELECT * FROM batch_items WHERE batch_id=? AND status='matched'", (batch_id,)).fetchall()
        groups: dict[tuple[str, str, str, str, str], list[object]] = {}
        for row in rows:
            key = (*record_identity(dict(row)), row["version"])
            groups.setdefault(key, []).append(row)
        conflicts = []
        for group in groups.values():
            if len({row["sha256"] for row in group}) > 1:
                conflicts.extend(group)
        return conflicts

    def create_exception_for_item(self, batch_id: str, item_id: str, file_name: str, kind: str, detail: str, record_id: str, record_no: str) -> None:
        title = "文件无法自动对应" if kind == "unmatched" else "版本或内容存在冲突"
        item = self.execute("SELECT metadata FROM batch_items WHERE id=?", (item_id,)).fetchone()
        detail = self._append_product_source(detail, json_loads(item["metadata"], {}) if item else {})
        self.conn.execute(
            """INSERT INTO exceptions(id,title,detail,type,record_id,item_id,batch_id,record_no,recommendation,action,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"EX-{uuid.uuid4().hex[:10]}",
                f"{file_name} · {title}",
                detail,
                "无法匹配" if kind == "unmatched" else "版本冲突",
                record_id or "",
                item_id,
                batch_id,
                record_no or "",
                "在批量录入中完成人工核对，或补充对应BOM/受控附件后重新提交。",
                "补充资料后重新校验",
                "open",
                now_iso(),
            ),
        )

    def upsert_sync_exception(self, *, title: str, detail: str, kind: str, record_id: str, file_id: str, record_no: str, recommendation: str) -> None:
        existing = self.conn.execute(
            "SELECT id FROM exceptions WHERE item_id=? AND type=? AND status='open'",
            (file_id, kind),
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE exceptions SET title=?,detail=?,record_id=?,record_no=?,recommendation=?,action=?,created_at=? WHERE id=?",
                (title, detail, record_id or "", record_no or "", recommendation, "修复本地文件或BOM关联后重新同步", now_iso(), existing["id"]),
            )
            return
        self.conn.execute(
            """INSERT INTO exceptions(id,title,detail,type,record_id,item_id,batch_id,record_no,recommendation,action,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"EX-{uuid.uuid4().hex[:10]}",
                title,
                detail,
                kind,
                record_id or "",
                file_id,
                "",
                record_no or "",
                recommendation,
                "修复本地文件或BOM关联后重新同步",
                "open",
                now_iso(),
            ),
        )

    def resolve_sync_exception_if_fixed(self, file_id: str, kind: str) -> None:
        if not file_id:
            return
        fixed, _detail = self.check_sync_exception_fixed(file_id, kind)
        if fixed:
            self.conn.execute("UPDATE exceptions SET status='resolved',resolved_at=? WHERE item_id=? AND type=? AND status='open'", (now_iso(), file_id, kind))

    def review(self, payload: dict) -> dict:
        batch_id = clean_text(payload.get("batch_id"))
        item_id = clean_text(payload.get("item_id"))
        if not batch_id or not item_id:
            raise ValueError("batch_id和item_id必填")
        with self.lock, self.conn.immediate():
            row = self.execute("SELECT * FROM batch_items WHERE id=? AND batch_id=?", (item_id, batch_id)).fetchone()
            if not row:
                raise KeyError("未找到批次项目")
            from backend.editing import require_edit_token
            require_edit_token(payload.get("expected_edit_token"), self.item_edit_token(row))
            if row["status"] == "committed":
                raise ValueError("已归档的核对项不能再次修改，请录入新的资料")
            updates = {}
            for key in ("code", "registrant", "product", "module", "type", "version", "linked_bom", "notes"):
                if key in payload:
                    updates[key] = clean_text(payload.get(key), "待确认" if key == "type" else "")
            if "project_category" in payload:
                updates["project_category"] = normalize_project_category(payload.get("project_category"))
                updates["project_category_source"] = "人工核对"
            record_id = clean_text(payload.get("record_id"))
            context_record_id = clean_text(payload.get("context_record_id"))
            if record_id:
                record = self.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                if not record:
                    raise KeyError("record_id不存在")
                record_id = record["id"]
                updates.setdefault("code", record["business_code"])
                updates.setdefault("registrant", record["registrant"])
                updates.setdefault("product", record["product"])
                updates.setdefault("module", record["module"])
                updates.setdefault("type", record["type"])
                updates.setdefault("project_category", record["project_category"])
                updates.setdefault("project_category_source", record["project_category_source"] or "沿用已有记录")
            if context_record_id:
                ctx = self.execute("SELECT * FROM records WHERE id=?", (context_record_id,)).fetchone()
                if not ctx:
                    raise KeyError("context_record_id不存在")
                context_record_id = ctx["id"]
                updates.setdefault("registrant", ctx["registrant"])
                updates.setdefault("product", ctx["product"])
                updates.setdefault("type", ctx["type"])
                updates.setdefault("project_category", ctx["project_category"])
                updates.setdefault("project_category_source", ctx["project_category_source"] or "沿用上下文记录")
                if not updates.get("linked_bom") and ctx["module"] == "BOM":
                    updates["linked_bom"] = ctx["business_code"]
            item = {**dict(row), **updates}
            item["metadata"] = json_loads(row["metadata"], {})
            item["metadata"].pop("review_pending", None)
            item["metadata"].pop("same_version_revision", None)
            item["metadata"]["review_note"] = clean_text(payload.get("review_note"))
            item = self.inherit_project_category(item)
            item["metadata"].pop("incomplete_archive", None)
            missing_fields = [key for key in ("code", "registrant", "product", "module", "version") if clean_text(item.get(key), "待确认") in ("", "待确认")]
            if payload.get("allow_incomplete") is True and not payload.get("save_pending") and missing_fields:
                item["metadata"]["incomplete_archive"] = {
                    "missing_fields": missing_fields, "confirmed_by": getattr(self._actor, "user", "本地用户"),
                    "confirmed_at": now_iso(), "reason": clean_text(payload.get("review_note")) or "文控确认按现有信息归档",
                    "fingerprint": incomplete_fingerprint(item, row["sha256"]),
                }
                if record_id:
                    item["metadata"]["incomplete_archive"]["selected_record_id"] = record_id
                    item["metadata"]["incomplete_archive"]["selected_record_no"] = item.get("code") or record["business_code"]
            updates["project_category"] = item["project_category"]
            updates["project_category_source"] = item["project_category_source"]
            updates["metadata"] = json_dumps(item["metadata"])
            status, matched_record_id, recommendation, current = self.classify_item(item, row["sha256"])
            if item["metadata"].get("parse_error"):
                matched_record_id = ""
                status = "unmatched"
                current = "—"
                recommendation = f"解析失败：{item['metadata'].get('parse_error')}，需重新上传可解析的原始文件。"
            elif dated_bom_metadata(item):
                if record_id and matched_record_id != record_id:
                    status = "conflict"
                    recommendation = "日期版本资料只能更新同一文件编号及归属的记录。"
            elif record_id:
                matched_record_id = record_id
                record = self.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                current = record["version"]
                conflict = self.category_conflict(item, record)
                project_conflict = self.same_project_category_conflict(item)
                cmp = compare_versions(clean_text(item.get("version")), current)
                if project_conflict:
                    status = "conflict"
                    recommendation = project_conflict
                elif conflict:
                    status = "conflict"
                    recommendation = conflict
                elif cmp is None:
                    status = "conflict"
                    recommendation = f"版本 {clean_text(item.get('version')) or '空'} 无法与当前版本 {current} 严格比较，需人工确认。"
                elif cmp < 0:
                    status = "conflict"
                    recommendation = f"低于当前版本 {current}，禁止覆盖。"
                elif cmp == 0:
                    current_file = self.execute("SELECT * FROM files WHERE record_id=? AND status='当前有效' ORDER BY created_at DESC LIMIT 1", (record_id,)).fetchone()
                    if current_file and current_file["sha256"] == row["sha256"]:
                        status = "duplicate"
                        recommendation = "同版本同文件，提交时将跳过。"
                    elif payload.get("confirm_same_version_revision") is True:
                        note = clean_text(payload.get("review_note"))
                        if not note:
                            raise ValueError("同版本修订确认必须填写核对说明。")
                        if not self._allows_same_version_revision(item):
                            status = "conflict"
                            recommendation = "同版本不同内容仅允许工艺文件或检验文件在文控确认后修订替换。"
                        elif not current_file:
                            status = "conflict"
                            recommendation = "未找到当前有效附件，不能确认同版本修订。"
                        else:
                            item["metadata"]["same_version_revision"] = {
                                "record_id": record_id,
                                "base_version": current,
                                "base_file_id": current_file["id"],
                                "base_sha256": current_file["sha256"],
                                "new_sha256": row["sha256"],
                                "note": note,
                                "confirmed_by": getattr(self._actor, "user", "本地用户"),
                                "confirmed_at": now_iso(),
                            }
                            status = "matched"
                            recommendation = "文控已确认同版本修订替换；提交时会再次校验当前版本和文件校验值。"
                    else:
                        status = "conflict"
                        recommendation = "同版本不同内容，禁止直接覆盖。"
                else:
                    status = "matched"
                    recommendation = "人工核对通过，可提交归档。"
            missing = [key for key in ("code", "registrant", "product", "module", "version") if clean_text(item.get(key), "待确认") in {"", "待确认"}]
            if missing:
                status, _, recommendation, _ = self.classify_item(item, row["sha256"])
            if payload.get("save_pending"):
                item["metadata"]["review_pending"] = True
                status = "unmatched"
                recommendation = "待补充：" + (item["metadata"].get("review_note") or "尚未确认资料信息")
            recommendation = self._append_product_source(recommendation, item["metadata"])
            updates["metadata"] = json_dumps(item["metadata"])
            set_parts = ["edit_revision=edit_revision+1", "result_message=NULL", "record_id=?", "context_record_id=?", "current_version=?", "detected_version=?", "status=?", "recommendation=?", "updated_at=?"]
            params = [matched_record_id, context_record_id, current, updates.get("version", row["version"]), status, recommendation, now_iso()]
            for key, value in updates.items():
                col = "linked_bom" if key == "linked_bom" else key
                set_parts.append(f"{col}=?")
                params.append(value)
            params.append(item_id)
            self.conn.execute(f"UPDATE batch_items SET {', '.join(set_parts)} WHERE id=?", params)
            if status in {"matched", "duplicate"}:
                self.conn.execute("UPDATE exceptions SET status='resolved',resolved_at=? WHERE item_id=? AND status='open'", (now_iso(), item_id))
            else:
                open_ex = self.execute("SELECT id FROM exceptions WHERE item_id=? AND status='open'", (item_id,)).fetchone()
                if not open_ex:
                    self.create_exception_for_item(batch_id, item_id, row["file_name"], status, recommendation, matched_record_id, updates.get("code", row["code"]))
            self.add_log("人工核对", row["file_name"], recommendation, batch_id, "成功" if status in {"matched", "duplicate"} else "待处理")
            return {"item": self.row_item(self.execute("SELECT * FROM batch_items WHERE id=?", (item_id,)).fetchone())}

    def commit_batch(self, batch_id: str, *, initial_latest_only: bool = False) -> dict:
        with self.lock:
            with self.conn.immediate():
                rows = self.conn.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY created_at,id", (batch_id,)).fetchall()
                if not rows:
                    raise KeyError("未找到批次")
                results = []
                buckets: dict[tuple[str, str, str, str], list[object]] = {}
                for row in rows:
                    if row["status"] == "matched":
                        if json_loads(row["metadata"], {}).get("incomplete_archive"):
                            allowed_key = ("manual-incomplete", row["id"], "", "")
                        else:
                            allowed_key = record_identity(dict(row))
                        key = allowed_key
                        buckets.setdefault(key, []).append(row)
                allowed = set()
                blocked_top = set()
                for bucket in buckets.values():
                    if json_loads(bucket[0]["metadata"], {}).get("incomplete_archive"):
                        allowed.update(row["id"] for row in bucket)
                        continue
                    if all(dated_bom_metadata(dict(row)) for row in bucket):
                        # Initial import keeps only the newest dated revision for a new identity.
                        # Include conflicted candidates when choosing the date: never silently
                        # promote an older file because the latest one failed review.
                        if initial_latest_only and not self.find_records_for_item(dict(bucket[0])):
                            candidates = [candidate for candidate in rows
                                          if record_identity(dict(candidate)) == record_identity(dict(bucket[0]))
                                          and dated_bom_metadata(dict(candidate))]
                            latest_date = max(candidate["version"] for candidate in candidates)
                            bucket = [candidate for candidate in bucket if candidate["version"] == latest_date]
                        # Recheck same-date collisions even after manual review.
                        by_date = {}
                        for candidate in bucket:
                            by_date.setdefault(candidate["version"], []).append(candidate)
                        for revisions in by_date.values():
                            if len({candidate["sha256"] for candidate in revisions}) > 1:
                                blocked_top.update(candidate["id"] for candidate in revisions)
                            else:
                                allowed.update(candidate["id"] for candidate in revisions)
                        continue
                    top_rows = highest_version_rows(bucket)
                    top_hashes = {row["sha256"] for row in top_rows}
                    if len(top_hashes) > 1:
                        blocked_top.update(row["id"] for row in top_rows)
                    else:
                        allowed.add(sorted(top_rows, key=lambda r: (r["created_at"], r["id"]))[0]["id"])
                rows = sorted(rows, key=lambda row: (0, row["version"], row["id"]) if dated_bom_metadata(dict(row)) else (1, row["created_at"], row["id"]))
                for row in rows:
                    if row["status"] == "committed":
                        results.append({"item_id": row["id"], "status": "skipped", "message": row["result_message"] or "此文件已归档，无需重复提交。"})
                        continue
                    if row["status"] in {"duplicate", "skipped"}:
                        msg = (row["result_message"] if row["status"] == "skipped" else None) or "同版本同文件，已跳过不重复归档。"
                        self.conn.execute("UPDATE batch_items SET result_message=? WHERE id=?", (msg, row["id"]))
                        results.append({"item_id": row["id"], "status": "skipped", "message": msg})
                        continue
                    if row["status"] != "matched":
                        msg = row["result_message"] or row["recommendation"] or "未通过匹配或版本门禁，保留为异常。"
                        results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                        continue
                    if row["id"] in blocked_top:
                        msg = "同批次同一日期存在不同文件内容，需人工确认，未归档。" if dated_bom_metadata(dict(row)) else "同批次最高版本存在不同文件内容，需人工确认，未归档。"
                        self.create_exception_for_item(batch_id, row["id"], row["file_name"], "conflict", msg, row["record_id"], row["code"])
                        self.conn.execute("UPDATE batch_items SET result_message=?,status='conflict' WHERE id=?", (msg, row["id"]))
                        results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                        continue
                    if row["id"] not in allowed:
                        msg = "同批次同一记录存在更高版本，本项目已跳过。"
                        self.conn.execute("UPDATE batch_items SET result_message=?,status='skipped' WHERE id=?", (msg, row["id"]))
                        results.append({"item_id": row["id"], "status": "skipped", "message": msg})
                        continue
                    item = dict(row)
                    metadata = json_loads(row["metadata"], {})
                    blob_path = metadata.get("blob_path")
                    if not blob_path:
                        msg = "缺少暂存文件，无法归档。"
                        results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                        continue
                    try:
                        blob = self.read_blob(blob_path)
                    except FileNotFoundError:
                        msg = "暂存文件不存在，无法归档。"
                        results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                        continue
                    if sha256_bytes(blob) != row["sha256"]:
                        msg = "暂存文件校验失败，无法归档。"
                        results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                        continue
                    dated = bool(dated_bom_metadata(item))
                    if dated or metadata.get("incomplete_archive") or metadata.get("review_pending"):
                        checked, _matched, checked_message, _current = self.classify_item({**item, "metadata": metadata}, row["sha256"])
                        if checked != "matched":
                            result_status = "skipped" if checked == "duplicate" else "blocked"
                            self.conn.execute("UPDATE batch_items SET status=?,result_message=? WHERE id=?", (checked, checked_message, row["id"]))
                            if checked != "duplicate":
                                self.create_exception_for_item(batch_id, row["id"], row["file_name"], "conflict", checked_message, row["record_id"], row["code"])
                            results.append({"item_id": row["id"], "status": result_status, "message": checked_message})
                            continue
                    project_conflict = self.same_project_category_conflict(item)
                    if project_conflict:
                        self.create_exception_for_item(batch_id, row["id"], row["file_name"], "conflict", project_conflict, row["record_id"], row["code"])
                        results.append({"item_id": row["id"], "status": "blocked", "message": project_conflict})
                        continue
                    existing = [] if metadata.get("incomplete_archive") else self.find_records_for_item(item)
                    record = existing[0] if existing else None
                    if record:
                        conflict = self.category_conflict(item, record)
                        if conflict:
                            self.create_exception_for_item(batch_id, row["id"], row["file_name"], "conflict", conflict, record["id"], record["business_code"])
                            results.append({"item_id": row["id"], "status": "blocked", "message": conflict})
                            continue
                    current_file = self.conn.execute("SELECT * FROM files WHERE record_id=? AND status='当前有效' ORDER BY created_at DESC LIMIT 1", (record["id"],)).fetchone() if record else None
                    historical = False
                    if record and current_file:
                        cmp = compare_versions(row["version"], record["version"])
                        historical = dated and cmp == -1
                        if cmp is None:
                            msg = f"提交时复核失败：版本 {row['version']} 无法与当前版本 {record['version']} 严格比较。"
                            self.create_exception_for_item(batch_id, row["id"], row["file_name"], "conflict", msg, record["id"], record["business_code"])
                            results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                            continue
                        if cmp < 0 and not dated:
                            msg = f"提交时复核失败：低于当前版本 {record['version']}。"
                            self.create_exception_for_item(batch_id, row["id"], row["file_name"], "conflict", msg, record["id"], record["business_code"])
                            results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                            continue
                        if cmp == 0:
                            if current_file["sha256"] == row["sha256"]:
                                msg = "同版本同文件，已跳过不重复归档。"
                                results.append({"item_id": row["id"], "status": "skipped", "message": msg})
                                continue
                            if self._same_version_revision_confirmed(metadata, item, row, record, current_file):
                                pass
                            else:
                                msg = "提交时复核失败：同版本但文件内容不同。"
                                self.create_exception_for_item(batch_id, row["id"], row["file_name"], "conflict", msg, record["id"], record["business_code"])
                                results.append({"item_id": row["id"], "status": "blocked", "message": msg})
                                continue
                    previous_current_version = record["version"] if record else ""
                    if record is None:
                        record = self.create_record_shell(item, batch_id)
                    if current_file and not historical:
                        self.conn.execute("UPDATE files SET status='历史版本' WHERE record_id=? AND status='当前有效'", (record["id"],))
                    file_id = str(uuid.uuid4())
                    self.conn.execute(
                        """INSERT INTO files(id,record_id,name,version,status,date,source,size,type,storage,sha256,blob_path,metadata,linked_bom,link_status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            file_id,
                            record["id"],
                            row["file_name"],
                            row["version"],
                            "历史版本" if historical else "当前有效",
                            _dt.date.today().isoformat(),
                            batch_id,
                            size_label(row["size"]),
                            file_type(row["file_name"]),
                            "本地保存",
                            row["sha256"],
                            blob_path,
                            row["metadata"] or "{}",
                            row["linked_bom"] or "",
                            self.link_status(row["linked_bom"], record["registrant"], record["product"], record["project_category"]),
                            now_iso(),
                        ),
                    )
                    if not historical:
                        self.conn.execute(
                            "UPDATE records SET version=?,previous=?,file_name=?,date=?,controlled=?,source=?,size=?,linked_bom=?,project_category=?,project_category_source=?,metadata=?,updated_at=? WHERE id=?",
                            (row["version"], record["version"], row["file_name"], today_text(), _dt.date.today().isoformat(), batch_id, size_label(row["size"]), row["linked_bom"] or record["linked_bom"], normalize_project_category(row["project_category"]), row["project_category_source"] or "本地录入", row["metadata"] or "{}", now_iso(), record["id"]),
                        )
                        if dated:
                            self.conn.execute("UPDATE records SET metadata=? WHERE id=?", (row["metadata"], record["id"]))
                    if dated:
                        if historical:
                            change = f"补充历史日期版本 {row['version']}；当前有效版本仍为 {previous_current_version}。"
                            action = "补充BOM历史"
                        elif previous_current_version:
                            change = f"{previous_current_version} → {row['version']}"
                            action = "BOM版本变更"
                        else:
                            change = f"新增量产成品 BOM：{row['version']}"
                            action = "新增BOM"
                        self.add_log(action, row["code"], f"{record['registrant']} / {record['project_category']} / {record['product']} / {record['module']}；{change}", batch_id)
                    else:
                        change = f"{previous_current_version} → {row['version']}" if previous_current_version else f"首次归档 {row['version']}"
                        self.add_log("文件版本更新" if previous_current_version else "文件首次归档", row["code"], f"{record['registrant']} / {record['project_category']} / {record['product']} / {record['module']}；{change}", batch_id)
                    self.conn.execute("UPDATE batch_items SET record_id=?,status='committed',result_message=? WHERE id=?", (record["id"], "已归档为当前有效版本。", row["id"]))
                    self.conn.execute("UPDATE exceptions SET status='resolved',resolved_at=? WHERE item_id=? AND status='open'", (now_iso(), row["id"]))
                    results.append({"item_id": row["id"], "status": "committed", "message": "已归档为当前有效版本。", "record_id": record["id"], "file_id": file_id})
                # Every outcome must be durable: UI, batch recovery and mail receipts
                # must observe the same decision, including failures discovered at commit time.
                for result in results:
                    saved_item = self.conn.execute("SELECT * FROM batch_items WHERE id=?", (result["item_id"],)).fetchone()
                    item_status = saved_item["status"]
                    if item_status == "committed":
                        continue
                    if result["status"] == "blocked":
                        if item_status == "matched":
                            item_status = "conflict"
                        existing_exception = self.conn.execute(
                            "SELECT id FROM exceptions WHERE item_id=? AND status='open'", (saved_item["id"],)).fetchone()
                        if existing_exception:
                            self.conn.execute("UPDATE exceptions SET detail=? WHERE item_id=? AND status='open'",
                                              (result["message"], saved_item["id"]))
                        else:
                            self.create_exception_for_item(batch_id, saved_item["id"], saved_item["file_name"],
                                item_status, result["message"], saved_item["record_id"], saved_item["code"])
                    elif result["status"] == "skipped":
                        item_status = "skipped"
                        self.conn.execute("UPDATE exceptions SET status='resolved',resolved_at=? WHERE item_id=? AND status='open'",
                                          (now_iso(), saved_item["id"]))
                    self.conn.execute(
                        "UPDATE batch_items SET status=?,result_message=?,recommendation=?,updated_at=?,edit_revision=edit_revision+1 WHERE id=?",
                        (item_status, result["message"], result["message"], now_iso(), saved_item["id"]))
                dated_records = set()
                for result in results:
                    if result["status"] != "committed":
                        continue
                    saved = self.conn.execute("SELECT * FROM files WHERE id=?", (result["file_id"],)).fetchone()
                    if json_loads(saved["metadata"], {}).get("version_rule") == "filename_date":
                        message = f"已按文件名日期归档为{saved['status']}。"
                        result["message"] = message
                        self.conn.execute("UPDATE batch_items SET result_message=? WHERE id=?", (message, result["item_id"]))
                        dated_records.add(saved["record_id"])
                for record_id in dated_records:
                    previous = self.conn.execute("SELECT version FROM files WHERE record_id=? AND status='历史版本' ORDER BY version DESC LIMIT 1", (record_id,)).fetchone()
                    self.conn.execute("UPDATE records SET previous=? WHERE id=?", (previous["version"] if previous else "", record_id))
                committed = sum(1 for r in results if r["status"] == "committed")
                blocked = sum(1 for r in results if r["status"] == "blocked")
                self.conn.execute("UPDATE batches SET status=?,updated_at=?,summary=? WHERE id=?", ("committed" if committed else "review", now_iso(), json_dumps({"total": len(results), "committed": committed, "blocked": blocked}), batch_id))
                self.add_log("批量录入", f"{batch_id} · {len(rows)}份", f"提交完成：归档{committed}份，阻止{blocked}份，其余跳过。", batch_id, "部分完成" if blocked else "成功")
                self.add_notification("量产DMR文件已本地归档", f"{batch_id} 完成提交：归档{committed}份，阻止{blocked}份。")
                return {"batch_id": batch_id, "results": results, "committed": committed, "blocked": blocked}

    def link_status(self, linked_bom: str, registrant: str = "", product: str = "", project_category: str = "待确认") -> str:
        linked_bom = clean_text(linked_bom)
        if not linked_bom:
            return "未关联"
        rows = self.conn.execute("SELECT id,registrant,product,version,project_category FROM records WHERE business_code=? AND module='BOM' AND EXISTS (SELECT 1 FROM files WHERE files.record_id=records.id)", (linked_bom,)).fetchall()
        if not rows:
            return "BOM缺失"
        registrant = clean_text(registrant)
        product = clean_text(product)
        category = confirmed_project_category(project_category)
        if registrant or product:
            matched = [row for row in rows if (not registrant or row["registrant"] == registrant) and (not product or row["product"] == product)]
            if len(matched) == 1:
                bom_category = confirmed_project_category(matched[0]["project_category"])
                if category and bom_category and category != bom_category:
                    return "BOM项目类别冲突"
                if category and bom_category:
                    return "已关联"
                return "主体产品已匹配，类别待确认"
            if matched:
                return "BOM需人工确认"
            return "BOM主体/产品不匹配"
        return "已关联" if len(rows) == 1 else "BOM需人工确认"

    def file_with_record(self, file_id: str):
        return self.conn.execute(
            """SELECT files.*, records.business_code AS record_no, records.registrant AS record_registrant, records.product AS record_product, records.project_category AS record_project_category
               FROM files JOIN records ON records.id=files.record_id WHERE files.id=?""",
            (file_id,),
        ).fetchone()

    def check_sync_exception_fixed(self, file_id: str, kind: str) -> tuple[bool, str]:
        row = self.file_with_record(file_id)
        if not row:
            return False, "文件记录不存在"
        if kind in {"本地附件缺失", "文件校验失败"}:
            try:
                data = self.read_blob(row["blob_path"])
            except FileNotFoundError:
                return False, "本地附件缺失"
            if sha256_bytes(data) != row["sha256"]:
                return False, "本地附件校验失败"
            return True, "本地附件校验通过"
        if kind == "BOM关联异常":
            status = self.link_status(row["linked_bom"], row["record_registrant"], row["record_product"], row["record_project_category"])
            self.conn.execute("UPDATE files SET link_status=? WHERE id=?", (status, file_id))
            return status in {"已关联", "未关联", "主体产品已匹配，类别待确认"}, status
        return False, "未知异常类型"

    def create_record_shell(self, item: dict, batch_id: str) -> object:
        record_id = str(uuid.uuid4())
        code = clean_text(item.get("code"), f"DMR-{uuid.uuid4().hex[:8]}")
        name = f"{clean_text(item.get('product'), '待确认')} {clean_text(item.get('module'), '受控文件')}"
        project_category = normalize_project_category(item.get("project_category", "待确认"))
        self.conn.execute(
            """INSERT INTO records(id,business_code,name,registrant,type,project_category,project_category_source,product,module,version,previous,file_name,date,controlled,source,owner,size,source_type,linked_bom,metadata,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record_id,
                code,
                name,
                clean_text(item.get("registrant"), "待确认"),
                clean_text(item.get("type"), "待确认"),
                project_category,
                clean_text(item.get("project_category_source"), "本地录入" if project_category != "待确认" else "未确认"),
                clean_text(item.get("product"), "待确认"),
                clean_text(item.get("module"), "待确认"),
                "",
                "",
                "",
                "",
                "",
                batch_id,
                "本地用户",
                "",
                "本地上传",
                clean_text(item.get("linked_bom")),
                item.get("metadata") or "{}",
                now_iso(),
                now_iso(),
            ),
        )
        return self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()

    def recompute_link_statuses(self) -> None:
        rows = self.conn.execute(
            """SELECT files.id, files.linked_bom, records.registrant, records.product, records.project_category
               FROM files JOIN records ON records.id=files.record_id"""
        ).fetchall()
        for row in rows:
            status = self.link_status(row["linked_bom"], row["registrant"], row["product"], row["project_category"])
            self.conn.execute("UPDATE files SET link_status=? WHERE id=?", (status, row["id"]))

    def update_project_classification(self, record_id: str, project_category: str, expected_edit_token=None) -> dict:
        category = normalize_project_category(project_category)
        with self.lock, self.conn.immediate():
            record = self.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if not record:
                raise KeyError("未找到记录")
            from backend.editing import require_edit_token
            require_edit_token(expected_edit_token, self.classification_edit_token(record))
            scope = self.execute("SELECT * FROM records WHERE registrant=? AND product=?", (record["registrant"], record["product"])).fetchall()
            before = {row["id"]: row["project_category"] or "待确认" for row in scope}
            before_counts = {label: list(before.values()).count(label) for label in sorted(set(before.values()))}
            now = now_iso()
            self.conn.execute(
                "UPDATE records SET edit_revision=edit_revision+1,project_category=?,project_category_source=?,updated_at=? WHERE registrant=? AND product=?",
                (category, "人工确认，非企业审批", now, record["registrant"], record["product"]),
            )
            self.conn.execute(
                """UPDATE batch_items SET edit_revision=edit_revision+1,project_category=?,project_category_source=?,updated_at=?
                   WHERE registrant=? AND product=? AND status!='committed'
                   AND (project_category IS NULL OR project_category='' OR project_category='待确认'
                        OR project_category_source IN ('继承同主体同产品已确认类别','migration_default','未提供','未确认'))""",
                (category, "人工确认，非企业审批", now, record["registrant"], record["product"]),
            )
            self.recompute_link_statuses()
            after_rows = self.execute("SELECT * FROM records WHERE registrant=? AND product=? ORDER BY business_code,module", (record["registrant"], record["product"])).fetchall()
            changed = [
                {"record_id": row["id"], "no": row["business_code"], "before": before.get(row["id"], "待确认"), "after": row["project_category"]}
                for row in after_rows
                if before.get(row["id"], "待确认") != row["project_category"]
            ]
            self.add_log(
                "项目类别确认",
                f"{record['registrant']} · {record['product']}",
                f"人工确认项目类别：{before_counts} → {category}；范围：同注册人主体+同产品，共{len(after_rows)}条记录；本地确认不代表企业审批。",
                f"CLASSIFY-{uuid.uuid4().hex[:8]}",
                "成功",
            )
            return {"project_category": category, "scope": {"registrant": record["registrant"], "product": record["product"], "record_count": len(after_rows)}, "changed": changed}

    def get_batch(self, batch_id: str) -> dict:
        batch = self.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise KeyError("未找到批次")
        items = [self.row_item(r) for r in self.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY created_at,id", (batch_id,)).fetchall()]
        return dict(batch) | {"summary": json_loads(batch["summary"], {}), "items": items}

    def resolve_exception(self, exception_id: str) -> dict:
        with self.lock, self.conn:
            ex = self.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
            if not ex:
                raise KeyError("未找到异常")
            if ex["status"] != "open":
                return {"exception": self.row_exception(ex), "resolved": True}
            if ex["item_id"] and ex["type"] in {"本地附件缺失", "文件校验失败", "BOM关联异常"}:
                fixed, detail = self.check_sync_exception_fixed(ex["item_id"], ex["type"])
                if not fixed:
                    raise ValueError(f"异常仍未被修复，不能关闭：{detail}")
                self.conn.execute("UPDATE exceptions SET status='resolved',resolved_at=? WHERE id=?", (now_iso(), exception_id))
                self.add_log("异常处理", ex["record_no"] or exception_id, f"复核通过并关闭异常：{detail}", exception_id, "成功")
                return {"exception": self.row_exception(self.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()), "resolved": True}
            item = self.execute("SELECT * FROM batch_items WHERE id=?", (ex["item_id"],)).fetchone() if ex["item_id"] else None
            if item and item["status"] not in {"committed", "duplicate"}:
                raise ValueError("异常仍未被修复，不能仅关闭；请先完成人工核对或重新提交。")
            if "BOM" in (ex["detail"] or "") or "BOM" in (ex["recommendation"] or ""):
                linked = item["linked_bom"] if item else ""
                if linked and not self.execute("SELECT id FROM records WHERE business_code=? AND module='BOM'", (linked,)).fetchone():
                    raise ValueError("关联BOM仍不存在，不能关闭异常。")
            self.conn.execute("UPDATE exceptions SET status='resolved',resolved_at=? WHERE id=?", (now_iso(), exception_id))
            self.add_log("异常处理", ex["record_no"] or exception_id, "复核通过并关闭异常", exception_id, "成功")
            return {"exception": self.row_exception(self.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()), "resolved": True}

    def sync_local(self) -> dict:
        with self.lock, self.conn:
            changes = []
            file_rows = self.execute(
                """SELECT files.*, records.business_code AS record_no, records.registrant AS record_registrant, records.product AS record_product, records.project_category AS record_project_category
                   FROM files JOIN records ON records.id=files.record_id"""
            ).fetchall()
            for row in file_rows:
                file_issue = ""
                try:
                    data = self.read_blob(row["blob_path"])
                    if sha256_bytes(data) != row["sha256"]:
                        file_issue = "本地附件校验失败"
                except FileNotFoundError:
                    file_issue = "本地附件缺失"
                if file_issue:
                    kind = "本地附件缺失" if file_issue == "本地附件缺失" else "文件校验失败"
                    self.upsert_sync_exception(
                        title=f"{row['name']} · {file_issue}",
                        detail=file_issue,
                        kind=kind,
                        record_id=row["record_id"],
                        file_id=row["id"],
                        record_no=row["record_no"],
                        recommendation="从原始上传批次重新归档该文件，或恢复本地附件后重新同步。",
                    )
                    changes.append({"source": "本地附件仓库", "object": row["name"], "before": row["sha256"], "after": file_issue, "detail": file_issue, "result": "待处理", "status": "warning"})
                else:
                    self.resolve_sync_exception_if_fixed(row["id"], "本地附件缺失")
                    self.resolve_sync_exception_if_fixed(row["id"], "文件校验失败")

                link_status_now = self.link_status(row["linked_bom"], row["record_registrant"], row["record_product"], row["record_project_category"])
                if link_status_now != (row["link_status"] or ""):
                    self.conn.execute("UPDATE files SET link_status=? WHERE id=?", (link_status_now, row["id"]))
                if row["linked_bom"] and link_status_now not in {"已关联", "主体产品已匹配，类别待确认"}:
                    change_detail = "关联BOM不存在" if link_status_now == "BOM缺失" else link_status_now
                    self.upsert_sync_exception(
                        title=f"{row['name']} · BOM关联异常",
                        detail=link_status_now,
                        kind="BOM关联异常",
                        record_id=row["record_id"],
                        file_id=row["id"],
                        record_no=row["record_no"],
                        recommendation="补充同主体、同产品的BOM记录，或修正该文件的BOM关联后重新同步。",
                    )
                    changes.append({"source": "本地关联检查", "object": row["record_no"], "before": row["linked_bom"], "after": link_status_now, "detail": change_detail, "result": "待处理", "status": "warning"})
                else:
                    self.resolve_sync_exception_if_fixed(row["id"], "BOM关联异常")
            payload = {"time": today_text(), "batch": f"LOCAL-CHECK-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}", "trigger": "本地检查", "confirmed": False, "confirmedBy": "", "changes": changes}
            self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('latest_sync',?)", (json_dumps(payload),))
            self.add_log("同步数据", "本地仓库检查", f"检查本地附件和BOM关联；发现{len(changes)}项问题。外部飞书、邮箱、PLM未接入。", payload["batch"], "待确认" if changes else "成功")
            return payload

    def confirm_sync(self) -> dict:
        with self.lock, self.conn:
            row = self.execute("SELECT value FROM settings WHERE key='latest_sync'").fetchone()
            payload = json_loads(row["value"], {}) if row else {}
            payload["confirmed"] = True
            payload["confirmedBy"] = f"本地用户 · {today_text()}确认"
            self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('latest_sync',?)", (json_dumps(payload),))
            self.add_log("同步确认", payload.get("batch", "LOCAL-CHECK"), "确认本地检查结果；未代表外部审批。", payload.get("batch", ""), "已确认")
            return payload

    def mark_notifications_read(self) -> dict:
        with self.lock, self.conn:
            self.conn.execute("UPDATE notifications SET read=1")
            return {"ok": True}


def preview_xlsx(data: bytes, name: str) -> str:
    try:
        with safe_zip(data) as zf:
            shared = []
            if "xl/sharedStrings.xml" in zf.namelist():
                root = ET.fromstring(read_zip_xml(zf, "xl/sharedStrings.xml"))
                for si in root.findall(f"{XLSX_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{XLSX_NS}t")))
            sheet_name = next((n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), "")
            rows = []
            if sheet_name:
                root = ET.fromstring(read_zip_xml(zf, sheet_name))
                for row in root.iter(f"{XLSX_NS}row"):
                    cells = []
                    for c in row.findall(f"{XLSX_NS}c"):
                        value = ""
                        v = c.find(f"{XLSX_NS}v")
                        if v is not None:
                            value = v.text or ""
                        elif c.attrib.get("t") == "inlineStr":
                            value = "".join(t.text or "" for t in c.iter(f"{XLSX_NS}t"))
                        if c.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                        cells.append(html.escape(value or ""))
                    rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
                    if len(rows) >= MAX_PREVIEW_ROWS:
                        break
            table = "<table border='1' cellspacing='0' cellpadding='4'>" + "".join(rows) + "</table>" if rows else "<p>未读取到工作表行。</p>"
            return f"<!doctype html><meta charset='utf-8'><title>{html.escape(name)}</title><h1>{html.escape(name)}</h1>{table}"
    except Exception as exc:  # noqa: BLE001
        return f"<!doctype html><meta charset='utf-8'><title>{html.escape(name)}</title><p>无法预览Excel：{html.escape(str(exc))}</p>"
