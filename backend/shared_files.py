"""Reuse an archived attachment under another authorized DMR record."""
from __future__ import annotations

import datetime as _dt
import uuid

from fastapi import HTTPException

from backend.editing import edit_token, require_edit_token
from backend.service import (
    clean_text,
    compare_versions,
    dated_bom_metadata,
    dated_bom_policy_error,
    file_type,
    json_dumps,
    json_loads,
    normalize_project_category,
    now_iso,
    sha256_bytes,
    size_label,
    today_text,
)
from backend.subjects import ALLOWED_ARCHIVE_SUBJECTS, validate_new_archive_subject


def _snapshot(store, access):
    records = [dict(row) for row in store.execute("SELECT * FROM records ORDER BY id").fetchall() if access.matches(dict(row))]
    record_ids = {row["id"] for row in records}
    files = [
        dict(row)
        for row in store.execute("SELECT * FROM files ORDER BY id").fetchall()
        if row["record_id"] in record_ids and (row["status"] == "当前有效" or "history" in access.permissions)
    ]
    return {"records": records, "files": files}


def share_details(store, access, source_file_id: str) -> dict:
    access.require("commit")
    access.require("download")
    source_file = dict(access.file(source_file_id))
    source_record = access.record(source_file["record_id"])
    return {
        "file_id": source_file_id,
        "source_file": store.row_file(source_file),
        "source_record": store.row_record(source_record),
        "defaults": {
            "code": source_record["business_code"],
            "version": source_file["version"],
            "file_name": source_file["name"],
            "linked_bom": source_file["linked_bom"] or source_record["linked_bom"] or "",
            "type": source_file["type"] or source_record["type"] or "",
        },
        "allowed_subjects": sorted(ALLOWED_ARCHIVE_SUBJECTS),
        "edit_token": edit_token("file-share", source_file_id, _snapshot(store, access)),
    }


def _reject_identity_overrides(payload: dict) -> None:
    blocked = [key for key in ("version", "file_name", "type", "linked_bom") if key in payload]
    if blocked:
        raise HTTPException(400, "共享归档不支持修改版本、文件名、文件类型或BOM关联")
    if "project_category_source" in payload:
        raise HTTPException(400, "共享归档项目类别来源由系统记录，不能由客户端指定")


def _target_fields(store, access, payload: dict, source_record: dict, source_file: dict) -> tuple[dict, object | None]:
    _reject_identity_overrides(payload)
    source_code = source_record["business_code"]
    source_module = source_record["module"]
    requested_code = clean_text(payload.get("code"), source_code)
    requested_module = clean_text(payload.get("module"), source_module)
    if requested_code != source_code or requested_module != source_module:
        raise HTTPException(400, "共享归档必须沿用来源文件编号和模块")
    target_id = clean_text(payload.get("target_record_id"))
    if target_id:
        target = access.record(target_id)
        validate_new_archive_subject(target["registrant"])
        if target["business_code"] != source_code or target["module"] != source_module:
            raise HTTPException(409, "目标清单必须与来源文件编号和模块一致")
        requested_category = clean_text(payload.get("project_category"))
        if requested_category and normalize_project_category(requested_category) != target["project_category"]:
            raise HTTPException(409, "目标清单项目类别与本次选择不一致")
        target = target | {
            "code": target["business_code"],
            "version": source_file["version"],
            "file_name": source_file["name"],
            "linked_bom": source_file["linked_bom"] or target["linked_bom"] or "",
        }
        return target, target

    required = {
        "code": source_code,
        "module": source_module,
        "registrant": clean_text(payload.get("registrant")),
        "project_category": clean_text(payload.get("project_category")),
        "product": clean_text(payload.get("product")),
    }
    missing = [key for key, value in required.items() if not value or value in {"待确认", "未知"}]
    if missing:
        raise HTTPException(400, "请完整填写目标文件编号、主体、项目类别、机型和模块")
    registrant = validate_new_archive_subject(required["registrant"])
    project_category = normalize_project_category(required["project_category"])
    if project_category == "待确认":
        raise HTTPException(400, "共享归档必须选择医疗或家电项目类别")
    target = {
        "business_code": required["code"],
        "code": required["code"],
        "registrant": registrant,
        "type": clean_text(payload.get("type"), source_record["type"] or source_file["type"] or "待确认"),
        "project_category": project_category,
        "project_category_source": "共享归档",
        "product": required["product"],
        "module": required["module"],
        "version": source_file["version"],
        "file_name": source_file["name"],
        "linked_bom": source_file["linked_bom"] or "",
    }
    access.require_resource(target)
    existing = store.execute(
        "SELECT * FROM records WHERE business_code=? AND registrant=? AND product=? AND module=?",
        (target["business_code"], target["registrant"], target["product"], target["module"]),
    ).fetchone()
    return target, existing


