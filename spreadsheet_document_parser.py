from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Sequence


VersionScope = Literal["software", "document"]
DecisionStatus = Literal["found", "pending", "missing"]


SOFTWARE_VERSION_LABELS = (
    "软件版本",
    "固件版本",
    "完整版本",
    "发布版本",
    "提测版本",
    "程序版本",
    "firmware version",
    "software version",
)
DOCUMENT_VERSION_LABELS = (
    "文档版本",
    "报告版本",
    "报表版本",
    "版本",
    "report version",
    "document version",
)
NON_VERSION_LABEL_MARKERS = (
    "测试名称",
    "测试编号",
    "测试目的",
    "硬件版本",
    "修订内容",
    "修订人",
    "更新履历",
    "版本信息",
    "页数",
    "日期",
    "date",
    "page",
)


@dataclass(frozen=True)
class SpreadsheetVersionCandidate:
    version: str
    raw_value: str
    label: str
    scope: VersionScope
    sheet_name: str
    row: int
    column: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "raw_value": self.raw_value,
            "label": self.label,
            "scope": self.scope,
            "sheet_name": self.sheet_name,
            "row": self.row,
            "column": self.column,
        }


def extract_spreadsheet_document_version(
    sheets: Sequence[tuple[str, Sequence[Sequence[str]]]] | None = None,
    rows: Sequence[Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Extract explicitly labeled document/software versions from spreadsheet rows.

    The function only trusts labels such as ``固件版本`` or ``版本 / Report Version``.
    When multiple distinct candidates exist in the selected scope, it returns
    ``pending`` instead of guessing the newest-looking value.
    """
    candidates = find_spreadsheet_version_candidates(sheets=sheets, rows=rows)
    by_scope: dict[VersionScope, list[SpreadsheetVersionCandidate]] = {
        "software": [candidate for candidate in candidates if candidate.scope == "software"],
        "document": [candidate for candidate in candidates if candidate.scope == "document"],
    }

    selected_scope: VersionScope | None = None
    if by_scope["software"]:
        selected_scope = "software"
    elif by_scope["document"]:
        selected_scope = "document"

    if selected_scope is None:
        version_candidates = [candidate.as_dict() for candidate in candidates]
        return {
            "status": "missing",
            "version": "",
            "scope": "",
            "version_evidence": None,
            "version_candidates": version_candidates,
            "candidates": version_candidates,
            "notes": ["未找到明确标签的软件或文档版本。"],
        }

    selected = by_scope[selected_scope]
    distinct_versions = sorted({candidate.version for candidate in selected if candidate.version})
    if len(distinct_versions) == 1:
        winner = next(candidate for candidate in selected if candidate.version == distinct_versions[0])
        version_evidence = winner.as_dict()
        version_candidates = [candidate.as_dict() for candidate in candidates]
        return {
            "status": "found",
            "version": winner.version,
            "scope": selected_scope,
            "version_evidence": version_evidence,
            "version_candidates": version_candidates,
            "candidate": version_evidence,
            "candidates": version_candidates,
            "notes": [],
        }

    version_candidates = [candidate.as_dict() for candidate in candidates]
    return {
        "status": "pending",
        "version": "",
        "scope": selected_scope,
        "version_evidence": None,
        "version_candidates": version_candidates,
        "candidates": version_candidates,
        "notes": [f"发现多个明确标注的{_scope_label(selected_scope)}版本，未自动选择。"],
    }


def find_spreadsheet_version_candidates(
    sheets: Sequence[tuple[str, Sequence[Sequence[str]]]] | None = None,
    rows: Sequence[Sequence[str]] | None = None,
) -> list[SpreadsheetVersionCandidate]:
    if sheets is None:
        if rows is None:
            raise ValueError("sheets or rows is required")
        sheets = [("", rows)]

    candidates: list[SpreadsheetVersionCandidate] = []
    for sheet_name, sheet_rows in sheets:
        for row_index, row in enumerate(sheet_rows, start=1):
            cells = [str(value or "").strip() for value in row]
            for column_index, label in enumerate(cells):
                scope = _label_scope(label)
                if scope is None:
                    continue
                raw_value = _value_for_label(cells, column_index, scope)
                version = _version_from_text(raw_value, scope)
                if not version:
                    continue
                candidates.append(
                    SpreadsheetVersionCandidate(
                        version=version,
                        raw_value=raw_value,
                        label=label,
                        scope=scope,
                        sheet_name=sheet_name,
                        row=row_index,
                        column=column_index + 1,
                    )
                )
    return _dedupe_candidates(candidates)


def is_software_history_version_row(rowmap: dict[str, str]) -> bool:
    """Return True only for actual software history rows, not appendix/BOM rows."""
    name = (rowmap.get("软件名字") or rowmap.get("软件名称") or "").strip()
    version = (
        rowmap.get("完整版本")
        or rowmap.get("软件版本")
        or rowmap.get("发布版本")
        or ""
    ).strip()
    if not (name and version):
        return False
    if _version_from_text(version, "software") != _normalize_version(version):
        return False
    if re.search(r"\.(?:hex|socprj|bin|zip|v\d+[a-z0-9]*config)\b", name, re.I):
        return True
    return bool(re.search(r"[A-Z0-9_ -]+[VA]\d+(?:\.\d+)+", name, re.I))


def _label_scope(label: str) -> VersionScope | None:
    normalized = _normalized_text(label)
    if not normalized:
        return None
    if any(marker in normalized for marker in (_normalized_text(x) for x in NON_VERSION_LABEL_MARKERS)):
        return None
    if _normalize_version(label):
        return None
    if any(marker in normalized for marker in (_normalized_text(x) for x in SOFTWARE_VERSION_LABELS)):
        return "software"
    if (
        normalized in {"版本", "version"}
        or any(marker in normalized for marker in (_normalized_text(x) for x in DOCUMENT_VERSION_LABELS if x != "版本"))
    ):
        return "document"
    return None


def _value_for_label(cells: Sequence[str], label_index: int, scope: VersionScope) -> str:
    inline_version = _version_from_text(cells[label_index], scope)
    if inline_version and cells[label_index] != inline_version:
        return cells[label_index]

    for value in cells[label_index + 1 : label_index + 4]:
        value = value.strip()
        if not value:
            continue
        if _label_scope(value) is not None:
            continue
        return value
    return ""


def _version_from_text(text: str, scope: VersionScope) -> str:
    text = (text or "").strip()
    if scope == "document":
        match = re.search(r"(?<![A-Za-z0-9])(?:Ver\.?|Version)?\s*([VA]?\d+(?:\.\d+)+)(?![A-Za-z0-9])", text, re.I)
        if match:
            value = match.group(1)
            return value.upper() if value[:1].upper() in {"V", "A"} else f"V{value}"
    match = re.search(r"(?<![A-Za-z0-9])([VA]\d+(?:\.\d+)+)(?![A-Za-z0-9])", text, re.I)
    return match.group(1).upper() if match else ""


def _normalize_version(text: str) -> str:
    match = re.search(r"(?<![A-Za-z0-9])([VA]\d+(?:\.\d+)+)(?![A-Za-z0-9])", text or "", re.I)
    return match.group(1).upper() if match else ""


def _dedupe_candidates(candidates: Iterable[SpreadsheetVersionCandidate]) -> list[SpreadsheetVersionCandidate]:
    seen: set[tuple[str, str, str, str]] = set()
    result: list[SpreadsheetVersionCandidate] = []
    for candidate in candidates:
        key = (candidate.scope, candidate.version, candidate.sheet_name, candidate.label)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").casefold())


def _scope_label(scope: VersionScope) -> str:
    return "软件" if scope == "software" else "文档"
