from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


MAX_ZIP_MEMBERS = 1200
MAX_XML_BYTES = 12 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 48 * 1024 * 1024
BOM_COMPONENT_LIMIT = 500
NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
LEGACY_DATE_VERSION_BOM_CODES = {"11W2002C12B"}


def parse_file(data: bytes, filename: str) -> dict[str, Any]:
    from product_identity import resolve_product
    from filename_metadata import enrich_filename
    return enrich_filename(resolve_product(_parse_file(data, filename), filename), filename)


def _parse_file(data: bytes, filename: str) -> dict[str, Any]:
    """Parse one DMR candidate file into normalized local metadata."""
    name = Path(filename).name
    suffix = Path(name).suffix.lower()
    item = _blank_record(name, data)

    try:
        if suffix == ".xlsx":
            return _parse_xlsx(data, name, item)
        if suffix == ".docx":
            return _parse_document(data, name, item)
        if suffix == ".hex":
            return _parse_hex_name(name, item)
        if suffix == ".socprj":
            return _parse_socprj_name(name, item)
        if suffix == ".zip":
            return _parse_zip_name(name, item)
        if suffix == ".pdf":
            return _parse_document(data, name, item)
        if suffix.lower().endswith("config") or name.lower().endswith(".v16py32config"):
            return _parse_config_name(name, item)
    except Exception as exc:  # keep import usable; backend can mark manual review.
        item["notes"].append(f"解析失败：{exc.__class__.__name__}: {exc}")
        item["metadata"]["parse_error"] = str(exc)

    item["notes"].append("未识别文件类型，已按附件归档，需人工核对。")
    return item


