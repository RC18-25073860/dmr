"""Read-only live locator for Feishu DMR catalog rows.

The functions here never write to Feishu.  They only read workbook metadata and
cell values, then resolve the current row and version cell for one file code.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable


READ_TIMEOUT_SECONDS = 45
READ_DEADLINE_SECONDS = 90
MAX_SHEETS = 12
MAX_ROWS_PER_SHEET = 2000
MAX_COLS_PER_SHEET = 80
READ_BATCH_ROWS = 100
CODE_HEADERS = {"编号", "文件编号", "物料编码"}
VERSION_HEADERS = {"版本", "版本号", "文件版本"}


class CatalogLocatorError(RuntimeError):
    """Raised when the live sheet cannot be read safely."""


def column_name(number: int) -> str:
    if number < 1:
        raise ValueError("column numbers are 1-based")
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cell_text(cell: Any) -> str:
    if isinstance(cell, str):
        return cell.strip()
    if isinstance(cell, dict):
        return _cell_text(cell.get("value", cell.get("text", "")))
    if isinstance(cell, list):
        return "".join(_cell_text(part) for part in cell).strip()
    return str(cell).strip() if cell is not None else ""


def _normalize_code(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _cli_executable(config: Any = None) -> str:
    configured = getattr(config, "lark_cli", None) or os.environ.get("LARK_CLI") or "lark-cli"
    resolved = shutil.which(configured) if os.path.basename(configured) == configured else configured
    if not resolved:
        raise CatalogLocatorError("未找到 lark-cli，无法读取飞书清单")
    return resolved


class LarkSheetReader:
    def __init__(
        self,
        executable: str | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: int = READ_TIMEOUT_SECONDS,
    ):
        self.executable = executable or _cli_executable()
        self.runner = runner or subprocess.run
        self.timeout = timeout
        self.deadline: float | None = None

    def call(self, *args: str) -> dict[str, Any]:
        prefix = tuple(args[:2])
        if prefix not in {("sheets", "+workbook-info"), ("sheets", "+cells-get")}:
            raise CatalogLocatorError("清单定位只允许读取飞书表格")
        command = [
            self.executable,
            *args,
            "--as",
            "user",
            "--format",
            "json",
        ]
        env = dict(
            os.environ,
            LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1",
            LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1",
        )
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CatalogLocatorError("读取飞书清单超时，请稍后重试")
            timeout = min(timeout, max(1, int(remaining)))
        try:
            result = self.runner(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CatalogLocatorError("读取飞书清单超时，请稍后重试") from exc
        except OSError as exc:
            raise CatalogLocatorError("无法启动 lark-cli 读取飞书清单") from exc
        if result.returncode:
            raise CatalogLocatorError(_safe_error(result.stderr) or "lark-cli 读取飞书清单失败")
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CatalogLocatorError("lark-cli 返回内容不是有效 JSON") from exc
        if envelope.get("ok") is not True:
            error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
            raise CatalogLocatorError(_safe_error(json.dumps(error, ensure_ascii=False)) or "飞书清单读取未成功")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise CatalogLocatorError("飞书清单读取结果格式异常")
        return data


def _safe_error(raw: str) -> str:
    if not raw:
        return "飞书清单读取失败，请确认权限和网络后重试"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        text = raw
    else:
        error = obj.get("error") if isinstance(obj, dict) else None
        text = json.dumps(error if isinstance(error, dict) else obj, ensure_ascii=False)
    lowered = text.lower()
    if any(word in lowered for word in ("permission", "forbidden", "access denied", "无权限", "没有权限")):
        return "当前账号没有读取这份清单的权限"
    if any(word in lowered for word in ("authorization", "missing_scope", "unauthorized", "scope", "授权")):
        return "飞书授权缺少读取清单权限"
    if any(word in lowered for word in ("timeout", "timed out", "超时")):
        return "读取飞书清单超时，请稍后重试"
    if any(word in lowered for word in ("rate", "too many", "频率", "限流")):
        return "飞书读取频率受限，请稍后重试"
    if any(word in lowered for word in ("not found", "404", "不存在")):
        return "未找到这份飞书清单，请确认链接和权限"
    return "飞书清单读取失败，请确认权限和网络后重试"


def read_live_location(
    document: dict[str, Any],
    code: str,
    *,
    reader: LarkSheetReader | None = None,
    config: Any = None,
    filename: str = "",
    module: str = "",
    max_rows: int = MAX_ROWS_PER_SHEET,
    max_cols: int = MAX_COLS_PER_SHEET,
    batch_rows: int = READ_BATCH_ROWS,
    deadline_seconds: int = READ_DEADLINE_SECONDS,
) -> dict[str, Any]:
    try:
        return _read_live_location(
            document,
            code,
            reader=reader,
            config=config,
            filename=filename, module=module,
            max_rows=max_rows,
            max_cols=max_cols,
            batch_rows=batch_rows,
            deadline_seconds=deadline_seconds,
        )
    except CatalogLocatorError as exc:
        return {
            "status": "read_failed",
            "document_token": document.get("token", ""),
            "document_name": document.get("name", ""),
            "document_url": document.get("url", ""),
            "code": code,
            "read_at": _utc_now(),
            "message": str(exc),
            "matches": [],
            "cloud_writes": 0,
        }


def _read_live_location(
    document: dict[str, Any],
    code: str,
    *,
    reader: LarkSheetReader | None = None,
    config: Any = None,
    filename: str = "",
    module: str = "",
    max_rows: int = MAX_ROWS_PER_SHEET,
    max_cols: int = MAX_COLS_PER_SHEET,
    batch_rows: int = READ_BATCH_ROWS,
    deadline_seconds: int = READ_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Read the current Feishu workbook and locate a unique code row.

    `document` must come from the vetted catalog snapshot in the parent service.
    The function returns only coordinates and values; it performs no cloud write.
    """
    token = document.get("token")
    target = _normalize_code(code)
    if not isinstance(token, str) or not token.strip():
        raise CatalogLocatorError("清单文档缺少 token")
    if not target and not filename.strip():
        raise CatalogLocatorError("请提供文件编号或文件名称")
    live = reader or LarkSheetReader(_cli_executable(config))
    if live.deadline is None:
        live.deadline = time.monotonic() + deadline_seconds
    before = live.call("sheets", "+workbook-info", "--spreadsheet-token", token)
    revision = before.get("revision")
    if revision is None:
        raise CatalogLocatorError("未读取到清单修订号，无法确认位置是否最新，请重试")
    sheets = before.get("sheets")
    if not isinstance(sheets, list):
        raise CatalogLocatorError("飞书清单缺少工作表信息")
    if len(sheets) > MAX_SHEETS:
        raise CatalogLocatorError("工作表数量超过安全读取上限")

    matches: list[dict[str, Any]] = []
    header_problems: list[str] = []
    code_hits: list[dict[str, Any]] = []
    read_at = _utc_now()
    for sheet in sheets:
        sheet_id = sheet.get("sheet_id")
        sheet_name = sheet.get("sheet_name") or sheet.get("title") or ""
        row_count = _positive_int(sheet.get("row_count"), "行数")
        col_count = _positive_int(sheet.get("column_count"), "列数")
        if not sheet_id:
            raise CatalogLocatorError("工作表缺少 sheet_id")
        if row_count > max_rows or col_count > max_cols:
            raise CatalogLocatorError(f"工作表 {sheet_name or sheet_id} 超过安全读取上限")
        rows = _read_sheet_rows(live, token, str(sheet_id), row_count, col_count, revision, batch_rows)
        for number, cells in rows.items():
            for col, value in cells.items():
                if target and _normalize_code(value) == target:
                    code_hits.append({'sheet_name': str(sheet_name), 'sheet_id': str(sheet_id), 'row': number,
                                      'code_cell': f'{col}{number}', 'read_at': read_at})
        located, problems = _locate_in_rows(rows, target, str(sheet_id), str(sheet_name), read_at, filename=filename, module=module)
        matches.extend(located)
        header_problems.extend(problems)

    if module or filename:
        # Scoped/name lookup must not count unrelated categories as unresolved code hits.
        code_hits = [{'sheet_id': m['sheet_id'], 'sheet_name': m['sheet_name'], 'row': m['row'], 'code_cell': m['code_cell']} for m in matches]
    after = live.call("sheets", "+workbook-info", "--spreadsheet-token", token)
    if revision is not None and after.get("revision") != revision:
        raise CatalogLocatorError("定位期间清单被他人修改，请刷新后重新定位")

    base = {
        "document_token": token,
        "document_name": before.get("title") or document.get("name", ""),
        "document_url": document.get("url", ""),
        "code": code,
        "read_at": _utc_now(),
        "revision": revision,
        "code_hits": code_hits,
        "cloud_writes": 0,
    }
    positioned = {(match["sheet_id"], match["code_cell"]) for match in matches}
    unresolved_hits = [hit for hit in code_hits if (hit["sheet_id"], hit["code_cell"]) not in positioned]
    base["problems"] = header_problems
    if unresolved_hits:
        return {
            **base, "status": "ambiguous",
            "message": "找到编号，但部分区域表头不唯一或缺少版本字段，请核对未定位的位置。",
            "matches": matches, "unresolved_hits": unresolved_hits,
        }
    if len(matches) == 1 and matches[0].get("match_basis") in {"code", "name"} and not ((module or filename) and header_problems):
        return {**base, "status": "located", "entry_status": "unique", "location": matches[0]}
    if matches:
        return {
            **base, "status": "ambiguous", "entry_status": "multiple" if len(matches) > 1 else "candidate",
            "message": f"找到 {len(matches)} 个候选位置，请确认名称和适用区域，可分别核对多处。",
            "matches": matches,
        }
    return {**base, "status": "not_found", "entry_status": "missing", "message": "清单已读取，未找到对应条目。请核对文件类别、编号或名称是否变更；确认清单中确实没有后，再人工新增。", "matches": []}


