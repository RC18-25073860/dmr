"""Offline backfill for finished-goods BOM filename-date versioning."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any

from backend.service import clean_text, compare_versions, json_dumps, json_loads, now_iso, parse_file


RULE_CHANGE = "V1.0 → YYYYMMDD"
MIGRATION_SOURCE = "BOM-DATE-RULE-20260921"


def migrate_bom_dates(store) -> dict[str, Any]:
    """Backfill existing local rows after BOM date-version parsing is generalized.

    The migration is explicit/offline by design: callers pass a DMRStore that
    points at an isolated data directory or a deliberately selected local DB.
    It never creates or deletes files, and blocks an entire record/item group
    when any member cannot be proven to be the same BOM document under the new
    filename-date rule.
    """

    report: dict[str, Any] = {
        "rule_change": RULE_CHANGE,
        "files_changed": 0,
        "records_changed": 0,
        "batch_items_changed": 0,
        "exceptions_resolved": 0,
        "logs_added": 0,
        "blocked": [],
        "unchanged": [],
        "records": [],
        "batch_items": [],
        "_blocked_record_ids": set(),
        "_blocked_identities": set(),
    }

    file_groups = _plan_file_groups(store, report)

    with store.lock, store.conn.immediate():
        changed_records = _apply_file_groups(store, file_groups, report)
        item_groups = _plan_batch_item_groups(store, report, committed=False)
        committed_item_groups = _plan_batch_item_groups(store, report, committed=True)
        if not file_groups and not item_groups and not committed_item_groups:
            return _public_report(report)
        _apply_batch_item_groups(store, item_groups, report)
        _apply_batch_item_groups(store, committed_item_groups, report)
        _refresh_batch_summaries(store, {plan["row"]["batch_id"] for plans in item_groups.values() for plan in plans})
        for record_id in changed_records:
            row = store.execute("SELECT business_code,version,previous FROM records WHERE id=?", (record_id,)).fetchone()
            if row:
                report["records"].append({"id": record_id, "code": row["business_code"], "version": row["version"], "previous": row["previous"] or ""})
    return _public_report(report)


def _plan_file_groups(store, report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows = store.execute(
        """SELECT files.*, records.business_code AS record_code, records.registrant AS record_registrant,
                  records.product AS record_product, records.module AS record_module
           FROM files JOIN records ON records.id=files.record_id
           WHERE records.module='BOM'
           ORDER BY files.record_id, files.created_at, files.id"""
    ).fetchall()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["record_id"]].append(dict(row))

    planned: dict[str, list[dict[str, Any]]] = {}
    for record_id, group in groups.items():
        plans: list[dict[str, Any]] = []
        block_reason = ""
        saw_non_date_bom = False
        for row in group:
            parsed, reason = _reparse_file_row(store, row)
            if reason == "not-date-rule":
                saw_non_date_bom = True
                continue
            if reason:
                block_reason = f"{row['name']}：{reason}"
                break
            existing_meta = json_loads(row["metadata"], {})
            parsed_meta = parsed["metadata"]
            new_version = clean_text(parsed["version"])
            old_version = clean_text(row["version"])
            new_meta = _merged_metadata(existing_meta, parsed_meta)
            if old_version == new_version and existing_meta.get("version_rule") == "filename_date":
                report["unchanged"].append({"kind": "file", "id": row["id"], "name": row["name"], "version": old_version})
                continue
            plans.append({"row": row, "parsed": parsed, "version": new_version, "metadata": new_meta, "old_version": old_version})
        if block_reason:
            _block_record_group(report, record_id, group, block_reason)
            continue
        if plans:
            if saw_non_date_bom:
                _block_record_group(report, record_id, group, "同一BOM记录同时存在日期版本文件和非日期版本文件，需人工确认后再迁移")
                continue
            date_error = _date_group_error(group, plans)
            if date_error:
                _block_record_group(report, record_id, group, date_error)
                continue
            planned[record_id] = plans
    return planned


def _plan_batch_item_groups(store, report: dict[str, Any], *, committed: bool) -> dict[str, list[dict[str, Any]]]:
    where = "status='committed'" if committed else "status!='committed'"
    rows = store.execute(
        f"""SELECT * FROM batch_items
           WHERE {where}
           ORDER BY batch_id, created_at, id"""
    ).fetchall()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        metadata = json_loads(item["metadata"], {})
        blob_path = metadata.get("blob_path")
        if not blob_path:
            continue
        groups[item["batch_id"]].append(item)

    planned: dict[str, list[dict[str, Any]]] = {}
    for batch_id, group in groups.items():
        plans: list[dict[str, Any]] = []
        block_reason = ""
        for item in group:
            parsed, reason = _reparse_batch_item(store, item)
            if reason == "not-date-rule":
                continue
            if reason:
                block_reason = f"{item['file_name']}：{reason}"
                break
            old_meta = json_loads(item["metadata"], {})
            parsed = _preserve_manual_item_fields(item, parsed)
            if _is_related_to_blocked_record(report, item, parsed):
                report["unchanged"].append(
                    {
                        "kind": "batch_item_blocked_by_record",
                        "id": item["id"],
                        "batch_id": item["batch_id"],
                        "version": item["version"],
                        "status": item["status"],
                    }
                )
                continue
            if committed:
                plan = {
                    "row": item,
                    "parsed": parsed,
                    "version": parsed["version"],
                    "metadata": _merged_metadata(old_meta, parsed["metadata"]),
                    "committed": True,
                }
                if _committed_batch_item_needs_update(item, plan):
                    plans.append(plan)
                else:
                    report["unchanged"].append({"kind": "committed_batch_item", "id": item["id"], "version": item["version"]})
                continue
            status, record_id, recommendation, current = store.classify_item(parsed, item["sha256"])
            plan = {
                "row": item,
                "parsed": parsed,
                "version": parsed["version"],
                "metadata": _merged_metadata(old_meta, parsed["metadata"]),
                "status": status,
                "record_id": record_id,
                "recommendation": recommendation,
                "current": current,
                "committed": False,
            }
            if _active_batch_item_needs_update(item, plan):
                plans.append(plan)
            else:
                report["unchanged"].append({"kind": "batch_item", "id": item["id"], "version": item["version"], "status": item["status"]})
        if block_reason:
            report["blocked"].append({"kind": "batch", "batch_id": batch_id, "reason": block_reason})
            continue
        if plans:
            planned[batch_id] = plans
    return planned


def _public_report(report: dict[str, Any]) -> dict[str, Any]:
    report.pop("_blocked_record_ids", None)
    report.pop("_blocked_identities", None)
    return report


def _block_record_group(report: dict[str, Any], record_id: str, group: list[dict[str, Any]], reason: str) -> None:
    identities = {_identity_from_row(row) for row in group}
    identities.discard(("", "", "", ""))
    report["blocked"].append({"kind": "record", "record_id": record_id, "reason": reason, "identities": [list(identity) for identity in sorted(identities)]})
    report["_blocked_record_ids"].add(record_id)
    report["_blocked_identities"].update(identities)


def _identity_from_row(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        clean_text(row.get("record_code") or row.get("code") or row.get("business_code")),
        clean_text(row.get("record_registrant") or row.get("registrant")),
        clean_text(row.get("record_product") or row.get("product")),
        clean_text(row.get("record_module") or row.get("module")),
    )


def _identity_from_item_or_parsed(item: dict[str, Any], parsed: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        clean_text(item.get("code") or parsed.get("code")),
        clean_text(item.get("registrant") or parsed.get("registrant")),
        clean_text(item.get("product") or parsed.get("product")),
        clean_text(item.get("module") or parsed.get("module")),
    )


def _identity_from_parsed(parsed: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        clean_text(parsed.get("code")),
        clean_text(parsed.get("registrant")),
        clean_text(parsed.get("product")),
        clean_text(parsed.get("module")),
    )


def _is_related_to_blocked_record(report: dict[str, Any], item: dict[str, Any], parsed: dict[str, Any]) -> bool:
    if clean_text(item.get("record_id")) in report["_blocked_record_ids"]:
        return True
    identities = report["_blocked_identities"]
    return _identity_from_item_or_parsed(item, parsed) in identities or _identity_from_parsed(parsed) in identities


def _reparse_file_row(store, row: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    try:
        data = store.read_blob(row["blob_path"])
    except FileNotFoundError:
        return None, "原始附件缺失，不能确认版本规则"
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        return None, "原始附件SHA-256与入库记录不一致"
    parsed = parse_file(data, row["name"])
    metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
    if metadata.get("parse_error"):
        return None, f"当前解析器报错：{metadata.get('parse_error')}"
    if metadata.get("version_rule") != "filename_date":
        if _looks_like_filename_date_bom(row["name"], row.get("record_module")):
            return None, "文件名像量产成品BOM日期版本，但当前解析器无法确认"
        return None, "not-date-rule"
    return _validate_parsed_against_scope(parsed, row, filename_key="name")


def _reparse_batch_item(store, item: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    metadata = json_loads(item["metadata"], {})
    try:
        data = store.read_blob(metadata["blob_path"])
    except (FileNotFoundError, KeyError):
        return None, "暂存附件缺失，不能确认版本规则"
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        return None, "暂存附件SHA-256与批次记录不一致"
    parsed = parse_file(data, item["file_name"])
    parsed_meta = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
    if parsed_meta.get("parse_error"):
        return None, f"当前解析器报错：{parsed_meta.get('parse_error')}"
    if parsed_meta.get("version_rule") != "filename_date":
        if _looks_like_filename_date_bom(item["file_name"], item.get("module")):
            return None, "文件名像量产成品BOM日期版本，但当前解析器无法确认"
        return None, "not-date-rule"
    return _validate_parsed_against_scope(parsed, item, filename_key="file_name", allow_empty_record=True)


def _validate_parsed_against_scope(parsed: dict[str, Any], row: dict[str, Any], *, filename_key: str, allow_empty_record: bool = False) -> tuple[dict[str, Any] | None, str]:
    metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
    date = clean_text(metadata.get("version_date"))
    if not re.fullmatch(r"20\d{6}", date) or compare_versions(date, date) is None:
        return None, "文件名日期不是有效YYYYMMDD"
    source_code = clean_text(metadata.get("source_bom_code") or metadata.get("root_row", {}).get("父项编码"))
    filename_code = clean_text(metadata.get("filename_bom_code"))
    if clean_text(parsed.get("module")) != "BOM":
        return None, "重新解析后不是BOM模块"
    if clean_text(parsed.get("code")) != source_code:
        return None, "重新解析编号与BOM根编号不一致"
    if filename_code and source_code and filename_code != source_code:
        return None, f"文件名编号{filename_code}与BOM根编号{source_code}不一致"

    expected = {
        "code": row.get("record_code") or row.get("code"),
        "registrant": row.get("record_registrant") or row.get("registrant"),
        "product": row.get("record_product") or row.get("product"),
        "module": row.get("record_module") or row.get("module"),
    }
    for parsed_key, expected_value in expected.items():
        expected_text = clean_text(expected_value)
        if allow_empty_record and parsed_key in {"code", "registrant", "product", "module"} and expected_text in {"", "待确认"}:
            continue
        if expected_text and clean_text(parsed.get(parsed_key)) != expected_text:
            return None, f"重新解析{parsed_key}={clean_text(parsed.get(parsed_key)) or '空'}，与现有记录{expected_text}不一致"
    if source_code and clean_text(expected.get("code")) and source_code != clean_text(expected.get("code")):
        return None, "source_bom_code与现有文件编号不一致"
    parsed["fileName"] = row[filename_key]
    return parsed, ""


def _looks_like_filename_date_bom(name: str, module: str | None) -> bool:
    return clean_text(module) == "BOM" and re.search(r"(?:^|[-_\s])20\d{6}\.[^.]+$", clean_text(name)) is not None


def _merged_metadata(existing: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    preserved = {key: existing[key] for key in ("blob_path", "original_blob_path", "document_version") if key in existing}
    merged.update(parsed)
    merged.update(preserved)
    if "document_version" not in merged and parsed.get("root_bom_version"):
        merged["document_version"] = parsed["root_bom_version"]
    return merged


def _preserve_manual_item_fields(item: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(parsed)
    category = clean_text(item.get("project_category"))
    source = clean_text(item.get("project_category_source"))
    if category in {"医疗", "家电"} or source in {"人工核对", "人工确认，非企业审批"}:
        parsed["project_category"] = category
        parsed["project_category_source"] = source or "人工核对"
    for key in ("linked_bom", "notes", "type"):
        if clean_text(item.get(key)) and clean_text(item.get(key)) != "待确认":
            parsed[key] = item[key]
    return parsed


def _active_batch_item_needs_update(item: dict[str, Any], plan: dict[str, Any]) -> bool:
    parsed = plan["parsed"]
    checks = {
        "code": parsed.get("code") or item["code"],
        "registrant": parsed.get("registrant") or item["registrant"],
        "type": parsed.get("type") or item["type"],
        "project_category": parsed.get("project_category") or item["project_category"],
        "project_category_source": parsed.get("project_category_source") or item["project_category_source"],
        "product": parsed.get("product") or item["product"],
        "module": parsed.get("module") or item["module"],
        "version": plan["version"],
        "linked_bom": parsed.get("linked_bom") or item["linked_bom"],
        "current_version": plan["current"],
        "detected_version": plan["version"],
        "status": plan["status"],
        "recommendation": plan["recommendation"],
        "record_id": plan["record_id"],
    }
    for key, value in checks.items():
        if clean_text(item.get(key)) != clean_text(value):
            return True
    return json_loads(item["metadata"], {}) != plan["metadata"]


def _committed_batch_item_needs_update(item: dict[str, Any], plan: dict[str, Any]) -> bool:
    if clean_text(item.get("version")) != clean_text(plan["version"]):
        return True
    if clean_text(item.get("detected_version")) != clean_text(plan["version"]):
        return True
    return json_loads(item["metadata"], {}) != plan["metadata"]


def _date_group_error(group: list[dict[str, Any]], plans: list[dict[str, Any]]) -> str:
    versions: dict[str, set[str]] = defaultdict(set)
    current_by_file = {plan["row"]["id"]: plan["version"] for plan in plans}
    for row in group:
        version = current_by_file.get(row["id"], clean_text(row["version"]))
        if re.fullmatch(r"20\d{6}", version):
            versions[version].add(row["sha256"])
    for version, hashes in versions.items():
        if len(hashes) > 1:
            return f"同一日期版本 {version} 存在不同文件内容，整组保留人工确认"
    return ""


def _apply_file_groups(store, file_groups: dict[str, list[dict[str, Any]]], report: dict[str, Any]) -> set[str]:
    changed_records: set[str] = set()
    for record_id, plans in file_groups.items():
        for plan in plans:
            row = plan["row"]
            store.execute("UPDATE files SET version=?,metadata=? WHERE id=?", (plan["version"], json_dumps(plan["metadata"]), row["id"]))
            report["files_changed"] += 1
        versions = _refresh_record_versions(store, record_id)
        if versions:
            changed_records.add(record_id)
            report["records_changed"] += 1
            changed = ", ".join(f"{plan['old_version']} → {plan['version']}" for plan in plans)
            store.add_log(
                "BOM日期版本规则回填",
                versions["code"],
                f"规则变更：{RULE_CHANGE}；文件版本回填：{changed}；当前有效：{versions['before']} → {versions['current']}，上一版本：{versions['previous'] or '无'}。",
                MIGRATION_SOURCE,
                "成功",
                user="本地规则迁移",
            )
            report["logs_added"] += 1
    return changed_records


def _refresh_record_versions(store, record_id: str) -> dict[str, str] | None:
    record = store.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    files = store.execute("SELECT * FROM files WHERE record_id=? ORDER BY version,created_at,id", (record_id,)).fetchall()
    dated = [dict(row) for row in files if re.fullmatch(r"20\d{6}", clean_text(row["version"])) and compare_versions(row["version"], row["version"]) is not None]
    if not record or not dated:
        return None
    dated.sort(key=lambda row: (row["version"], row["created_at"] or "", row["id"]))
    current = dated[-1]
    previous_rows = [row for row in dated if row["version"] != current["version"]]
    previous = previous_rows[-1]["version"] if previous_rows else ""
    for row in dated:
        status = "当前有效" if row["id"] == current["id"] else "历史版本"
        store.execute("UPDATE files SET status=? WHERE id=?", (status, row["id"]))
    current_meta = json_loads(current["metadata"], {})
    store.execute(
        """UPDATE records SET version=?,previous=?,file_name=?,date=?,controlled=?,source=?,size=?,linked_bom=?,metadata=?,updated_at=?
           WHERE id=?""",
        (
            current["version"],
            previous,
            current["name"],
            record["date"] or "",
            record["controlled"] or "",
            current["source"] or record["source"] or "",
            current["size"] or record["size"] or "",
            current["linked_bom"] or record["linked_bom"] or "",
            json_dumps(_merged_metadata(json_loads(record["metadata"], {}), current_meta)),
            now_iso(),
            record_id,
        ),
    )
    return {"code": record["business_code"], "before": record["version"], "current": current["version"], "previous": previous}


def _apply_batch_item_groups(store, item_groups: dict[str, list[dict[str, Any]]], report: dict[str, Any]) -> None:
    for _batch_id, plans in item_groups.items():
        for plan in plans:
            row = plan["row"]
            parsed = plan["parsed"]
            if plan.get("committed"):
                store.execute(
                    "UPDATE batch_items SET version=?,detected_version=?,metadata=?,updated_at=? WHERE id=?",
                    (plan["version"], plan["version"], json_dumps(plan["metadata"]), now_iso(), row["id"]),
                )
                report["batch_items_changed"] += 1
                report["batch_items"].append({"id": row["id"], "batch_id": row["batch_id"], "version": plan["version"], "status": "committed"})
                continue
            store.execute(
                """UPDATE batch_items
                   SET code=?,registrant=?,type=?,project_category=?,project_category_source=?,product=?,module=?,
                       version=?,linked_bom=?,notes=?,metadata=?,current_version=?,detected_version=?,status=?,
                       recommendation=?,record_id=?,updated_at=?
                   WHERE id=?""",
                (
                    parsed.get("code") or row["code"],
                    parsed.get("registrant") or row["registrant"],
                    parsed.get("type") or row["type"],
                    parsed.get("project_category") or row["project_category"],
                    parsed.get("project_category_source") or row["project_category_source"],
                    parsed.get("product") or row["product"],
                    parsed.get("module") or row["module"],
                    plan["version"],
                    parsed.get("linked_bom") or row["linked_bom"],
                    parsed.get("notes") if isinstance(parsed.get("notes"), str) else "；".join(parsed.get("notes", [])),
                    json_dumps(plan["metadata"]),
                    plan["current"],
                    plan["version"],
                    plan["status"],
                    plan["recommendation"],
                    plan["record_id"],
                    now_iso(),
                    row["id"],
                ),
            )
            report["batch_items_changed"] += 1
            report["batch_items"].append({"id": row["id"], "batch_id": row["batch_id"], "version": plan["version"], "status": plan["status"]})
            if plan["status"] in {"matched", "duplicate"}:
                resolved = store.execute(
                    """UPDATE exceptions SET status='resolved',resolved_at=?,action=?
                       WHERE item_id=? AND status='open' AND type='版本冲突'
                       AND (detail LIKE '%同版本%' OR detail LIKE '%版本%' OR detail LIKE '%V1.0%')""",
                    (now_iso(), f"按量产成品BOM文件名日期规则回填版本（{RULE_CHANGE}），旧版本冲突已消除；仍需按批次提交归档。", row["id"]),
                )
                report["exceptions_resolved"] += max(0, getattr(resolved, "rowcount", 0))


def _refresh_batch_summaries(store, batch_ids: set[str]) -> None:
    for batch_id in sorted(batch_ids):
        rows = store.execute("SELECT status FROM batch_items WHERE batch_id=?", (batch_id,)).fetchall()
        statuses = [row["status"] for row in rows]
        summary = {
            "total": len(statuses),
            "matched": statuses.count("matched"),
            "conflict": statuses.count("conflict"),
            "unmatched": statuses.count("unmatched"),
            "duplicate": statuses.count("duplicate"),
        }
        store.execute("UPDATE batches SET status='analyzed',summary=?,updated_at=? WHERE id=? AND status!='committed'", (json_dumps(summary), now_iso(), batch_id))
