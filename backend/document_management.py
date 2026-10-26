"""Document-control routing rules for catalog update reminders.

The platform stores files locally, but Feishu catalog/register updates remain a
human-controlled step.  This module decides what kind of human target should be
maintained for a file without claiming that every file is a direct row in a
mass-production DMR list.
"""
from __future__ import annotations

import json
import re
from typing import Any


DIRECT_DMR_TARGET = {
    "target": "production_dmr_list",
    "target_label": "DMR 清单",
    "direct_catalog_row": True,
    "catalog_locator_enabled": True,
}


INDIRECT_RULES = {
    "结构图纸": {
        "target": "structural_drawing_register",
        "target_label": "结构图纸台账",
        "instruction": "结构图纸按图纸台账维护。平台只提示相关产品和候选资料，不在量产 DMR 清单中自动定位或建议新增行。",
    },
    "包装图纸": {
        "target": "packaging_drawing_register",
        "target_label": "包装图纸台账",
        "instruction": "包装图纸按包装图纸台账维护。请按物料号、主体和图纸版本人工核对，平台不把它当作量产 DMR 清单直接行。",
    },
    "承认书&规格书": {
        "target": "ack_spec_register",
        "target_label": "承认书及规格书台账",
        "instruction": "承认书和规格书按物料台账维护。可通过 BOM 反查适用产品，但不能据此自动确认已经命中某一份 DMR 清单条目。",
    },
}


KIND_RULES = {
    "bom_notice": {
        "target": "production_dmr_list",
        "target_label": "DMR 清单（发放通知线索）",
        "instruction": "这是BOM发放通知，并非BOM原始表格。请核对通知中的BOM编号、版本和对应附件后，人工维护相关DMR清单；不要将通知本身当成BOM版本。",
        "update_kind": "bom_notice_review",
    },
    "structural_drawing": {**INDIRECT_RULES["结构图纸"], "update_kind": "register_review"},
    "packaging_drawing": {**INDIRECT_RULES["包装图纸"], "update_kind": "register_review"},
    "acknowledgement": {**INDIRECT_RULES["承认书&规格书"], "update_kind": "register_review"},
    "specification": {**INDIRECT_RULES["承认书&规格书"], "update_kind": "register_review"},
    "incoming_inspection": {
        "target": "general_document_register",
        "target_label": "通用文件台账",
        "instruction": "来料/进料检验类文件按通用文件台账维护。请人工确认台账位置；平台不在量产 DMR 清单中自动定位或新增行。",
        "update_kind": "general_register_review",
    },
    "hardware": {
        "target": "hardware_register",
        "target_label": "硬件资料台账",
        "instruction": "硬件资料按硬件资料台账维护。请人工确认适用产品、硬件版本和受控范围；平台不在量产 DMR 清单中自动定位或新增行。",
        "update_kind": "hardware_register_review",
    },
    "ecn": {
        "target": "ecn_register",
        "target_label": "ECN 变更台账",
        "instruction": "ECN 资料按变更台账维护。请人工核对关联 BOM、变更编号和生效范围；平台不自动推断量产 DMR 清单区域。",
        "update_kind": "ecn_register_review",
    },
}


SOFTWARE_REGISTER_RULE = {
    "target": "software_release_register",
    "target_label": "软件履历及发布关系",
    "instruction": "软件履历/发布资料用于核对固件、配置文件、适用 BOM 和发布关系；不直接定位为量产 DMR 清单中的某一行。",
    "update_kind": "software_release_review",
}


INCOMING_INSPECTION_RE = re.compile(r"(来料|进料|来件|IQC|Incoming\s*Inspection|Incoming)", re.I)
SOFTWARE_HISTORY_KINDS = {"software_history"}
SOFTWARE_REGISTER_KINDS = {
    "software_history",
    "software_release",
    "software_release_record",
    "software_version_history",
    "history",
    "release",
}
BOM_KIND = {"bom"}