def _positive_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CatalogLocatorError(f"工作表{label}无效") from exc
    if number < 1:
        raise CatalogLocatorError(f"工作表{label}无效")
    return number


def _read_sheet_rows(
    reader: LarkSheetReader,
    token: str,
    sheet_id: str,
    row_count: int,
    col_count: int,
    revision: Any,
    batch_rows: int,
) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    columns = [column_name(index) for index in range(1, col_count + 1)]
    for start in range(1, row_count + 1, batch_rows):
        end = min(row_count, start + batch_rows - 1)
        requested = f"A{start}:{columns[-1]}{end}"
        data = reader.call(
            "sheets",
            "+cells-get",
            "--spreadsheet-token",
            token,
            "--sheet-id",
            sheet_id,
            "--range",
            requested,
            "--include",
            "value,formula",
            "--max-chars",
            "500000",
        )
        if revision is not None and data.get("revision") != revision:
            raise CatalogLocatorError("定位期间清单被他人修改，请刷新后重新定位")
        if data.get("has_more") or not data.get("ranges"):
            raise CatalogLocatorError("飞书清单读取不完整，请缩小范围后重试")
        received: list[int] = []
        for block in data.get("ranges", []):
            if block.get("truncated"):
                raise CatalogLocatorError("飞书清单读取被截断，请缩小范围后重试")
            row_indices = block.get("row_indices", [])
            col_indices = block.get("col_indices", [])
            cells = block.get("cells", [])
            if col_indices != columns:
                raise CatalogLocatorError("飞书清单返回列坐标不完整")
            if len(cells) != len(row_indices) or any(len(row) != len(columns) for row in cells):
                raise CatalogLocatorError("飞书清单返回单元格尺寸异常")
            received.extend(row_indices)
            for number, values in zip(row_indices, cells):
                rows.setdefault(number, {}).update({col: _cell_text(cell) for col, cell in zip(columns, values)})
        if received != list(range(start, end + 1)):
            raise CatalogLocatorError("飞书清单返回行坐标不完整")
    return rows