def share_file(store, access, source_file_id: str, payload: dict) -> dict:
    access.require("commit")
    access.require("download")
    with store.lock, store.conn.immediate():
        require_edit_token(payload.get("expected_edit_token"), edit_token("file-share", source_file_id, _snapshot(store, access)))
        reason = clean_text(payload.get("reason"))
        if not reason or len(reason) > 2000:
            raise HTTPException(400, "请填写共享归档原因（1–2000字）")
        source_file = dict(access.file(source_file_id))
        source_record = access.record(source_file["record_id"])
        if clean_text(payload.get("target_record_id")) == source_record["id"]:
            raise HTTPException(400, "目标清单不能与来源清单相同")
        try:
            content = store.read_blob(source_file["blob_path"])
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "来源附件缺失或不可读取，不能共享归档") from exc
        if sha256_bytes(content) != source_file["sha256"]:
            raise HTTPException(409, "来源附件校验失败，不能共享归档")

        target, existing = _target_fields(store, access, payload, source_record, source_file)
        access.require_resource(target)
        project_conflict = store.same_project_category_conflict(target)
        if project_conflict:
            raise HTTPException(409, project_conflict)
        if not target["version"]:
            raise HTTPException(400, "目标版本不能为空")
        if not target["file_name"]:
            raise HTTPException(400, "目标文件名不能为空")
        error = dated_bom_policy_error(source_file | {"code": target["business_code"], "module": target["module"]})
        if error:
            raise HTTPException(409, error)

        target_record = dict(existing) if existing else None
        current_file = None
        if target_record:
            current_file = store.execute(
                "SELECT * FROM files WHERE record_id=? AND status='当前有效' ORDER BY created_at DESC LIMIT 1",
                (target_record["id"],),
            ).fetchone()

        historical = False
        if target_record and current_file:
            same_version_files = store.execute(
                "SELECT * FROM files WHERE record_id=? AND version=?",
                (target_record["id"], target["version"]),
            ).fetchall()
            if same_version_files:
                if all(row["sha256"] == source_file["sha256"] for row in same_version_files):
                    return {
                        "status": "skipped",
                        "message": "目标清单同版本同文件，已跳过不重复归档。",
                        "record_id": target_record["id"],
                        "file_id": next(row["id"] for row in same_version_files if row["sha256"] == source_file["sha256"]),
                        "source_file_id": source_file_id,
                    }
                raise HTTPException(409, "目标清单同版本已有不同文件内容，禁止共享覆盖")
            cmp = compare_versions(target["version"], target_record["version"])
            if cmp is None:
                raise HTTPException(409, f"目标版本 {target['version']} 无法与当前版本 {target_record['version']} 严格比较")
            historical = bool(dated_bom_metadata(source_file)) and cmp < 0
            if cmp < 0 and not historical:
                raise HTTPException(409, f"目标版本低于当前版本 {target_record['version']}，禁止覆盖")

        share_id = f"SHARE-{uuid.uuid4().hex[:12]}"
        store.execute(
            "INSERT INTO batches(id,status,created_at,updated_at,summary,owner_user_id) VALUES(?,?,?,?,?,?)",
            (share_id, "committed", now_iso(), now_iso(), json_dumps({"kind": "shared_file", "source_file_id": source_file_id}), access.user["id"]),
        )
        previous_current_version = target_record["version"] if target_record else ""
        if target_record is None:
            target_record = dict(store.create_record_shell(target | {"metadata": source_file["metadata"] or "{}"}, share_id))
        if current_file and not historical:
            store.execute("UPDATE files SET status='历史版本' WHERE record_id=? AND status='当前有效'", (target_record["id"],))

        metadata = json_loads(source_file["metadata"], {})
        metadata["shared_from"] = {
            "file_id": source_file["id"],
            "record_id": source_record["id"],
            "record_no": source_record["business_code"],
            "registrant": source_record["registrant"],
            "product": source_record["product"],
        }
        file_id = str(uuid.uuid4())
        file_size = source_file["size"] or size_label(len(content))
        store.execute(
            """INSERT INTO files(id,record_id,name,version,status,date,source,size,type,storage,sha256,blob_path,metadata,linked_bom,link_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                file_id,
                target_record["id"],
                target["file_name"],
                target["version"],
                "历史版本" if historical else "当前有效",
                _dt.date.today().isoformat(),
                share_id,
                file_size,
                source_file["type"] or file_type(target["file_name"]),
                source_file["storage"] or "本地保存",
                source_file["sha256"],
                source_file["blob_path"],
                json_dumps(metadata),
                target["linked_bom"],
                store.link_status(target["linked_bom"], target_record["registrant"], target_record["product"], target_record["project_category"]),
                now_iso(),
            ),
        )
        if not historical:
            store.execute(
                "UPDATE records SET version=?,previous=?,file_name=?,date=?,controlled=?,source=?,size=?,linked_bom=?,project_category=?,project_category_source=?,metadata=?,updated_at=? WHERE id=?",
                (
                    target["version"],
                    previous_current_version,
                    target["file_name"],
                    today_text(),
                    _dt.date.today().isoformat(),
                    share_id,
                    file_size,
                    target["linked_bom"] or target_record["linked_bom"],
                    normalize_project_category(target_record["project_category"]),
                    target_record["project_category_source"] or "共享归档",
                    json_dumps(metadata),
                    now_iso(),
                    target_record["id"],
                ),
            )
        detail = {
            "source_file_id": source_file_id,
            "source_record_id": source_record["id"],
            "target_record_id": target_record["id"],
            "target_file_id": file_id,
            "status": "历史版本" if historical else "当前有效",
            "reason": reason,
        }
        store.add_log(
            "附件共享归档",
            source_file["name"],
            (
                f"{source_record['registrant']} / {source_record['product']} / {source_record['module']} / {source_record['business_code']}"
                f" → {target_record['registrant']} / {target_record['product']} / {target_record['module']} / {target_record['business_code']}"
                f"；原因：{reason}；详情：{json_dumps(detail)}"
            ),
            share_id,
        )
        return {
            "status": "committed",
            "message": "已复用来源附件归档到目标清单。",
            "record_id": target_record["id"],
            "file_id": file_id,
            "source_file_id": source_file_id,
            "batch_id": share_id,
            "results": [{"status": "committed", "record_id": target_record["id"], "file_id": file_id}],
            "committed": 1,
            "blocked": 0,
        }