def json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def catalog_update_policy(record: Any, file: Any | None = None) -> dict[str, Any]:
    """Return UI/API metadata for the human maintenance target.

    `record` and `file` may be sqlite rows or dict-like objects.  The result is
    descriptive only; callers must still keep Feishu operations read-only.
    """
    record_meta = json_dict(_get(record, "metadata"))
    file_meta = json_dict(_get(file, "metadata")) if file is not None else {}
    metadata = {**record_meta, **file_meta}
    module = _text(_get(record, "module") or _get(file, "module"))
    filename = _text(_get(file, "name") or _get(record, "file_name") or metadata.get("source_filename"))
    kind = _text(metadata.get("kind"))
    management_kind = _text(metadata.get("management_kind") or kind)
    code = _text(_get(record, "business_code") or _get(record, "code"))
    linked_bom = _text(_get(file, "linked_bom") or _get(record, "linked_bom") or metadata.get("linked_bom"))

    if module in INDIRECT_RULES:
        base = _indirect_base(INDIRECT_RULES[module], "register_review")
    elif management_kind in SOFTWARE_REGISTER_KINDS:
        base = _indirect_base(
            SOFTWARE_REGISTER_RULE,
            "software_history_applicability_review" if management_kind in SOFTWARE_HISTORY_KINDS else "software_release_review",
        )
        if management_kind in SOFTWARE_HISTORY_KINDS:
            base["instruction"] = "软件履历表用于核对适用 BOM、固件/配置文件版本和发布关系；履历本身更新不等同于所有程序自动升级。"
    elif management_kind in KIND_RULES:
        rule = KIND_RULES[management_kind]
        base = _indirect_base(rule, rule["update_kind"])
    elif module == "检验文件" and INCOMING_INSPECTION_RE.search(filename):
        base = _indirect_base(KIND_RULES["incoming_inspection"], "general_register_review")
    else:
        base = {
            **DIRECT_DMR_TARGET,
            "instruction": "请打开对应 DMR 清单，按类别、编号或名称核对条目；平台只提示位置和新旧版本，不直接写入飞书。",
            "update_kind": "dmr_row_review",
        }

    if base["direct_catalog_row"] and (module == "BOM" or kind in BOM_KIND):
        base["update_kind"] = "bom_version_review"
        base["instruction"] = "请打开对应 DMR 清单，核对 BOM 编号、版本日期/版本号和 ECN 依据；平台只提示，不直接写入飞书。"
    elif base["direct_catalog_row"] and module == "软件":
        base["update_kind"] = "software_attachment_review"
        base["instruction"] = "请结合软件履历表核对该软件附件的版本和适用 BOM；同批次只能作为线索，不能自动覆盖其他配套文件版本。"

    evidence = relation_evidence(metadata, code=code, linked_bom=linked_bom)
    base["relation_evidence"] = evidence
    base["relation_candidates"] = relation_candidates(metadata, code=code, linked_bom=linked_bom)
    base["relation_scope"] = "parsed_evidence_only"
    base["catalog_location_hint"] = catalog_location_hint(base, module=module)
    return {
        "management_target": base["target"],
        "management_target_label": base["target_label"],
        "management_instruction": base["instruction"],
        "direct_catalog_row": bool(base["direct_catalog_row"]),
        "catalog_locator_enabled": bool(base["catalog_locator_enabled"]),
        "catalog_location_hint": base["catalog_location_hint"],
        "update_kind": base["update_kind"],
        "relation_evidence": base["relation_evidence"],
        "relation_candidates": base["relation_candidates"],
        "relation_scope": base["relation_scope"],
    }


def _indirect_base(rule: dict[str, Any], update_kind: str) -> dict[str, Any]:
    return {
        "target": rule["target"],
        "target_label": rule["target_label"],
        "instruction": rule["instruction"],
        "direct_catalog_row": False,
        "catalog_locator_enabled": False,
        "update_kind": update_kind,
    }


def catalog_location_hint(policy: dict[str, Any], *, module: str) -> str:
    if not policy.get("direct_catalog_row"):
        return f"{policy['target_label']}：请使用清单链接作为入口，人工核对台账或登记表位置。"
    if module:
        return f"DMR 清单 / {module} 区域：优先按编号定位；编号不一致时仅展示名称候选。"
    return "DMR 清单：先确认资料类别，再按编号或名称定位。"


def relation_evidence(metadata: dict[str, Any], *, code: str = "", linked_bom: str = "") -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for key, label in (
        ("source_bom_code", "解析到的来源 BOM"),
        ("filename_bom_code", "文件名 BOM 编号"),
        ("linked_bom", "文件关联 BOM"),
    ):
        value = _text(metadata.get(key))
        if value:
            evidence.append({"field": key, "label": label, "value": value, "source": "metadata"})
    root = metadata.get("root_row") if isinstance(metadata.get("root_row"), dict) else {}
    root_code = _text(root.get("父项编码"))
    if root_code:
        evidence.append({"field": "root_row.父项编码", "label": "BOM 根行父项编码", "value": root_code, "source": "parsed_bom"})
    for row in metadata.get("versions") if isinstance(metadata.get("versions"), list) else []:
        if isinstance(row, dict) and _text(row.get("linked_bom")):
            evidence.append({
                "field": "versions.linked_bom",
                "label": "软件履历行 BOM",
                "value": _text(row.get("linked_bom")),
                "source": "software_history",
            })
    if linked_bom:
        evidence.append({"field": "linked_bom", "label": "受控文件关联 BOM", "value": linked_bom, "source": "record"})
    if code and metadata.get("version_rule") == "filename_date":
        evidence.append({"field": "business_code", "label": "日期版本 BOM 编号", "value": code, "source": "record"})
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for row in evidence:
        key = (row["field"], row["value"], row["source"])
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return unique


def relation_candidates(metadata: dict[str, Any], *, code: str = "", linked_bom: str = "") -> list[dict[str, str]]:
    values = []
    for value in (linked_bom, metadata.get("source_bom_code"), metadata.get("filename_bom_code"), code if metadata.get("kind") == "bom" else ""):
        text = _text(value)
        if text:
            values.append(text)
    for row in metadata.get("versions") if isinstance(metadata.get("versions"), list) else []:
        if isinstance(row, dict) and _text(row.get("linked_bom")):
            values.append(_text(row.get("linked_bom")))
    result = []
    for value in dict.fromkeys(values):
        result.append({"type": "bom", "code": value, "confidence": "candidate", "basis": "parsed_metadata"})
    return result


def _get(row: Any, key: str) -> Any:
    if row is None:
        return ""
    if isinstance(row, dict):
        return row.get(key, "")
    try:
        return row[key]
    except Exception:
        return getattr(row, key, "")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