def _column_number(column: str) -> int:
    value = 0
    for char in column:
        value = value * 26 + ord(char) - ord("A") + 1
    return value


def _header_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _region_title(rows, header_row, start, end):
    # Titles are descriptive only; matching always uses physical code/version columns.
    for number in sorted((n for n in rows if max(1, header_row - 15) <= n < header_row)):
        values = [value for col, value in rows[number].items() if start <= _column_number(col) <= end and value]
        if any(_header_text(v) in {"项目名称", "型号", "地区", "名称", "编号", "版本"} for v in values):
            continue
        for value in values:
            if 5 <= len(value) <= 120 and any(word in value for word in ("国内", "海外", "医疗版", "家电版")):
                return value
    return f"{column_name(start)}–{column_name(end)} 列区域"


def _region_area(rows, header_row, start, end):
    for number in range(header_row - 1, max(0, header_row - 10), -1):
        cells = rows.get(number, {})
        labels = [col for col, value in cells.items() if start <= _column_number(col) <= end and _header_text(value) in {"地区", "区域", "市场"}]
        if labels:
            label_column = min(_column_number(col) for col in labels)
            return " / ".join(value for col, value in sorted(cells.items(), key=lambda pair: _column_number(pair[0])) if label_column < _column_number(col) <= end and value)
    return ""


