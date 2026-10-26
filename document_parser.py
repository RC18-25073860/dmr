from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


MAX_ZIP_MEMBERS = 1200
MAX_XML_BYTES = 12 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 48 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
BUNDLED_PDF_PYTHON = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

SOFTWARE_FILE_RE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_&().-]*?"
    r"(?P<version>[VA]\d+(?:\.\d+){2,})"
    r"\.(?:hex|socprj|zip|bin|dfu|elf))",
    re.I,
)
VERSION_RE = re.compile(r"([VA]\d+(?:\.\d+)+)", re.I)


def parse_document_file(data: bytes, filename: str) -> dict[str, Any]:
    """Parse DOCX/PDF software document content into a parsers.py-compatible item."""
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    item = _blank_record(name, data)
    item["type"] = "Word附件" if suffix == ".docx" else "PDF附件"

    if suffix == ".docx":
        text = extract_docx_text(data)
        item["metadata"]["kind"] = "docx"
    elif suffix == ".pdf":
        text = extract_pdf_text(data)
        item["metadata"]["kind"] = "pdf"
        item["metadata"]["pdf_text_extractor"] = "pdfplumber_or_pypdf_subprocess" if text else "unavailable"
    else:
        raise ValueError(f"unsupported document type: {suffix or '<none>'}")

    # A printed BOM distribution email is evidence of a release, not the BOM
    # workbook itself and not a software version document.
    if suffix == ".pdf" and name.startswith("BOM发放"):
        item["code"] = Path(name).stem
        item["module"] = "BOM"
        item["type"] = "BOM发放通知"
        item["metadata"]["management_kind"] = "bom_notice"
        item["metadata"]["document_role"] = "bom_release_notice"
        item["metadata"]["text_preview"] = text[:1000]
        item["notes"].append("这是BOM发放通知，不是BOM原始表格；请核对邮件中的编号、版本及附件后再归档。")
        return item

    from product_identity import models
    item["metadata"]["product_body_candidates"] = models(text)
    evidence = extract_software_version(text, name)
    item["metadata"]["text_preview"] = text[:1000]
    item["metadata"]["document_parse"] = evidence
    item["metadata"]["management_kind"] = {
        "software_release_record": "release",
        "software_history": "history",
    }.get(evidence["document_role"], "software")
    item["metadata"]["document_role"] = evidence["document_role"]
    item["metadata"]["version_semantic"] = {
        "发布版本": "software_release_document_version",
        "完整版本": "software_complete_version",
        "软件文件名": "software_program_version",
    }.get(evidence["version_source"], "")
    if evidence.get("software_filename"):
        item["metadata"]["identifiers"]["software_filename"] = evidence["software_filename"]
        item["metadata"]["identifiers"]["software_branch"] = _strip_version_from_stem(_strip_ext(str(evidence["software_filename"])))
    if evidence.get("version"):
        item["metadata"]["field_evidence"].append(
            {
                "field": "version",
                "source": evidence["version_source"],
                "value": evidence["version"],
                "scope": item["metadata"]["version_semantic"],
            }
        )
    item["metadata"]["version_candidates"] = evidence["version_candidates"]
    item["metadata"]["version_evidence"] = evidence["version_evidence"]

    if "软件" in name or "软件" in text:
        item["module"] = "软件"
    if "履历" in name or "软件履历" in text:
        item["type"] = "软件履历"
    elif "发布记录" in name or "软件发布记录" in text or "发布表" in name or "软件发布表" in text:
        item["type"] = "软件发布记录"

    if evidence["version"]:
        item["version"] = evidence["version"]
        item["code"] = _strip_date_suffix(_strip_version_from_stem(_strip_ext(name)))
        item["notes"].append(f"从{evidence['version_source']}提取版本。")
    else:
        item["code"] = _strip_date_suffix(_strip_version_from_stem(_strip_ext(name)))
        if suffix == ".pdf" and not text:
            item["notes"].append("PDF 文本提取器不可用或未能提取文本，需人工核对。")
        else:
            item["notes"].append("未找到唯一明确的文档版本/完整软件版本，需人工核对。")

    if evidence.get("product"):
        item["product"] = str(evidence["product"])

    return item


def extract_docx_text(data: bytes) -> str:
    texts: list[str] = []
    total = 0
    with _safe_zip(data) as zf:
        names = [
            name
            for name in zf.namelist()
            if name.startswith("word/")
            and name.endswith(".xml")
            and (
                name == "word/document.xml"
                or name.startswith("word/header")
                or name.startswith("word/footer")
                or name.startswith("word/footnotes")
                or name.startswith("word/endnotes")
            )
        ]
        for name in names:
            raw = _read_zip_xml(zf, name)
            total += len(raw)
            if total > MAX_TOTAL_XML_BYTES:
                raise ValueError("docx XML total bytes exceeds limit")
            texts.append(_xml_text(raw))
    return _compact_text("\n".join(texts))


