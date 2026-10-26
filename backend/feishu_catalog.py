from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status


CATALOG_FILE = "feishu_catalog.json"
SCHEMA_VERSION = 1
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{10,80}$")
FEISHU_HOST = os.environ.get("DMR_FEISHU_HOST", "https://example.feishu.cn").rstrip("/")
SHEET_URL_RE = re.compile(r"^" + re.escape(FEISHU_HOST) + r"/sheets/[A-Za-z0-9]{10,80}$")


class CatalogSnapshotError(ValueError):
    pass


def install_feishu_catalog(app: FastAPI, config, policy) -> None:
    from backend.catalog_refresh import CatalogRefreshService

    snapshot_path = Path(config.data_dir) / CATALOG_FILE
    if getattr(app.state, "catalog_refresh", None) is None:
        app.state.catalog_refresh = CatalogRefreshService(Path(config.data_dir))

    def require_global_read(request: Request) -> None:
        access = policy(request, "read")
        if not access.global_scope:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="DMR清单云文档目录只开放给全局授权人员；当前账号仍可查看授权范围内的受控文件。",
            )

    @app.get("/api/dmr-catalog")
    def dmr_catalog(request: Request):
        require_global_read(request)
        snapshot = load_snapshot(snapshot_path)
        result = summarize_snapshot(snapshot)
        try:
            fetched=datetime.fromisoformat(snapshot['fetched_at'].replace('Z','+00:00'))
            result['snapshot_stale']=(datetime.now(timezone.utc)-fetched).total_seconds()>86400
        except (ValueError,TypeError): result['snapshot_stale']=True
        refresh_status = app.state.catalog_refresh.status()
        result["refresh_status"] = refresh_status
        result["sync_status"] = {key: refresh_status.get(key, "") for key in ("status", "checked_at", "message")}
        return result

    @app.post("/api/dmr-catalog/refresh")
    def dmr_catalog_refresh(request: Request, force: bool = False, wait: bool = False):
        require_global_read(request)
        return app.state.catalog_refresh.ensure_fresh(force=force, wait=wait, wait_timeout=120)

    @app.get("/api/dmr-catalog-search")
    def dmr_catalog_search(
        request: Request,
        query: str = "",
        registrant: str = "",
        project_category: str = "",
        product: str = "",
        module: str = "",
    ):
        require_global_read(request)
        refresh = app.state.catalog_refresh.ensure_fresh(force=True, wait=True, wait_timeout=120)
        if refresh.get("status") != "success" or refresh.get("running"):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="未能读取最新飞书目录，请检查授权或网络后重试")
        snapshot = load_snapshot(snapshot_path)
        return search_snapshot(
            snapshot,
            query=query,
            registrant=registrant,
            project_category=project_category,
            product=product,
            module=module,
        )

    @app.get("/api/dmr-catalog/{token}")
    def dmr_catalog_document(token: str, request: Request):
        require_global_read(request)
        if not _valid_token(token):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DMR清单文档不存在")
        snapshot = load_snapshot(snapshot_path)
        document = next((item for item in snapshot["documents"] if item["token"] == token), None)
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DMR清单文档不存在")
        return copy.deepcopy(document) | {"writeback_enabled": False}


def load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="DMR清单快照尚未生成，请先完成飞书只读同步。")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        snapshot = _validate_snapshot(raw)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"DMR清单快照不可用：{exc}") from exc
    snapshot["writeback_enabled"] = False
    return snapshot


def summarize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    result["writeback_enabled"] = False
    result["documents"] = [
        _summarize_document(document)
        for document in result["documents"]
    ]
    return result


def search_snapshot(
    snapshot: dict[str, Any],
    *,
    query: str = "",
    registrant: str = "",
    project_category: str = "",
    product: str = "",
    module: str = "",
) -> dict[str, Any]:
    query = _clean_query(query)
    registrant = _clean_filter(registrant)
    project_category = _clean_filter(project_category)
    product = _clean_filter(product)
    module = _clean_filter(module)
    documents: list[dict[str, Any]] = []
    needs_row_match = bool(query or module)
    for document in snapshot["documents"]:
        document_context = _document_context_text(document)
        if registrant and not _filter_matches(registrant, document_context):
            continue
        if project_category and not _filter_matches(project_category, document_context):
            continue
        if product and not _product_filter_matches(product, document_context):
            continue
        matches = _document_matches(document, query=query, module=module)
        document_context_match = bool(query and _filter_matches(query, document_context))
        if document_context_match:
            matches.append(
                {
                    "sheet_name": "",
                    "sheet_id": "",
                    "row": 0,
                    "value": document_context,
                    "module": module,
                    "match_basis": "document",
                    "requires_manual_confirmation": True,
                }
            )
        unverified = bool(module and not query and not matches)
        unverified = unverified or bool(module and document_context_match and not _has_module_verified_match(matches))
        if needs_row_match and not matches and not unverified:
            continue
        summary = _summarize_document(document)
        if matches:
            summary["matches"] = matches[:20]
        if unverified:
            summary["catalog_match_unverified"] = True
            summary["match_note"] = "snapshot has no sheet cells; matched by document context only"
        documents.append(summary)
    return {
        "documents": documents,
        "fetched_at": snapshot["fetched_at"],
        "source": "snapshot",
    }


