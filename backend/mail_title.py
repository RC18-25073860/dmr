from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.service import clean_text
from backend.subjects import ALLOWED_ARCHIVE_SUBJECTS


PROJECT_CATEGORIES = {"医疗": ("医疗", "医疗项目", "国内医疗", "海外医疗"), "家电": ("家电", "家电项目")}
MODULE_ALIASES = {
    "承认书&规格书": ("承认书&规格书", "承认书", "规格书"),
    "检验文件": ("检验文件", "检验规范", "检验记录"),
    "工艺文件": ("工艺文件", "作业指导书", "工艺流程图", "工艺规程"),
    "包装图纸": ("包装图纸", "包装"),
    "结构图纸": ("结构图纸", "结构"),
    "软件": ("软件", "固件", "配置文件", "履历表", "软件履历"),
    "BOM": ("BOM",),
}
MISSING_VALUES = {"", "待确认", "未确认"}
ECN_SUBJECT_RE = re.compile(r"(?<![A-Za-z])(?:ECN|ENC)(?![A-Za-z])", re.I)
BOM_SUBJECT_RE = re.compile(r"(?<![A-Za-z])BOM(?![A-Za-z])", re.I)
TITLE_PRODUCT_RE = re.compile(
    r"^(?:UI|UIM|UM|JI|JD|JSG|JCS|EM|EG|UC|UN|UD|UB|JM|MI|W|G|M)\d{1,3}[A-Z]{0,2}(?:\s+(?:PR|PRO(?:MAX)?|GR|DB|RE|PW|GP|WH|MP|BL|PN|BR|WG|BK|CB))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MailTitleField:
    value: str
    evidence: str


def classify_mail_subject(subject: str) -> str:
    """Classify the mail event, separately from the types of its attachments."""
    if ECN_SUBJECT_RE.search(subject or ""):
        return "ecn"
    if BOM_SUBJECT_RE.search(subject or ""):
        return "bom"
    return "other"


def extract_mail_title(subject: str) -> dict[str, Any]:
    """Extract conservative DMR hints from a mail subject.

    The title is treated as supporting evidence only. Versions are deliberately
    ignored here because different file types use different version rules.
    """
    text = clean_text(subject)
    if not text:
        return {}

    fields: dict[str, MailTitleField] = {}
    registrant = _single_match(text, ALLOWED_ARCHIVE_SUBJECTS)
    if registrant:
        fields["registrant"] = MailTitleField(registrant, registrant)

    category = _single_category(text)
    if category:
        fields["project_category"] = MailTitleField(category, category)

    # An ECN mail may mention BOM files, but the mail itself is not a BOM issue.
    module = "" if classify_mail_subject(text) == "ecn" else _single_module(text)
    if module:
        fields["module"] = MailTitleField(module, module)

    product = _product_from_segments(text, fields)
    if product:
        fields["product"] = MailTitleField(product, product)

    if not fields:
        return {}
    return {
        "source": "mail_subject",
        "subject": text,
        "fields": {key: {"value": field.value, "evidence": field.evidence} for key, field in fields.items()},
    }


def apply_mail_title_evidence(item: dict[str, Any], title: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not title or not isinstance(title.get("fields"), dict):
        return item, {}

    updated = dict(item)
    metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
    metadata = dict(metadata)
    applied: list[str] = []
    conflicts: list[dict[str, str]] = []
    title_fields = title["fields"]

    for field, payload in title_fields.items():
        if field not in {"registrant", "project_category", "product", "module"} or not isinstance(payload, dict):
            continue
        value = clean_text(payload.get("value"))
        if not value:
            continue
        current = clean_text(updated.get(field), "待确认")
        if metadata.get(field + "_reading_status") == "pending" or metadata.get(field + "_filename_conflict"):
            conflicts.append({"field": field, "existing": "文件内存在多个候选", "mail_title": value})
            continue
        if current in MISSING_VALUES:
            updated[field] = value
            applied.append(field)
            if field == "project_category":
                updated["project_category_source"] = "邮件标题"
            continue
        if not _compatible_field(field, current, value):
            conflicts.append({"field": field, "existing": current, "mail_title": value})

    mail_title = {
        "source": title.get("source", "mail_subject"),
        "subject": title.get("subject", ""),
        "fields": title_fields,
        "applied": applied,
        "conflicts": conflicts,
        "status": "conflict" if conflicts else ("applied" if applied else "informational"),
    }
    metadata["mail_title"] = mail_title
    if conflicts:
        labels = {"registrant": "公司主体", "project_category": "项目类别", "product": "产品型号", "module": "DMR子模块"}
        parts = [f"{labels.get(row.get('field'), row.get('field'))}：文件为{row.get('existing')}，邮件标题为{row.get('mail_title')}" for row in conflicts]
        metadata["review_pending"] = True
        metadata["review_note"] = "邮件标题与文件解析结果不一致，需人工确认：" + "；".join(parts)
    updated["metadata"] = metadata
    return updated, mail_title


def _single_match(text: str, values) -> str:
    matches = sorted({value for value in values if value and value in text})
    return matches[0] if len(matches) == 1 else ""


def _single_category(text: str) -> str:
    matches = []
    for category, aliases in PROJECT_CATEGORIES.items():
        if any(alias in text for alias in aliases):
            matches.append(category)
    matches = sorted(set(matches))
    return matches[0] if len(matches) == 1 else ""


def _single_module(text: str) -> str:
    matches = []
    for module, aliases in MODULE_ALIASES.items():
        if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.IGNORECASE) for alias in aliases):
            matches.append(module)
    matches = sorted(set(matches))
    return matches[0] if len(matches) == 1 else ""


def _product_from_segments(text: str, fields: dict[str, MailTitleField]) -> str:
    from product_identity import models

    chunks = [clean_text(part) for part in re.split(r"[|｜/／>＞,，;；]+|[-—–]+", text)]
    candidates: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if _chunk_matches_known_field(chunk, fields):
            continue
        cleaned = _clean_product_chunk(chunk)
        product_models = models(cleaned)
        if len(product_models) == 1 and _normalized(product_models[0]) == _normalized(cleaned):
            candidates.append(product_models[0])
        elif TITLE_PRODUCT_RE.fullmatch(cleaned):
            candidates.append(cleaned.upper())
    candidates = list(dict.fromkeys(candidates))
    return candidates[0] if len(candidates) == 1 else ""


def _chunk_matches_known_field(chunk: str, fields: dict[str, MailTitleField]) -> bool:
    for field in fields.values():
        if field.evidence and field.evidence in chunk:
            return True
    return any(token in chunk for token in ("DMR", "清单", "更新", "上传", "附件", "资料", "项目"))


def _clean_product_chunk(chunk: str) -> str:
    text = clean_text(chunk)
    text = re.sub(r"^(产品|产品型号|型号|机型)[:：\s]*", "", text)
    text = re.sub(r"\bV\d+(?:\.\d+)*\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b20\d{6}\b", "", text)
    return re.sub(r"\s+", " ", text).strip(" _-")


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", clean_text(value)).upper()


def _compatible_field(field: str, existing: str, title_value: str) -> bool:
    if _normalized(existing) == _normalized(title_value):
        return True
    if field != "product":
        return False
    from product_identity import compatible, models

    left = models(existing)
    right = models(title_value)
    if len(left) == 1 and len(right) == 1 and compatible(left[0], right[0]):
        return True
    return False