def extract_pdf_text(data: bytes) -> str:
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("pdf bytes exceed limit")
    return _extract_pdf_text_with_subprocess(data)


def extract_software_version(text: str, filename: str = "") -> dict[str, Any]:
    compact = _compact_text(text)
    role = _document_role(compact, filename)
    file_matches = _software_file_matches(compact)
    unique_file_match = _unique_software_file_match(file_matches)
    complete_match = _unique_labeled_version(compact, "完整版本")
    release_match = _unique_labeled_version(compact, "发布版本")

    selected_version = ""
    source = ""
    source_filename = ""
    candidates: list[dict[str, Any]] = []
    for version in _labeled_versions(compact, "发布版本"):
        candidates.append({"source": "发布版本", "version": version, "scope": "release_document"})
    for version in _labeled_versions(compact, "完整版本"):
        candidates.append({"source": "完整版本", "version": version, "scope": "software_complete"})
    for match in file_matches:
        candidates.append(
            {
                "source": "软件文件名",
                "version": _normalize_version(match.group("version")),
                "scope": "software_file",
                "software_filename": match.group("name"),
            }
        )

    if role == "software_release_record":
        if release_match:
            selected_version = release_match
            source = "发布版本"
            if unique_file_match:
                source_filename = unique_file_match.group("name")
    elif complete_match and _version_depth(complete_match) >= 4:
        selected_version = complete_match
        source = "完整版本"
        if unique_file_match:
            source_filename = unique_file_match.group("name")
    elif unique_file_match and role == "software_history":
        selected_version = _normalize_version(unique_file_match.group("version"))
        source = "软件文件名"
        source_filename = unique_file_match.group("name")

    product = _product_from_text(compact, filename)

    return {
        "version": selected_version,
        "version_source": source,
        "document_role": role,
        "software_filename": source_filename,
        "firmware_version": _normalize_version(unique_file_match.group("version")) if unique_file_match else "",
        "complete_version": complete_match,
        "release_version": release_match,
        "document_version": release_match if role == "software_release_record" else "",
        "version_candidates": _mark_selected(_dedupe_candidates(candidates), selected_version, source),
        "version_evidence": {
            "source": source,
            "version": selected_version,
            "software_filename": source_filename,
            "snippet": _evidence_snippet(compact, source_filename or selected_version or release_match),
        },
        "linked_bom": "",
        "registrant": "",
        "product": product,
        "evidence": _evidence_snippet(compact, source_filename or selected_version or release_match),
    }


def _blank_record(filename: str, data: bytes) -> dict[str, Any]:
    return {
        "code": "",
        "registrant": "",
        "product": "",
        "module": "",
        "type": "",
        "project_category": "待确认",
        "project_category_source": "未提供",
        "version": "",
        "linked_bom": "",
        "notes": [],
        "metadata": {
            "kind": "unknown",
            "management_kind": "unknown",
            "sha256": hashlib.sha256(data).hexdigest(),
            "source_filename": filename,
            "size": len(data),
            "identifiers": {},
            "dates": {},
            "field_evidence": [],
        },
    }


def _safe_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(BytesIO(data))
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError("zip member count exceeds limit")
    total = 0
    for info in infos:
        if info.filename.startswith("/") or ".." in Path(info.filename).parts:
            raise ValueError("zip member path is unsafe")
        total += info.file_size
        if info.file_size > MAX_XML_BYTES and info.filename.endswith(".xml"):
            raise ValueError("zip XML member exceeds limit")
        if total > MAX_TOTAL_XML_BYTES:
            raise ValueError("zip uncompressed bytes exceed limit")
    return zf


def _read_zip_xml(zf: zipfile.ZipFile, name: str) -> bytes:
    raw = zf.read(name)
    if b"<!DOCTYPE" in raw[:2048].upper() or b"<!ENTITY" in raw[:4096].upper():
        raise ValueError("XML doctype/entity declarations are not allowed")
    return raw


def _xml_text(raw: bytes) -> str:
    root = ET.fromstring(raw)
    values: list[str] = []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag in {"t", "instrText"} and elem.text:
            values.append(elem.text)
        elif tag in {"tab", "br", "cr", "p"}:
            values.append("\n" if tag == "p" else " ")
    return "".join(values)