def _summarize_document(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "sheets"} | {"sheet_count": len(document.get("sheets", []))}


def _clean_query(value: Any) -> str:
    return str(value or "").strip()


def _clean_filter(value: Any) -> str:
    text = _clean_query(value)
    return "" if text.casefold() == "all" else text


def _document_context_text(document: dict[str, Any]) -> str:
    return " ".join(part for part in [document.get("name", ""), " / ".join(document.get("path", []))] if part)


def _document_search_text(document: dict[str, Any]) -> str:
    parts = [_document_context_text(document)]
    for sheet in document.get("sheets", []):
        for block in sheet.get("ranges", []):
            for row in block.get("cells", []):
                parts.extend(_cell_text(cell) for cell in row)
    return " ".join(part for part in parts if part)


def _document_has_cells(document: dict[str, Any]) -> bool:
    for sheet in document.get("sheets", []):
        for block in sheet.get("ranges", []):
            for row in block.get("cells", []):
                if any(_cell_text(cell) for cell in row):
                    return True
    return False


def _filter_matches(needle: str, haystack: str) -> bool:
    left = _normalized_text(needle)
    right = _normalized_text(haystack)
    return bool(left and left in right)


def _product_filter_matches(needle: str, haystack: str) -> bool:
    normalized = _normalized_text(needle)
    target = _normalized_text(haystack)
    if not normalized:
        return True
    if normalized in target:
        return True
    base = _base_product_model(normalized)
    return bool(base and len(base) >= 4 and base in target)


def _has_module_verified_match(matches: list[dict[str, Any]]) -> bool:
    return any(match.get("module") and match.get("match_basis") != "document" for match in matches)


def _base_product_model(value: str) -> str:
    return re.sub(r"(?:PR|PLUS|PRO)$", "", value, flags=re.IGNORECASE)


def _normalized_text(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).casefold()


def _document_matches(document: dict[str, Any], *, query: str, module: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str]] = set()
    for sheet in document.get("sheets", []):
        rows = _sheet_rows(sheet)
        # With no file keyword, find section headings, not a nonexistent file row.
        # Each cell is checked independently so side-by-side regions are supported.
        if module and not query:
            from backend.catalog_locator import category_name
            for number, cells in rows.items():
                for column, value in cells.items():
                    if category_name(value) == module:
                        matches.append({"sheet_id": sheet.get("sheet_id", ""),
                                        "sheet_name": sheet.get("sheet_name", ""),
                                        "row": number, "cell": f"{column}{number}",
                                        "value": value, "module": module,
                                        "match_basis": "section"})
            continue
        for match in _locator_matches(rows, sheet, query=query, module=module):
            key = (str(match.get("sheet_id", "")), str(match.get("code_cell", "")), int(match.get("row", 0)), str(match.get("match_basis", "")))
            if key not in seen:
                seen.add(key)
                matches.append(match)
        for match in _cell_matches(rows, sheet, query=query, module=module):
            key = (str(match.get("sheet_id", "")), str(match.get("cell", "")), int(match.get("row", 0)), str(match.get("match_basis", "")))
            if key not in seen:
                seen.add(key)
                matches.append(match)
    return sorted(matches, key=lambda item: (item.get("sheet_name", ""), item.get("row", 0), item.get("cell", item.get("code_cell", ""))))


def _sheet_rows(sheet: dict[str, Any]) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for block in sheet.get("ranges", []):
        row_indices = block.get("row_indices", [])
        col_indices = block.get("col_indices", [])
        cells = block.get("cells", [])
        for number, values in zip(row_indices, cells):
            rows.setdefault(number, {}).update({col: _cell_text(cell) for col, cell in zip(col_indices, values)})
    return rows


