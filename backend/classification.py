from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any


PENDING = "待确认"
MEDICAL = "医疗"
HOME = "家电"
MANUAL_SOURCE = "人工核对"
RULE_VERSION = "2026-09-22-r1"

SOURCE_CATALOG = "用户提供截图目录"
SOURCE_RUIRUI_BOM = "瑞瑞BOM规则"
SOURCE_FILENAME = "文件名字段"
SOURCE_DECLARED = "原文件显式分类"

MEDICAL_PRODUCTS = {
    "UI04(A1)",
    "UI04(A2)",
    "UI06",
    "UI05",
    "UI07",
    "Y6",
    "Y6S",
    "JI08",
    "UIM20",
    "UI20",
    "UI20S",
    "UI04S",
    "UI06S",
    "MI01",
    "JD20U",
    "M12Promax",
    "M12U",
    "M30U",
    "UM10",
    "UI10",
}

HOME_PRODUCTS = {
    "EM50",
    "JSG10",
    "JCS10",
    "JD10",
    "M12",
    "M30",
    "UC06",
    "UC20S",
    "UN20",
    "UD20",
    "JD20",
    "G20",
    "G18",
    "EG50",
    "W10",
    "G10",
    "JM20",
    "W20",
    "UB02",
}

PRODUCT_ALIASES = {
    "W20AI": "W20",
}

BOM_MEDICAL_REGISTRANTS = {"广西由莱", "新加坡由莱"}

VALID_CATEGORIES = {MEDICAL, HOME, PENDING}
NON_MEDICAL_RE = re.compile(r"(?:非医疗|非\s*医疗|非医用|非\s*医用)")
MEDICAL_RE = re.compile(r"(?:医疗版|国内医疗版|医疗项目|医疗器械|医疗)")
HOME_RE = re.compile(r"(?:家电|小家电|家用电器|非医疗)")


def apply_classification(item: dict, filename: str = "") -> dict:
    """Return a classified copy of item with traceable project metadata."""

    result = deepcopy(item)
    metadata = _metadata_dict(result.get("metadata"))
    product_text = _product_evidence_text(result, filename)
    file_type = _clean(result.get("type"))
    module = _clean(result.get("module"))
    is_bom = file_type.upper() == "BOM" or module.upper() == "BOM"
    declared_category = _normal_category(result.get("project_category"))
    declared_source = _clean(result.get("project_category_source"))

    evidence: list[dict[str, Any]] = []
    prior = metadata.get("project_classification", {})
    if prior:
        evidence.extend(row for row in prior.get("evidence", []) if row.get("source") == SOURCE_DECLARED)
    if not prior and declared_category in {MEDICAL, HOME} and declared_source not in {SOURCE_CATALOG, SOURCE_RUIRUI_BOM, SOURCE_FILENAME, "继承同主体同产品已确认类别"}:
        evidence.append(
            _evidence(
                declared_category,
                SOURCE_DECLARED,
                "原文件或上游解析已有明确分类",
                {"field": "project_category", "source": declared_source or "未提供"},
            )
        )

    evidence.extend(_match_products(product_text))

    if is_bom:
        registrant = _clean(result.get("registrant"))
        if registrant in BOM_MEDICAL_REGISTRANTS:
            evidence.append(
                _evidence(
                    MEDICAL,
                    SOURCE_RUIRUI_BOM,
                    f"BOM主体精确命中：{registrant}",
                    {"field": "registrant", "value": registrant},
                )
            )

    filename_match = _filename_evidence(filename)
    if filename_match:
        evidence.append(filename_match)

    final_category, source, conflict = _resolve(evidence)
    if source == SOURCE_DECLARED:
        source = explicit_source = next(
            (row.get("data", {}).get("source") for row in evidence if row.get("source") == SOURCE_DECLARED),
            "",
        )
        source = explicit_source or SOURCE_DECLARED
    elif source == "未确认":
        source = declared_source or "未提供"
    if "人工" in declared_source and declared_category in VALID_CATEGORIES:
        final_category = declared_category
        source = declared_source
        conflict = False
        evidence.append(
            _evidence(
                declared_category,
                declared_source,
                "人工分类已确认，保留人工结果",
                {"field": "project_category_source"},
            )
        )

    metadata["project_classification"] = {
        "rule_version": RULE_VERSION,
        "source_note": "依据用户提供截图目录与瑞瑞BOM规则；尚未接入外部知识库或企业主系统。",
        "evidence": evidence,
        "conflict": conflict,
    }
    result["metadata"] = metadata
    result["project_category"] = final_category
    result["project_category_source"] = source
    return result