def _extract_pdf_text_with_subprocess(data: bytes) -> str:
    python = _pdf_python()
    if not python:
        return ""
    script = (
        "import io, json, sys\n"
        "data = sys.stdin.buffer.read()\n"
        "text = ''\n"
        "try:\n"
        "    import pdfplumber\n"
        "    with pdfplumber.open(io.BytesIO(data)) as pdf:\n"
        "        text = '\\n'.join(page.extract_text() or '' for page in pdf.pages)\n"
        "except Exception:\n"
        "    try:\n"
        "        from pypdf import PdfReader\n"
        "        reader = PdfReader(io.BytesIO(data))\n"
        "        text = '\\n'.join(page.extract_text() or '' for page in reader.pages)\n"
        "    except Exception:\n"
        "        text = ''\n"
        "sys.stdout.write(json.dumps({'text': text}, ensure_ascii=False))\n"
    )
    try:
        proc = subprocess.run(
            [str(python), "-c", script],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "ignore") or "{}")
    except json.JSONDecodeError:
        return ""
    return _compact_text(str(payload.get("text") or ""))


def _pdf_python() -> Path | None:
    configured = os.environ.get("DMR_DOCUMENT_PYTHON", "").strip()
    if configured:
        path = Path(configured)
        return path if path.exists() else None
    if BUNDLED_PDF_PYTHON.exists() and Path(sys.executable) != BUNDLED_PDF_PYTHON:
        return BUNDLED_PDF_PYTHON
    return None


def _software_file_matches(text: str) -> list[re.Match[str]]:
    matches = list(SOFTWARE_FILE_RE.finditer(text))
    hex_matches = [m for m in matches if m.group("name").lower().endswith(".hex")]
    return hex_matches or matches


def _unique_software_file_match(matches: list[re.Match[str]]) -> re.Match[str] | None:
    by_version = {_normalize_version(match.group("version")) for match in matches}
    by_version.discard("")
    if len(by_version) != 1:
        return None
    by_name = {match.group("name") for match in matches}
    return next(match for match in matches if match.group("name") == sorted(by_name)[0])


def _document_role(text: str, filename: str) -> str:
    haystack = filename + "\n" + text[:300]
    if "发布记录" in haystack or "发布表" in haystack:
        return "software_release_record"
    if "履历" in haystack:
        return "software_history"
    return "software_document"


def _mark_selected(candidates: list[dict[str, Any]], version: str, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        row["selected"] = row.get("version") == version and row.get("source") == source
        rows.append(row)
    return rows


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        key = (
            str(candidate.get("source") or ""),
            str(candidate.get("version") or ""),
            str(candidate.get("scope") or ""),
            str(candidate.get("software_filename") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(candidate)
    return rows


def _unique_labeled_version(text: str, label: str) -> str:
    versions = set(_labeled_versions(text, label))
    if label == "完整版本":
        versions = {version for version in versions if _version_depth(version) >= 4}
    return next(iter(versions)) if len(versions) == 1 else ""


def _labeled_versions(text: str, label: str) -> list[str]:
    versions: list[str] = []
    for match in re.finditer(rf"{re.escape(label)}\s*[:：]\s*({VERSION_RE.pattern})", text, re.I):
        version = _normalize_version(match.group(1))
        if version:
            versions.append(version)
    return versions


def _normalize_version(value: str) -> str:
    match = VERSION_RE.search(value or "")
    return match.group(1).upper() if match else ""


def _version_depth(value: str) -> int:
    version = _normalize_version(value)
    return len(version[1:].split(".")) if version else 0


def _product_from_text(text: str, filename: str) -> str:
    for pattern in (
        r"(UI20S\s*GR)\s*[强\[]",
        r"\b(UI20S)\b",
        r"型号[:：]\s*([A-Za-z0-9]+)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            product = re.sub(r"\s+", " ", match.group(1)).upper()
            if product.isdigit():
                continue
            return product
    return ""


def _evidence_snippet(text: str, needle: str) -> str:
    if not needle:
        return ""
    index = text.find(needle)
    if index == -1:
        return ""
    start = max(0, index - 80)
    end = min(len(text), index + len(needle) + 80)
    return text[start:end]


def _compact_text(text: str) -> str:
    text = re.sub(r"[\r\t\xa0]+", " ", text or "")
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    return text.strip()


def _strip_ext(name: str) -> str:
    return re.sub(r"\.[^.]+$", "", Path(name).name)


def _strip_version_from_stem(stem: str) -> str:
    stem = re.sub(r"[_\-\s]*[VA]\d+(?:\.\d+)+.*$", "", stem, flags=re.I)
    return stem.strip(" _-")


def _strip_date_suffix(stem: str) -> str:
    return re.sub(r"[-_\s]*(?:20\d{2}[._-]?\d{1,2}[._-]?\d{1,2}|20\d{6}).*$", "", stem).strip(" _-")