def _locator_matches(rows: dict[int, dict[str, str]], sheet: dict[str, Any], *, query: str, module: str) -> list[dict[str, Any]]:
    if not query and not module:
        return []
    try:
        from backend.catalog_locator import _locate_in_rows
    except Exception:
        return []
    target = re.sub(r"\s+", "", query).upper()
    located, _problems = _locate_in_rows(
        rows,
        target,
        str(sheet.get("sheet_id", "")),
        str(sheet.get("sheet_name", "")),
        "",
        filename=query,
        module=module,
    )
    return [
        {
            key: value
            for key, value in match.items()
            if key
            in {
                "sheet_name",
                "sheet_id",
                "row",
                "code_cell",
                "name_cell",
                "file_name",
                "current_code",
                "module",
                "match_basis",
                "requires_manual_confirmation",
                "version_cell",
                "current_version",
                "region_title",
                "region_area",
            }
        }
        for match in located
    ]


def _cell_matches(rows: dict[int, dict[str, str]], sheet: dict[str, Any], *, query: str, module: str) -> list[dict[str, Any]]:
    if not query:
        return []
    normalized_query = _normalized_text(query)
    if not normalized_query:
        return []
    modules_by_row = _module_by_row(rows)
    results: list[dict[str, Any]] = []
    for number, cells in rows.items():
        row_module = modules_by_row.get(number, "")
        if module and row_module != module:
            continue
        for column, value in cells.items():
            normalized_value = _normalized_text(value)
            if normalized_query and normalized_query in normalized_value:
                results.append(
                    {
                        "sheet_name": str(sheet.get("sheet_name", "")),
                        "sheet_id": str(sheet.get("sheet_id", "")),
                        "row": number,
                        "cell": f"{column}{number}",
                        "value": value,
                        "module": row_module,
                        "match_basis": "cell",
                        "requires_manual_confirmation": True,
                    }
                )
    return results


def _module_by_row(rows: dict[int, dict[str, str]]) -> dict[int, str]:
    try:
        from backend.catalog_locator import category_name
    except Exception:
        category_name = _local_category_name
    current = ""
    result: dict[int, str] = {}
    for number in sorted(rows):
        values = [value for value in rows[number].values() if value.strip()]
        if len(values) == 1:
            section = category_name(values[0])
            if section:
                current = section
                result[number] = current
                continue
        if current:
            result[number] = current
    return result


def _local_category_name(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).upper()
    labels = {
        "BOM": ("BOM", "物料清单"),
        "软件": ("软件",),
        "工艺文件": ("工艺文件",),
        "检验文件": ("检验文件",),
        "承认书&规格书": ("承认书&规格书", "承认书", "规格书"),
        "包装图纸": ("包装图纸",),
        "结构图纸": ("结构图纸",),
    }
    for module, names in labels.items():
        if any(text == name or text.startswith(name + "（") or text.startswith(name + "(") for name in names):
            return module
    return ""


def _cell_text(cell: Any) -> str:
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, dict):
        return _cell_text(cell.get("value", cell.get("text", "")))
    if isinstance(cell, list):
        return "".join(_cell_text(part) for part in cell).strip()
    return str(cell).strip() if cell is not None else ""


def _validate_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CatalogSnapshotError("根对象必须是JSON对象")
    required = {"schema_version", "root_token", "root_url", "fetched_at", "folders", "documents"}
    missing = sorted(required - set(raw))
    if missing:
        raise CatalogSnapshotError(f"缺少字段: {', '.join(missing)}")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise CatalogSnapshotError("schema_version不受支持")
    if not _valid_token(raw["root_token"]):
        raise CatalogSnapshotError("root_token格式不正确")
    if raw["root_url"] != f"{FEISHU_HOST}/drive/folder/{raw['root_token']}":
        raise CatalogSnapshotError("root_url必须是对应根目录的干净飞书云盘链接")
    if not isinstance(raw["fetched_at"], str) or not raw["fetched_at"].strip():
        raise CatalogSnapshotError("fetched_at不能为空")

    folders = _validate_folders(raw["folders"], raw["root_token"])
    documents = _validate_documents(raw["documents"], {folder["token"] for folder in folders})
    return {
        "schema_version": SCHEMA_VERSION,
        "root_token": raw["root_token"],
        "root_url": raw["root_url"],
        "fetched_at": raw["fetched_at"],
        "writeback_enabled": False,
        "folders": folders,
        "documents": documents,
    }