def _metadata_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return deepcopy(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"raw_metadata": value}
        return parsed if isinstance(parsed, dict) else {"raw_metadata": parsed}
    return {}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normal_category(value: Any) -> str:
    text = _clean(value)
    aliases = {
        "医疗项目": MEDICAL,
        "医疗器械": MEDICAL,
        "家电项目": HOME,
        "小家电": HOME,
        "非医疗": HOME,
    }
    return aliases.get(text, text if text in VALID_CATEGORIES else "")


def _product_evidence_text(item: dict, filename: str) -> str:
    parts = [
        filename,
        item.get("file_name"),
        item.get("name"),
        item.get("product"),
    ]
    return " ".join(_clean(part) for part in parts if part is not None)


def _match_products(text: str) -> list[dict[str, Any]]:
    normalized = _normalize_text(text)
    candidates: list[tuple[int, str, str, str]] = []
    for category, products in ((MEDICAL, MEDICAL_PRODUCTS), (HOME, HOME_PRODUCTS)):
        for product in products:
            if _has_token(normalized, _normalize_text(product)):
                candidates.append((len(_normalize_text(product)), category, product, product))
    for alias, product in PRODUCT_ALIASES.items():
        if _has_token(normalized, _normalize_text(alias)):
            category = HOME if product in HOME_PRODUCTS else MEDICAL
            candidates.append((len(_normalize_text(alias)), category, alias, product))
    if not candidates:
        return []
    by_canonical: dict[str, tuple[int, str, str, str]] = {}
    for row in sorted(candidates, reverse=True):
        _length, _category, _matched, canonical = row
        by_canonical.setdefault(canonical, row)
    evidence = []
    for _length, category, matched, canonical in by_canonical.values():
        detail = f"产品目录精确命中：{matched}"
        if canonical != matched:
            detail += f"（按{canonical}处理）"
        evidence.append(
            _evidence(
                category,
                SOURCE_CATALOG,
                detail,
                {"matched": matched, "canonical_product": canonical},
            )
        )
    return evidence


def _normalize_text(text: str) -> str:
    value = _clean(text).translate(str.maketrans({"（": "(", "）": ")"})).upper()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*([()&])\s*", r"\1", value)
    return value


def _has_token(text: str, token: str) -> bool:
    if not token:
        return False
    for match in re.finditer(re.escape(token), text):
        prev_char = text[match.start() - 1] if match.start() else ""
        next_char = text[match.end()] if match.end() < len(text) else ""
        if _continues_token(prev_char, token, left=True):
            continue
        if _continues_token(next_char, token, left=False):
            continue
        return True
    return False


def _continues_token(char: str, token: str, left: bool) -> bool:
    if not char:
        return False
    if not char.isalnum():
        return False
    if char.isdigit():
        return True
    if char.isalpha() and char.isascii():
        return True
    return False


def _filename_evidence(filename: str) -> dict[str, Any] | None:
    name = _clean(filename)
    if not name:
        return None
    if NON_MEDICAL_RE.search(name):
        return _evidence(HOME, SOURCE_FILENAME, "文件名明确包含非医疗/非医用", {"filename": name})
    if HOME_RE.search(name) and not MEDICAL_RE.search(name):
        return _evidence(HOME, SOURCE_FILENAME, "文件名明确包含家电标志", {"filename": name})
    if MEDICAL_RE.search(name):
        return _evidence(MEDICAL, SOURCE_FILENAME, "文件名明确包含医疗标志", {"filename": name})
    return None


def _resolve(evidence: list[dict[str, Any]]) -> tuple[str, str, bool]:
    explicit = [row for row in evidence if row.get("category") in {MEDICAL, HOME}]
    categories = {row["category"] for row in explicit}
    if not categories:
        return PENDING, "未确认", False
    if len(categories) > 1:
        return PENDING, "规则依据冲突，待核对", True
    category = next(iter(categories))
    source = explicit[0].get("source") or "规则判断"
    return category, source, False


def _evidence(category: str, source: str, detail: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"category": category, "source": source, "detail": detail, "data": data}