def enrich_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach unambiguous companion files to software-history rows in-place."""
    histories = [x for x in items if x.get("metadata", {}).get("kind") == "software_history"]
    if not histories:
        return items

    history_versions: dict[str, list[dict[str, Any]]] = {}
    for history in histories:
        for row in history.get("metadata", {}).get("versions", []):
            name = str(row.get("name") or "")
            if name:
                key = _normalized_name(name)
                history_versions.setdefault(key, []).append(row)

    for item in items:
        if item.get("metadata", {}).get("kind") == "software_history":
            continue
        norm = _normalized_name(str(item.get("metadata", {}).get("source_filename") or ""))
        candidates = history_versions.get(norm, [])
        row = _select_unambiguous_history_row(candidates)
        if row:
            item["linked_bom"] = row.get("linked_bom", "") or item.get("linked_bom", "")
            item["version"] = row.get("version", "") or item.get("version", "")
            item["registrant"] = row.get("registrant", "") or item.get("registrant", "")
            from product_identity import resolve_product, models
            item["metadata"]["product_body_candidates"] = list(dict.fromkeys(item["metadata"].get("product_body_candidates", []) + models(row.get("product", ""))))
            item["product"] = row.get("product", "") or item.get("product", "")
            resolve_product(item, str(item["metadata"].get("source_filename") or ""))
            item["module"] = item.get("module") or "软件"
            item["type"] = item.get("type") or "软件附件"
            if not item.get("project_category") or item.get("project_category") == "待确认":
                item["project_category"] = row.get("project_category", "") or "待确认"
                item["project_category_source"] = row.get("project_category_source", "") or item.get("project_category_source", "待确认")
            item["metadata"]["linked_history_row"] = row
            item["notes"].append("按软件履历表中的文件名精确关联。")
            from filename_metadata import enrich_filename
            enrich_filename(item, str(item["metadata"].get("source_filename") or ""))
            continue
        if candidates:
            item["metadata"]["history_link_candidates"] = candidates
            item["notes"].append("存在同名履历候选但注册人或BOM不唯一，未自动关联，需人工核对。")
            continue

        if item.get("metadata", {}).get("kind") in {"pdf", "docx"}:
            stem = _strip_ext(str(item.get("metadata", {}).get("source_filename") or ""))
            for history in histories:
                hstem = _strip_ext(str(history.get("metadata", {}).get("source_filename") or ""))
                if stem and hstem and (stem in hstem or hstem in stem):
                    item["module"] = "软件"
                    item["type"] = "履历伴随文档"
                    item["metadata"]["companion_history_code"] = history.get("code", "")
                    item["metadata"]["companion_suggestion_only"] = True
                    item["notes"].append("与软件履历表同名或同主干，作为伴随文档候选；版本以本文件内容读取结果为准，未继承其他文件版本。")
                    break

    return items


def compare_versions(new: str, old: str) -> int | None:
    left = _version_key(new)
    right = _version_key(old)
    if left is None or right is None:
        return None
    if left[0] != right[0]:
        return None
    return (left > right) - (left < right)


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


def _parse_xlsx(data: bytes, name: str, item: dict[str, Any]) -> dict[str, Any]:
    sheets = _read_xlsx(data)
    rows = [row for _, sheet_rows in sheets for row in sheet_rows]
    from product_identity import models
    item["metadata"]["product_body_candidates"] = list(dict.fromkeys(
        model for row in rows for index, cell in enumerate(row)
        if re.fullmatch(r"(?:产品型号|型号|机型|规格型号)[:：]?", cell.strip())
        for model in models(" ".join(row[index + 1:index + 3]))
    ))
    if _looks_like_bom(rows):
        return _parse_bom_xlsx(rows, name, item)
    if _looks_like_software_history(rows, name):
        return _parse_software_history(rows, name, item)
    return _parse_generic_xlsx(rows, name, item, sheets=sheets)


def _parse_bom_xlsx(rows: list[list[str]], name: str, item: dict[str, Any]) -> dict[str, Any]:
    header_index = next(i for i, row in enumerate(rows) if "BOM层级" in row)
    header = rows[header_index]
    fields = {value: idx for idx, value in enumerate(header) if value}
    root = None
    for row in rows[header_index + 1 :]:
        if _get(row, fields, "BOM层级") in {"0", "0.0"}:
            root = row
            break
    if root is None:
        raise ValueError("BOM root row not found")

    code = _get(root, fields, "父项编码") or _code_from_filename(name)
    version_raw = _get(root, fields, "BOM版本") or _version_from_name(name)
    document_version = _strip_code_prefix(version_raw, code)
    display_version = document_version
    root_map = _row_dict(root, header)
    from product_identity import models
    item["metadata"]["product_body_candidates"] = models(_get(root, fields, "规格型号"))
    full_spec = _get(root, fields, "规格型号") or _spec_from_filename(name)
    registrant = _registrant_from_text(full_spec) or _registrant_from_text(name)
    product = _stable_product_name(full_spec or name)
    filename_date = _date_from_name(name)
    bom_type = _get(root, fields, "物料类别") or _get(root, fields, "父项名称") or "BOM"
    bom_components, bom_components_truncated = _bom_components(rows, header_index, header, fields)
    metadata: dict[str, Any] = {
        "kind": "bom",
        "management_kind": "bom",
        "full_spec": full_spec,
        "root_bom_version": version_raw,
        "document_version": document_version,
        "version_semantic": "document_version",
        "root_row": root_map,
        "row_count": max(0, len(rows) - header_index - 1),
        "bom_components": bom_components,
        "bom_components_truncated": bom_components_truncated,
        "filename_date": filename_date,
        "source_bom_code": code,
        "identifiers": {
            "bom_no": code,
            "finished_goods_no": code if _is_finished_goods_bom(name, root_map, code) else "",
            "document_version": document_version,
        },
        "dates": {"version_date": filename_date} if filename_date else {},
        "field_evidence": [
            {"field": "bom_no", "source": "bom_root", "value": code},
            {"field": "document_version", "source": "bom_root", "value": document_version},
        ],
    }
    use_filename_date = _is_finished_goods_bom(name, root_map, code)
    if use_filename_date:
        stem_code = _code_from_filename(name)
        display_version = filename_date
        metadata.update(
            {
                "version_rule": "filename_date",
                "version_scope": "finished_goods_bom",
                "document_version": document_version,
                "version_date": filename_date,
                "version_semantic": "finished_goods_bom_date",
                "source_bom_code": code,
                "filename_bom_code": stem_code,
            }
        )
        metadata["dates"]["version_date"] = filename_date
        metadata["field_evidence"].append({"field": "version_date", "source": "filename", "value": filename_date})
        item.update(
            {
                "code": code,
                "registrant": registrant,
                "product": product,
                "module": "BOM",
                "type": bom_type,
                "project_category": _project_category_from_row(root_map)[0],
                "project_category_source": _project_category_from_row(root_map)[1],
                "version": display_version,
                "linked_bom": code,
            }
        )
        item["metadata"].update(metadata)
        if stem_code != code:
            raise ValueError(f"文件名编号 {stem_code or '空'} 与BOM根编号 {code} 不一致")
        if not filename_date:
            raise ValueError(f"{code} 已确认按文件名日期管理版本，但文件名缺少有效YYYYMMDD日期")

    item.update(
        {
            "code": code,
            "registrant": registrant,
            "product": product,
            "module": "BOM",
            "type": bom_type,
            "project_category": _project_category_from_row(root_map)[0],
            "project_category_source": _project_category_from_row(root_map)[1],
            "version": display_version,
            "linked_bom": code,
        }
    )
    item["metadata"].update(metadata)
    return item


def _bom_components(rows: list[list[str]], header_index: int, header: list[str], fields: dict[str, int]) -> tuple[list[dict[str, Any]], bool]:
    material_key = _first_existing_field(fields, ("子项编码", "物料编码", "子项物料编码", "物料号"))
    if not material_key:
        return [], False
    drawing_key = _first_existing_field(fields, ("图号", "图纸编号", "图纸号", "图纸编码"))
    components: list[dict[str, Any]] = []
    for physical_row, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        level = _get(row, fields, "BOM层级")
        if level in {"0", "0.0"}:
            continue
        material_no = _get(row, fields, material_key)
        if not material_no:
            continue
        component: dict[str, Any] = {
            "material_no": material_no,
            "row": physical_row,
            "source_column": material_key,
        }
        if drawing_key:
            drawing_no = _get(row, fields, drawing_key)
            if drawing_no:
                component["drawing_no"] = drawing_no
                component["drawing_source_column"] = drawing_key
        components.append(component)
        if len(components) >= BOM_COMPONENT_LIMIT:
            return components, True
    return components, False


def _first_existing_field(fields: dict[str, int], candidates: tuple[str, ...]) -> str:
    return next((candidate for candidate in candidates if candidate in fields), "")


def _parse_software_history(rows: list[list[str]], name: str, item: dict[str, Any]) -> dict[str, Any]:
    header_index = _history_header_index(rows)
    header = rows[header_index]
    versions: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        rowmap = _row_dict(row, header)
        version = rowmap.get("完整版本") or rowmap.get("软件版本") or rowmap.get("发布版本") or ""
        sw_name = rowmap.get("软件名字") or rowmap.get("软件名称") or ""
        from spreadsheet_document_parser import is_software_history_version_row
        if not is_software_history_version_row(rowmap):
            continue
        bom_info = rowmap.get("BOM信息") or ""
        registrant = _registrant_from_text(bom_info)
        product = _product_from_bom_info(bom_info)
        linked_bom = _linked_bom_from_text(bom_info)
        project_category, project_category_source = _project_category_from_row(rowmap)
        versions.append(
            {
                "version": _normalize_version(version),
                "name": sw_name,
                "linked_bom": linked_bom,
                "registrant": registrant,
                "product": product,
                "project_category": project_category,
                "project_category_source": project_category_source,
                "date": _excel_date(rowmap.get("更新日期") or rowmap.get("发布日期") or ""),
                "publisher": rowmap.get("发布人") or "",
                "evidence": {k: v for k, v in rowmap.items() if v},
            }
        )

    main = _select_history_main_row(versions)

    code_seed = str((main or {}).get("name") or name)
    code = _strip_version_from_stem(_strip_ext(code_seed)) + "-HISTORY"
    item.update(
        {
            "code": code,
            "registrant": str((main or {}).get("registrant") or ""),
            "product": str((main or {}).get("product") or _stable_product_name(name)),
            "module": "软件",
            "type": "软件履历",
            "project_category": str((main or {}).get("project_category") or "待确认"),
            "project_category_source": str((main or {}).get("project_category_source") or "未提供"),
            # A history row describes a program release, not the revision of
            # this spreadsheet. Keep the document pending until its own
            # version is supplied or confirmed by document control.
            "version": "",
            "linked_bom": str((main or {}).get("linked_bom") or ""),
        }
    )
    if item["type"] == "软件履历" and not item["linked_bom"]:
        item["notes"].append("履历表主版本未提供明确 BOM 编号，需人工核对关联。")
    if main and main.get("version"):
        item["notes"].append("表内程序版本不等于履历表文档版本，请人工确认本文件版本。")
    if versions and main is None:
        item["notes"].append("软件履历表包含多个程序/版本候选，未自动选择主版本；同批附件仍按文件名逐条精确关联。")
    from product_identity import models
    item["metadata"]["product_body_candidates"] = models(str((main or {}).get("product") or ""))
    item["metadata"].update(
        {
            "kind": "software_history",
            "management_kind": "history",
            "versions": versions,
            "main": main,
            "main_selection_status": "found" if main else ("pending" if versions else "missing"),
            "version_semantic": "software_program_version",
            "document_version": "",
            "identifiers": {
                "software_history_code": code,
                "linked_bom": str((main or {}).get("linked_bom") or ""),
            },
            "field_evidence": [
                {"field": "software_program_version", "source": "software_history_row", "value": str((main or {}).get("version") or "")},
                {"field": "linked_bom", "source": "software_history_row", "value": str((main or {}).get("linked_bom") or "")},
            ],
        }
    )
    if main is None:
        item["metadata"]["history_main_candidates"] = versions
    return item


def _select_history_main_row(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not versions:
        return None
    program_rows = [
        row
        for row in versions
        if re.search(r"\.(?:hex|socprj|bin|dfu|elf|zip|v\d+[a-z0-9]*config)$", str(row.get("name", "")), re.I)
    ]
    pool = program_rows or versions
    signatures = {
        (
            _strip_version_from_stem(_strip_ext(str(row.get("name", "")))),
            str(row.get("version", "")),
        )
        for row in pool
        if row.get("name") and row.get("version")
    }
    if len(signatures) == 1:
        return pool[0]
    return None


def _parse_generic_xlsx(rows: list[list[str]], name: str, item: dict[str, Any], *, sheets=None) -> dict[str, Any]:
    stem = _strip_ext(name)
    item["code"] = _strip_date_suffix(_strip_version_from_stem(stem))
    item["version"] = _version_from_name(name)
    item["metadata"]["kind"] = "spreadsheet"
    item["metadata"]["management_kind"] = "spreadsheet"
    if "软件" in name or "整改" in name or "发布" in name:
        item["module"] = "软件"
        item["type"] = "软件资料"
        item["notes"].append("Excel 非履历/BOM结构，按软件资料归档，需人工核对内容。")
    else:
        item["type"] = "表格附件"
        item["notes"].append("Excel 未匹配 BOM 或软件履历结构，按附件归档。")
    item["metadata"]["title"] = _first_nonempty(rows)
    from spreadsheet_document_parser import extract_spreadsheet_document_version
    result = extract_spreadsheet_document_version(sheets=sheets, rows=None if sheets else rows)
    item["metadata"].update({"version_evidence": result["version_evidence"], "version_candidates": result["version_candidates"], "version_reading_status": result["status"]})
    if result["status"] == "found":
        item["version"] = result["version"]
        item["metadata"]["version_semantic"] = result["scope"] + "_version"
        evidence = result["version_evidence"]
        item["notes"].append(f"从原件字段“{evidence['label']}”读取版本 {result['version']}。")
    elif result["status"] == "pending":
        item["version"] = ""
        item["notes"].extend(result["notes"])
    return item


def _parse_hex_name(name: str, item: dict[str, Any]) -> dict[str, Any]:
    stem = _strip_ext(name)
    item.update(
        {
            "code": _strip_version_from_stem(stem),
            "module": "软件",
            "type": "固件",
            "version": _version_from_name(name),
        }
    )
    item["metadata"]["kind"] = "firmware_hex"
    item["metadata"]["management_kind"] = "software"
    item["metadata"]["version_semantic"] = "software_program_version"
    return item


def _parse_socprj_name(name: str, item: dict[str, Any]) -> dict[str, Any]:
    stem = re.sub(r"_0x[0-9A-Fa-f]+$", "", _strip_ext(name))
    item.update({"code": _strip_version_from_stem(stem), "module": "软件", "type": "触摸/配置工程", "version": _version_from_name(name)})
    item["metadata"]["kind"] = "socprj"
    item["metadata"]["management_kind"] = "software"
    item["metadata"]["version_semantic"] = "software_project_version"
    return item


def _parse_zip_name(name: str, item: dict[str, Any]) -> dict[str, Any]:
    stem = _strip_ext(name)
    is_software_tool = bool(re.search(r"软件|固件|校准|工具|firmware|software", stem, re.I))
    item.update(
        {
            "code": _strip_version_from_stem(stem),
            "module": "软件" if is_software_tool else "",
            "type": "工具包" if is_software_tool else "压缩包附件",
            "version": _version_from_name(name),
        }
    )
    item["metadata"]["kind"] = "zip_attachment"
    item["metadata"]["management_kind"] = "software" if is_software_tool else "attachment"
    item["metadata"]["version_semantic"] = "package_or_tool_version"
    return item


def _parse_document(data: bytes, name: str, item: dict[str, Any]) -> dict[str, Any]:
    from document_parser import parse_document_file
    result = parse_document_file(data, name)
    # A filename version remains usable for attachments without readable content.
    # Conflicting content candidates must still be resolved by document control.
    if not result["version"] and not result["metadata"].get("version_candidates"):
        result["version"] = _version_from_name(name)
    return result


def _parse_config_name(name: str, item: dict[str, Any]) -> dict[str, Any]:
    stem = _strip_ext(name)
    item.update({"code": _strip_version_from_stem(stem), "module": "软件", "type": "配置文件", "version": _version_from_name(name)})
    item["metadata"]["kind"] = "config"
    item["metadata"]["management_kind"] = "software"
    item["metadata"]["version_semantic"] = "configuration_version"
    item["notes"].append("配置文件仅从文件名识别版本，内容需人工核对。")
    return item


def _read_xlsx(data: bytes) -> list[tuple[str, list[list[str]]]]:
    with _safe_zip(data) as zf:
        shared = _shared_strings(zf)
        sheet_names = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        sheets = []
        for sheet_name in sheet_names:
            root = ET.fromstring(_read_zip_xml(zf, sheet_name))
            rows: list[list[str]] = []
            for row in root.findall(".//a:row", NS):
                values: dict[int, str] = {}
                for cell in row.findall("a:c", NS):
                    index = _column_index(cell.attrib.get("r", "A1"))
                    values[index] = _cell_value(cell, shared)
                if values:
                    width = max(values) + 1
                    rows.append([values.get(i, "") for i in range(width)])
            sheets.append((sheet_name, rows))
        return sheets


def _safe_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(BytesIO(data))
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError("zip member count exceeds limit")
    total_xml = 0
    for info in infos:
        name = info.filename
        norm = posixpath.normpath(name)
        if norm.startswith("../") or norm.startswith("/") or "\x00" in name:
            raise ValueError("unsafe zip member name")
        if name.endswith(".xml"):
            total_xml += info.file_size
            if info.file_size > MAX_XML_BYTES:
                raise ValueError("xml member exceeds limit")
    if total_xml > MAX_TOTAL_XML_BYTES:
        raise ValueError("total xml size exceeds limit")
    return zf


def _read_zip_xml(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError("xml member exceeds limit")
    return _reject_unsafe_xml(zf.read(name))


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(_read_zip_xml(zf, "xl/sharedStrings.xml"))
    values = []
    for si in root.findall("a:si", NS):
        values.append("".join(t.text or "" for t in si.findall(".//a:t", NS)))
    return values


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    value = cell.find("a:v", NS)
    if cell.attrib.get("t") == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(".//a:t", NS)).strip()
    if value is None:
        return ""
    raw = (value.text or "").strip()
    if cell.attrib.get("t") == "s" and raw.isdigit():
        return shared[int(raw)].strip() if int(raw) < len(shared) else ""
    return raw


def _column_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref)
    if not letters:
        return 0
    total = 0
    for ch in letters.group(1):
        total = total * 26 + ord(ch) - ord("A") + 1
    return total - 1


def _looks_like_bom(rows: list[list[str]]) -> bool:
    return any("BOM层级" in row and "父项编码" in row and "BOM版本" in row for row in rows[:10])


def _looks_like_software_history(rows: list[list[str]], name: str) -> bool:
    if "软件履历" in name:
        return True
    return _history_header_index(rows) >= 0


def _history_header_index(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows[:20]):
        if ("软件名字" in row or "软件名称" in row) and ("BOM信息" in row or "软件版本" in row or "完整版本" in row):
            return i
    return -1


def _row_dict(row: list[str], header: list[str]) -> dict[str, str]:
    return {key: row[i] if i < len(row) else "" for i, key in enumerate(header) if key}


def _get(row: list[str], fields: dict[str, int], key: str) -> str:
    idx = fields.get(key)
    return row[idx].strip() if idx is not None and idx < len(row) else ""


def _project_category_from_row(rowmap: dict[str, str]) -> tuple[str, str]:
    for key in ("项目类别", "项目属性", "项目分类", "业务类别"):
        raw = (rowmap.get(key) or "").strip()
        if raw in {"医疗", "家电"}:
            return raw, key
        if raw in {"医疗项目", "医疗器械"}:
            return "医疗", key
        if raw in {"家电项目", "小家电"}:
            return "家电", key
    return "待确认", "未提供"


def _registrant_from_text(text: str) -> str:
    match = re.search(r"【([^】]+)】", text or "")
    return match.group(1).strip() if match else ""


def _stable_product_name(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^[A-Z0-9]{6,}(_V[0-9A-Za-z.]+)?[-_\s]*", "", text)
    text = re.split(r"[【（(]", text, maxsplit=1)[0]
    text = re.split(r"\[|强脉冲", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip(" _-")


def _product_from_bom_info(text: str) -> str:
    text = text or ""
    for part in re.split(r"[\t\r\n]+", text):
        if "[" in part or "强脉冲" in part or "美容仪" in part or "大排灯" in part:
            product = _stable_product_name(part)
            if product and not product.startswith("成品"):
                return product
    without_code = re.sub(r"^[0-9A-Z]{8,14}B?", "", text).strip()
    model = re.search(r"([A-Z]{1,5}\d{1,3}[A-Z]?(?:\s+[A-Z0-9]+){0,3})\s*(?:\[|强脉冲|美容仪|大排灯)", without_code)
    if model:
        return re.sub(r"\s+", " ", model.group(1)).strip()
    return _stable_product_name(without_code)


def _linked_bom_from_text(text: str) -> str:
    match = re.search(r"([0-9A-Z]{8,14}B?)", text or "")
    return match.group(1) if match else ""


def _strip_code_prefix(version: str, code: str) -> str:
    version = (version or "").strip()
    if code and version.startswith(code + "_"):
        version = version[len(code) + 1 :]
    return version


def _code_from_filename(name: str) -> str:
    match = re.match(r"(.+?)_V", Path(name).name, re.I)
    return match.group(1).strip() if match else ""


def _is_finished_goods_bom(name: str, rowmap: dict[str, str], code: str) -> bool:
    if code in LEGACY_DATE_VERSION_BOM_CODES:
        return True
    stem = _strip_ext(name)
    if re.search(r"(?:^|[-_])成品(?:$|[-_（(])", stem) and not re.search(r"(?:^|[-_])半成品(?:$|[-_（(])", stem):
        return True
    for key in ("物料类别", "父项名称"):
        value = (rowmap.get(key) or "").strip()
        if value.startswith("成品") and not value.startswith("半成品"):
            return True
    return False


def _spec_from_filename(name: str) -> str:
    stem = _strip_ext(name)
    return re.sub(r"^[A-Z0-9]{6,}_V[0-9A-Za-z.]+-", "", stem)


def _version_from_name(name: str) -> str:
    match = re.search(r"(?<![A-Za-z0-9])([VA]\d+(?:\.\d+)+)(?![A-Za-z0-9])", name, re.I)
    if not match:
        match = re.search(r"([VA]\d+(?:\.\d+)+)", name, re.I)
    return match.group(1).upper() if match else ""


def _version_key(value: str) -> tuple[str, tuple[int, ...]] | None:
    value = _normalize_version(value)
    if re.fullmatch(r"20\d{6}", value):
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError:
            return None
        return ("DATE", (int(value),))
    match = re.fullmatch(r"([VA])(\d+(?:\.\d+)*)", value, re.I)
    if not match:
        return None
    return (match.group(1).upper(), tuple(int(x) for x in match.group(2).split(".")))


def _normalize_version(value: str) -> str:
    value = (value or "").strip()
    match = re.search(r"([VA]\d+(?:\.\d+)+)", value, re.I)
    return match.group(1).upper() if match else value


def _strip_ext(name: str) -> str:
    return re.sub(r"\.[^.]+$", "", Path(name).name)


def _strip_version_from_stem(stem: str) -> str:
    stem = re.sub(r"[_\-\s]*[VA]\d+(?:\.\d+)+.*$", "", stem, flags=re.I)
    return stem.strip(" _-")


def _strip_date_suffix(stem: str) -> str:
    return re.sub(r"[-_\s]*(?:20\d{2}[._-]?\d{1,2}[._-]?\d{1,2}|20\d{6}).*$", "", stem).strip(" _-")


def _date_from_name(name: str) -> str:
    stem = _strip_ext(name)
    match = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})$", stem)
    if not match:
        return ""
    value = "".join(match.groups())
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return ""
    return value


def _excel_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"\d+(?:\.0)?", value):
        days = int(float(value))
        return (datetime(1899, 12, 30) + timedelta(days=days)).date().isoformat()
    return value


def _first_nonempty(rows: list[list[str]]) -> str:
    for row in rows:
        for value in row:
            if value:
                return value
    return ""


def _xml_text(data: bytes) -> str:
    root = ET.fromstring(_reject_unsafe_xml(data))
    return "".join(root.itertext()).strip()


def _normalized_name(name: str) -> str:
    return Path(name).name.casefold().replace(" ", "")


def _select_unambiguous_history_row(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    with_bom = [row for row in candidates if row.get("linked_bom")]
    pool = with_bom or candidates
    signatures = {
        (
            row.get("linked_bom", ""),
            row.get("registrant", ""),
            row.get("product", ""),
            row.get("version", ""),
        )
        for row in pool
    }
    if len(signatures) == 1:
        return pool[0]
    return None


def _reject_unsafe_xml(data: bytes) -> bytes:
    prefix = data[:4096].upper()
    if b"<!DOCTYPE" in prefix or b"<!ENTITY" in prefix:
        raise ValueError("xml doctype/entity declarations are not allowed")
    return data