def _locate_in_rows(
    rows: dict[int, dict[str, str]],
    target: str,
    sheet_id: str,
    sheet_name: str,
    read_at: str,
    *, filename: str = "", module: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    matches: list[dict[str, Any]] = []
    problems: list[str] = []
    regions: dict[str, dict[str, Any]] = {}
    last_column = max((_column_number(c) for cells in rows.values() for c in cells), default=1)
    for number in sorted(rows):
        cells = rows[number]
        code_cols = sorted((col for col, value in cells.items() if _header_text(value) in CODE_HEADERS), key=_column_number)
        version_cols = sorted((col for col, value in cells.items() if _header_text(value) in VERSION_HEADERS), key=_column_number)
        names_at_header = sorted((col for col, value in cells.items() if _header_text(value) in {"名称", "文件名称", "文件名"}), key=_column_number)
        actual_code_cols = set(code_cols)
        if not code_cols and names_at_header and version_cols:
            code_cols = names_at_header
        if code_cols:
            anchors = sorted((_column_number(col) for col, value in cells.items() if _header_text(value) in {"序号", "名称", "文件名称"}))
            starts = []
            previous_code = 0
            for code_col in code_cols:
                code_number = _column_number(code_col)
                candidates = [anchor for anchor in anchors if previous_code < anchor < code_number]
                # A repeated header in one side of a worksheet must not reset the other side.
                preceding_versions = [_column_number(col) for col in version_cols if previous_code < _column_number(col) < code_number] if previous_code else []
                fallback_start = max(preceding_versions, default=previous_code) + 1
                start = min(candidates) if candidates else regions.get(code_col, {}).get("start", fallback_start)
                starts.append(start)
                previous_code = code_number
            for index, code_col in enumerate(code_cols):
                code_number = _column_number(code_col)
                start = starts[index]
                end = starts[index + 1] - 1 if index + 1 < len(starts) else last_column
                if len(code_cols) == 1 and code_col in regions:
                    end = regions[code_col]["end"]
                versions = [col for col in version_cols if code_number < _column_number(col) <= end]
                remarks = [_column_number(col) for col, value in cells.items() if _header_text(value) == "备注" and code_number < _column_number(col) <= end]
                if len(remarks) == 1 and (not versions or remarks[0] > max(_column_number(col) for col in versions)):
                    end = remarks[0]
                # Replace only headers in this horizontal region; never pair with a neighbour.
                for old in list(regions):
                    if start <= _column_number(old) <= end:
                        del regions[old]
                name_columns = [col for col in names_at_header if start <= _column_number(col) <= end]
                regions[code_col] = {"name_column": name_columns[0] if len(name_columns) == 1 else None, "has_code": code_col in actual_code_cols, "module": "", "version": None, "start": start, "end": end, "header_row": number, "title": _region_title(rows, number, start, end), "area": _region_area(rows, number, start, end)}
                if len(versions) != 1:
                    problems.append(f"{sheet_name or sheet_id} 第 {number} 行 {column_name(start)}–{column_name(end)} 区域：编号列 {code_col} 对应 {len(versions)} 个版本表头")
                    continue
                regions[code_col]["version"] = versions[0]
        elif version_cols:
            for col in version_cols:
                col_number = _column_number(col)
                for code_col, region in list(regions.items()):
                    if region["start"] <= col_number <= region["end"]:
                        region["version"] = None
                problems.append(f"{sheet_name or sheet_id} 第 {number} 行 {col} 列出现版本表头，但同区域缺少编号表头")
        for code_col, region in regions.items():
            region_values = [v for c, v in cells.items() if region['start'] <= _column_number(c) <= region['end'] and v.strip()]
            if len(region_values) == 1:
                section = category_name(region_values[0])
                if section:
                    region['module'] = section
                    continue
            if not region["version"] or (module and region.get('module') != module):
                continue
            name_col = region.get('name_column')
            file_name = cells.get(name_col, '') if name_col else ''
            raw_code = cells.get(code_col, '') if region.get('has_code') else ''
            basis = 'code' if target and _normalize_code(raw_code) == target else ''
            if not basis and filename and file_name:
                if exact_name(file_name) == exact_name(filename):
                    basis = 'name'
                elif stable_name(file_name) and stable_name(file_name) == stable_name(filename):
                    basis = 'normalized_name'
                elif keyword_name_matches(file_name, filename, raw_code, target):
                    basis = 'keyword_name'
                # A changed identifier is evidence to review, never an automatic replacement.
                if basis and target and raw_code.strip() not in {'', '/', '／', '-', '—'}:
                    basis = 'name_changed_code'
            if not basis:
                continue
            matched_col = code_col if basis == 'code' else name_col
            version_col = region["version"]
            matches.append({
                "sheet_name": sheet_name, "sheet_id": sheet_id, "row": number,
                "code_column": code_col, "code_cell": f"{matched_col}{number}",
                "name_cell": f"{name_col}{number}" if name_col else "", "file_name": file_name,
                "current_code": raw_code,
                "module": region.get("module", ""), "match_basis": basis,
                "requires_manual_confirmation": basis not in {"code", "name"},
                "version_column": version_col, "version_cell": f"{version_col}{number}",
                "current_version": cells.get(version_col, ""), "read_at": read_at,
                "header_row": region["header_row"], "region_title": region["title"], "region_area": region["area"],
                "region_start_column": column_name(region["start"]), "region_end_column": column_name(region["end"]),
            })
    return sorted(matches, key=lambda match: (match["row"], _column_number(match["code_column"]))), problems


def category_name(value):
    text = _header_text(value).upper()
    labels = {'BOM': ('BOM', '物料清单'), '软件': ('软件',), '工艺文件': ('工艺文件',),
              '检验文件': ('检验文件',), '承认书&规格书': ('承认书&规格书', '承认书', '规格书'),
              '包装图纸': ('包装图纸',), '结构图纸': ('结构图纸',)}
    for module, names in labels.items():
        if any(text == name or text.startswith(name + '（') or text.startswith(name + '(') for name in names):
            return module
    return ''


def exact_name(value):
    return re.sub(r'\s+', '', str(value)).casefold().strip('《》')


def stable_name(value):
    # Remove only explicit versions, dates, and known extensions, never model digits.
    text = re.sub(r'\.(xlsx?|docx?|pdf|hex|zip|socprj|dwg|dxf|step|stp|txt|v16py32config)$', '', str(value), flags=re.I)
    text = re.sub(r'(?<![A-Za-z0-9])[VAB]\d+(?:\.\d+)*(?![A-Za-z0-9])', '', text, flags=re.I)
    text = re.sub(r'(?<!\d)20\d{2}[-_.]?\d{2}[-_.]?\d{2}(?!\d)', '', text)
    text = re.sub(r'[_—-]+', '_', text)
    return exact_name(text).strip('_-— ')


def keyword_name_matches(name, query, current_code='', uploaded_code=''):
    """Substring candidates after removing explicit version and identifier prefixes.

    Never treat a model alone as a file name: that would mark an entire module.
    This is discovery only; callers must retain the candidate status.
    """
    def searchable(value, code):
        value = str(value).strip()
        if code and value.casefold().startswith(code.casefold()):
            suffix = value[len(code):]
            if suffix[:1] in {'_', '-', ' ', '—'}:
                value = suffix.lstrip('_-— ')
        return re.sub(r'[_\s—-]+', '', stable_name(value))
    left, right = searchable(name, current_code), searchable(query, uploaded_code)
    if not left or not right:
        return False
    shorter = min((left, right), key=len)
    if len(shorter) < 4 or re.fullmatch(r'[a-z]+\d+[a-z0-9]*', shorter):
        return False
    return left in right or right in left