def _validate_folders(raw: Any, root_token: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise CatalogSnapshotError("folders必须是非空数组")
    seen: set[str] = set()
    folders: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CatalogSnapshotError(f"folders[{index}]必须是对象")
        token = _required_token(item, f"folders[{index}].token")
        if token in seen:
            raise CatalogSnapshotError(f"folders[{index}].token重复")
        seen.add(token)
        parent_token = item.get("parent_token")
        if parent_token is not None and not _valid_token(parent_token):
            raise CatalogSnapshotError(f"folders[{index}].parent_token格式不正确")
        folders.append(
            {
                "token": token,
                "parent_token": parent_token,
                "name": _required_text(item, "name", f"folders[{index}].name"),
                "path": _required_path(item, f"folders[{index}].path"),
            }
        )
    if root_token not in seen:
        raise CatalogSnapshotError("folders缺少根目录")
    known = set(seen)
    roots = [folder for folder in folders if folder["parent_token"] is None]
    if len(roots) != 1 or roots[0]["token"] != root_token:
        raise CatalogSnapshotError("folders必须只有一个根目录")
    for index, folder in enumerate(folders):
        parent_token = folder["parent_token"]
        if parent_token is not None and parent_token not in known:
            raise CatalogSnapshotError(f"folders[{index}].parent_token不存在")
    parent_by_token = {folder["token"]: folder["parent_token"] for folder in folders}
    for folder in folders:
        chain: set[str] = set()
        token = folder["token"]
        while token is not None:
            if token in chain:
                raise CatalogSnapshotError("folders存在循环引用")
            chain.add(token)
            token = parent_by_token.get(token)
    children_by_parent: dict[str, list[str]] = {}
    for folder in folders:
        parent_token = folder["parent_token"]
        if parent_token:
            children_by_parent.setdefault(parent_token, []).append(folder["token"])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(token: str) -> None:
        if token in visiting:
            raise CatalogSnapshotError("folders存在循环引用")
        if token in visited:
            return
        visiting.add(token)
        for child_token in children_by_parent.get(token, []):
            visit(child_token)
        visiting.remove(token)
        visited.add(token)

    visit(root_token)
    if visited != known:
        raise CatalogSnapshotError("folders存在未挂到根目录的目录")
    return folders


def _validate_documents(raw: Any, folder_tokens: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise CatalogSnapshotError("documents必须是数组")
    seen: set[str] = set()
    documents: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CatalogSnapshotError(f"documents[{index}]必须是对象")
        token = _required_token(item, f"documents[{index}].token")
        if token in seen:
            raise CatalogSnapshotError(f"documents[{index}].token重复")
        seen.add(token)
        parent_token = _required_token(item, f"documents[{index}].parent_token")
        if parent_token not in folder_tokens:
            raise CatalogSnapshotError(f"documents[{index}].parent_token不存在")
        url = _required_text(item, "url", f"documents[{index}].url", max_len=300)
        if not SHEET_URL_RE.fullmatch(url) or not url.endswith(f"/{token}"):
            raise CatalogSnapshotError(f"documents[{index}].url必须是对应的飞书表格链接")
        doc_type = item.get("type")
        if doc_type != "sheet":
            raise CatalogSnapshotError(f"documents[{index}].type必须是sheet")
        documents.append(
            {
                "token": token,
                "parent_token": parent_token,
                "name": _required_text(item, "name", f"documents[{index}].name"),
                "url": url,
                "path": _required_path(item, f"documents[{index}].path"),
                "type": "sheet",
                "revision": _optional_revision(item),
                "read_at": _optional_text(item, "read_at", max_len=80),
                "sheets": _validate_sheets(item.get("sheets"), index),
            }
        )
    return documents


def _validate_sheets(raw: Any, document_index: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise CatalogSnapshotError(f"documents[{document_index}].sheets必须是数组")
    sheets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        prefix = f"documents[{document_index}].sheets[{index}]"
        if not isinstance(item, dict):
            raise CatalogSnapshotError(f"{prefix}必须是对象")
        sheet_id = _required_text(item, "sheet_id", f"{prefix}.sheet_id", max_len=120)
        if sheet_id in seen:
            raise CatalogSnapshotError(f"{prefix}.sheet_id重复")
        seen.add(sheet_id)
        row_count = _required_non_negative_int(item, "row_count", f"{prefix}.row_count")
        column_count = _required_non_negative_int(item, "column_count", f"{prefix}.column_count")
        ranges = _validate_ranges(item.get("ranges"), prefix)
        _validate_sheet_ranges_complete(ranges, row_count, column_count, prefix)
        sheets.append(
            {
                "sheet_id": sheet_id,
                "sheet_name": _required_text(item, "sheet_name", f"{prefix}.sheet_name"),
                "row_count": row_count,
                "column_count": column_count,
                "ranges": ranges,
            }
        )
    return sheets


def _validate_ranges(raw: Any, prefix: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise CatalogSnapshotError(f"{prefix}.ranges必须是数组")
    ranges: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        item_prefix = f"{prefix}.ranges[{index}]"
        if not isinstance(item, dict):
            raise CatalogSnapshotError(f"{item_prefix}必须是对象")
        row_indices = item.get("row_indices")
        col_indices = item.get("col_indices")
        cells = item.get("cells")
        if not isinstance(row_indices, list) or not all(isinstance(row, int) and row >= 0 for row in row_indices):
            raise CatalogSnapshotError(f"{item_prefix}.row_indices必须是非负整数数组")
        if not isinstance(col_indices, list) or not all(isinstance(col, str) and col for col in col_indices):
            raise CatalogSnapshotError(f"{item_prefix}.col_indices必须是文本数组")
        if len(set(row_indices)) != len(row_indices):
            raise CatalogSnapshotError(f"{item_prefix}.row_indices不能重复")
        if len(set(col_indices)) != len(col_indices):
            raise CatalogSnapshotError(f"{item_prefix}.col_indices不能重复")
        if not isinstance(cells, list) or not all(isinstance(row, list) for row in cells):
            raise CatalogSnapshotError(f"{item_prefix}.cells必须是二维数组")
        if len(cells) != len(row_indices) or any(len(row) != len(col_indices) for row in cells):
            raise CatalogSnapshotError(f"{item_prefix}.cells尺寸必须匹配坐标")
        if item.get("truncated") is not False:
            raise CatalogSnapshotError(f"{item_prefix}.truncated必须为false")
        ranges.append(
            {
                "range": _required_text(item, "range", f"{item_prefix}.range", max_len=120),
                "actual_range": _required_text(item, "actual_range", f"{item_prefix}.actual_range", max_len=120),
                "row_indices": row_indices,
                "col_indices": col_indices,
                "cells": cells,
                "truncated": item.get("truncated") is True,
            }
        )
    return ranges


def _validate_sheet_ranges_complete(ranges: list[dict[str, Any]], row_count: int, column_count: int, prefix: str) -> None:
    expected_rows = set(range(1, row_count + 1))
    expected_cols = _column_labels(column_count)
    seen_rows: set[int] = set()
    for index, item in enumerate(ranges):
        item_prefix = f"{prefix}.ranges[{index}]"
        col_indices = item["col_indices"]
        if col_indices != expected_cols:
            raise CatalogSnapshotError(f"{item_prefix}.col_indices必须完整覆盖A到第{column_count}列")
        row_indices = item["row_indices"]
        duplicate_rows = seen_rows.intersection(row_indices)
        if duplicate_rows:
            raise CatalogSnapshotError(f"{item_prefix}.row_indices跨range重复")
        seen_rows.update(row_indices)
    if seen_rows != expected_rows:
        raise CatalogSnapshotError(f"{prefix}.ranges行号必须完整覆盖1到{row_count}")


def _column_labels(count: int) -> list[str]:
    labels: list[str] = []
    for index in range(1, count + 1):
        value = index
        label = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            label = chr(ord("A") + remainder) + label
        labels.append(label)
    return labels


def _required_token(item: dict[str, Any], field: str) -> str:
    value = item.get(field.rsplit(".", 1)[-1])
    if not isinstance(value, str) or not _valid_token(value):
        raise CatalogSnapshotError(f"{field}格式不正确")
    return value


def _valid_token(value: str) -> bool:
    return bool(isinstance(value, str) and TOKEN_RE.fullmatch(value))


def _required_text(item: dict[str, Any], key: str, field: str, *, max_len: int = 200) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogSnapshotError(f"{field}不能为空")
    if len(value) > max_len:
        raise CatalogSnapshotError(f"{field}过长")
    return value


def _optional_text(item: dict[str, Any], key: str, *, max_len: int) -> str:
    value = item.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_len:
        raise CatalogSnapshotError(f"{key}格式不正确")
    return value


def _optional_revision(item: dict[str, Any]) -> str | int:
    value = item.get("revision", "")
    if value is None:
        return ""
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and len(value) <= 120:
        return value
    raise CatalogSnapshotError("revision格式不正确")


def _required_path(item: dict[str, Any], field: str) -> list[str]:
    path = item.get("path")
    if not isinstance(path, list) or not path or not all(isinstance(part, str) and part.strip() for part in path):
        raise CatalogSnapshotError(f"{field}必须是非空文本数组")
    return path


def _required_non_negative_int(item: dict[str, Any], key: str, field: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or value < 0:
        raise CatalogSnapshotError(f"{field}必须是非负整数")
    return value
