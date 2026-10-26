import base64
import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import zipfile
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("dmr_server_under_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def xlsx_bom(code: str, version: str, product: str, registrant: str, salt: str = "") -> bytes:
    headers = ["BOM层级", "父项编码", "BOM版本", "规格型号", "物料类别", "备注"]
    root = ["0", code, version, f"{product}[测试产品]【{registrant}】", "BOM", salt]

    def cell(col, row, value):
        return f'<c r="{col}{row}" t="inlineStr"><is><t>{value}</t></is></c>'

    rows = []
    for idx, values in enumerate([headers, root], start=1):
        cols = "".join(cell(chr(ord("A") + i), idx, value) for i, value in enumerate(values))
        rows.append(f'<row r="{idx}">{cols}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
    )
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return out.getvalue()


def xlsx_with_sheet_xml(sheet_xml: str) -> bytes:
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return out.getvalue()


class Harness:
    def __init__(self, data_dir: Path):
        self.server_mod = load_server()
        self.server = self.server_mod.build_server(0, data_dir)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.closed = False
        self.cookie = ''
        if os.environ.get('DMR_AUTH_MODE', 'local') == 'local':
            status = self.request('GET', '/api/auth/status')
            if status['setup_required']:
                self.request('POST', '/api/auth/setup', {'display_name':'测试管理员','username':'fixture-admin','password':'Fixture-Admin-2026'})
            else:
                self.request('POST', '/api/auth/login', {'username':'fixture-admin','password':'Fixture-Admin-2026'})
            roles = set(os.environ.get('DMR_LOCAL_ROLES','admin').split(','))
            if 'admin' not in roles:
                role = next((r for r in ('document_control','uploader','reader') if r in roles), 'reader')
                self.request('POST','/api/users',{'username':'fixture-user','display_name':'测试用户','password':'Initial-Fixture-2026','role':role,'active':True,'scopes':[{'registrant':'*','project_category':'*','product':'*'}],'can_download':True,'can_history':True})
                self.request('POST','/api/auth/login',{'username':'fixture-user','password':'Initial-Fixture-2026'})
                self.request('POST','/api/auth/change-password',{'current_password':'Initial-Fixture-2026','new_password':'Fixture-User-2026'})
                self.request('POST','/api/auth/login',{'username':'fixture-user','password':'Fixture-User-2026'})

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        if self.server_mod.STORE:
            self.server_mod.STORE.close()

    def request(self, method, path, body=None, status=200, headers=None):
        if method == 'POST' and isinstance(body, dict) and 'expected_edit_token' not in body:
            if path == '/api/review' and body.get('batch_id') and body.get('item_id') and status == 200:
                batch = self.request('GET', '/api/batches/' + body['batch_id'])
                item = next((item for item in batch['items'] if item['id'] == body['item_id']), None)
                if item: body = body | {'expected_edit_token': item['edit_token']}
            elif path.startswith('/api/records/') and path.endswith('/classification') and status != 403:
                record_id = path.split('/')[3]
                record = next((row for row in self.request('GET', '/api/state')['records'] if row['id'] == record_id), None)
                if record: body = body | {'expected_edit_token': record['edit_token']}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie: request_headers["Cookie"] = self.cookie
        if headers:
            request_headers.update(headers)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, request_headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.getheader('Set-Cookie'): self.cookie = resp.getheader('Set-Cookie').split(';',1)[0]
        conn.close()
        if resp.status != status:
            raise AssertionError((resp.status, status, data[:500]))
        ctype = resp.getheader("Content-Type") or ""
        if "application/json" in ctype:
            return json.loads(data.decode("utf-8"))
        headers = []
        for key, value in resp.getheaders():
            headers.append((key, value))
            headers.append(("-".join(part.capitalize() for part in key.split("-")), value))
            if key.lower() == "etag":
                headers.append(("ETag", value))
        return data, headers

    def raw(self, method, path, payload=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie: request_headers["Cookie"] = self.cookie
        if headers:
            request_headers.update(headers)
        conn.request(method, path, payload, request_headers)
        resp = conn.getresponse()
        data = resp.read()
        all_headers = []
        for key, value in resp.getheaders():
            all_headers.append((key, value))
            all_headers.append(("-".join(part.capitalize() for part in key.split("-")), value))
        conn.close()
        return resp.status, data, all_headers


def with_server(fn):
    with tempfile.TemporaryDirectory(dir=ROOT / "tests") as tmp:
        harness = Harness(Path(tmp))
        try:
            fn(harness, Path(tmp))
        finally:
            harness.close()


class BackendTests(unittest.TestCase):
    def test_full_log_history_remains_queryable(self):
        def run(h, data_dir):
            store = h.server_mod.STORE
            with store.lock, store.conn:
                for n in range(505):
                    store.add_log("测试历史", str(n), "自动生成的边界测试日志", "TEST-LOG", "成功")
            self.assertEqual(len(store.state()["operationLogs"]), 200)
            pages = [h.request("GET", f"/api/logs?limit=200&offset={offset}") for offset in (0, 200, 400)]
            self.assertEqual(pages[0]["total"], 505)
            self.assertEqual(len({row["object"] for page in pages for row in page["items"]}), 505)
            self.assertFalse(pages[-1]["has_more"])
        with_server(run)

    def test_repeat_upload_persists_download_and_history(self):
        def run(h, data_dir):
            first_bytes = xlsx_bom("BOM-100", "V1.0", "W20", "深圳金茉", "first")
            first = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-100_V1.0-W20【深圳金茉】-部件.xlsx", "data": b64(first_bytes)}]})
            self.assertEqual(first["summary"]["matched"], 1)
            self.assertEqual(h.request("POST", "/api/commit", {"batch_id": first["batch_id"]})["committed"], 1)
            state = h.request("GET", "/api/state")
            record = next(r for r in state["records"] if r["no"] == "BOM-100")
            file1 = next(f for f in state["libraryFiles"] if f["recordId"] == record["id"] and f["status"] == "当前有效")
            payload, headers = h.request("GET", f"/api/download/{file1['id']}")
            self.assertEqual(payload, first_bytes)
            self.assertEqual(dict(headers)["ETag"], file1["sha256"])

            second = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-100_V1.1-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-100", "V1.1", "W20", "深圳金茉", "second"))}]})
            self.assertEqual(second["items"][0]["status"], "matched")
            self.assertEqual(h.request("POST", "/api/commit", {"batch_id": second["batch_id"]})["committed"], 1)
            state2 = h.request("GET", "/api/state")
            files = [f for f in state2["libraryFiles"] if f["recordId"] == record["id"]]
            self.assertEqual(sorted(f["status"] for f in files), ["历史版本", "当前有效"])

            h.close()
            h2 = Harness(data_dir)
            try:
                persisted = h2.request("GET", "/api/state")
                persisted_record = next(r for r in persisted["records"] if r["no"] == "BOM-100")
                self.assertEqual(persisted_record["version"], "V1.1")
                self.assertEqual(len([f for f in persisted["libraryFiles"] if f["recordId"] == persisted_record["id"]]), 2)
            finally:
                h2.close()

        with_server(run)

    def test_version_gates_same_hash_diff_hash_downgrade_and_same_batch_highest(self):
        def run(h, _data_dir):
            v2 = xlsx_bom("BOM-200", "V2.0", "W20", "深圳金茉", "v2")
            initial = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-200_V2.0-W20【深圳金茉】-部件.xlsx", "data": b64(v2)}]})
            self.assertEqual(h.request("POST", "/api/commit", {"batch_id": initial["batch_id"]})["committed"], 1)

            duplicate = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-200_V2.0-W20【深圳金茉】-部件.xlsx", "data": b64(v2)}]})
            self.assertEqual(duplicate["items"][0]["status"], "duplicate")
            downgrade = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-200_V1.9-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-200", "V1.9", "W20", "深圳金茉", "old"))}]})
            self.assertEqual(downgrade["items"][0]["status"], "conflict")
            same_diff = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-200_V2.0-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-200", "V2.0", "W20", "深圳金茉", "different"))}]})
            self.assertEqual(same_diff["items"][0]["status"], "conflict")

            batch = h.request(
                "POST",
                "/api/analyze",
                {
                    "files": [
                        {"name": "BOM-200_V2.1-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-200", "V2.1", "W20", "深圳金茉", "v21"))},
                        {"name": "BOM-200_V2.10-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-200", "V2.10", "W20", "深圳金茉", "v210"))},
                    ]
                },
            )
            self.assertEqual([item["status"] for item in batch["items"]], ["matched", "matched"])
            committed = h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})
            self.assertEqual(committed["committed"], 1)
            self.assertTrue(any(r["status"] == "skipped" for r in committed["results"]))
            state = h.request("GET", "/api/state")
            self.assertEqual(next(r for r in state["records"] if r["no"] == "BOM-200")["version"], "V2.10")

        with_server(run)

    def test_manual_review_context_exception_gate_and_missing_bom_note(self):
        def run(h, _data_dir):
            bom = h.request("POST", "/api/analyze", {"files": [{"name": "1120S09C01B_V1.0-UI20S GR【广西由莱】-部件.xlsx", "data": b64(xlsx_bom("1120S09C01B", "V1.0", "UI20S GR", "广西由莱", "bom"))}]})
            h.request("POST", "/api/commit", {"batch_id": bom["batch_id"]})
            state = h.request("GET", "/api/state")
            bom_record = next(r for r in state["records"] if r["no"] == "1120S09C01B")

            unknown = h.request("POST", "/api/analyze", {"files": [{"name": "UI20S_CH_JZ项目软件履历表.pdf", "data": b64(b"%PDF-1.4 history")}]})
            item = unknown["items"][0]
            self.assertIn(item["status"], {"unmatched", "matched"})
            reviewed = h.request(
                "POST",
                "/api/review",
                {
                    "batch_id": unknown["batch_id"],
                    "item_id": item["id"],
                    "context_record_id": bom_record["id"],
                    "code": "SOFT-UI20S-CH-JZ",
                    "module": "软件",
                    "version": "V1.0.2.02",
                    "linked_bom": "1120S09C02B",
                },
            )
            self.assertEqual(reviewed["item"]["status"], "matched")
            self.assertEqual(reviewed["item"]["registrant"], bom_record["registrant"])
            self.assertEqual(h.request("POST", "/api/commit", {"batch_id": unknown["batch_id"]})["committed"], 1)
            state2 = h.request("GET", "/api/state")
            soft = next(r for r in state2["records"] if r["no"] == "SOFT-UI20S-CH-JZ")
            self.assertEqual(soft["module"], "软件")
            self.assertEqual(soft["linked_bom"], "1120S09C02B")
            current_file = next(f for f in state2["libraryFiles"] if f["recordId"] == soft["id"] and f["status"] == "当前有效")
            self.assertEqual(current_file["link_status"], "BOM缺失")

            bad = h.request("POST", "/api/analyze", {"files": [{"name": "完全未知资料.zip", "data": b64(b"zip")}]})
            ex = next(e for e in h.request("GET", "/api/state")["exceptions"] if e["itemId"] == bad["items"][0]["id"])
            h.request("POST", f"/api/exceptions/{ex['id']}/resolve", {}, status=400)

        with_server(run)

    def test_cross_subject_same_code_keeps_records_separate(self):
        def run(h, _data_dir):
            batch = h.request(
                "POST",
                "/api/analyze",
                {
                    "files": [
                        {"name": "BOM-300_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-300", "V1.0", "ZZ99", "深圳金茉", "a"))},
                        {"name": "BOM-300_V1.0-ZZ99【广西由莱】-部件.xlsx", "data": b64(xlsx_bom("BOM-300", "V1.0", "ZZ99", "广西由莱", "b"))},
                    ]
                },
            )
            self.assertTrue(all(i["status"] == "matched" for i in batch["items"]))
            self.assertEqual(h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})["committed"], 2)
            state = h.request("GET", "/api/state")
            records = [r for r in state["records"] if r["no"] == "BOM-300"]
            self.assertEqual(sorted(r["registrant"] for r in records), ["广西由莱", "深圳金茉"])

        with_server(run)

    def test_security_json_loopback_and_static_data_denied(self):
        def run(h, _data_dir):
            h.request("GET", "/.runtime/dmr.sqlite3", status=404)
            h.request("POST", "/api/sync", {}, headers={"Origin": "https://example.com"}, status=403)
            status, body, _headers = h.raw("POST", "/api/sync", b"{}", {"Content-Type": "text/plain"})
            self.assertEqual(status, 400, body[:200])
            status, _body, _headers = h.raw("POST", "/api/sync", b"{}", {"Content-Type": "application/jsonx"})
            self.assertEqual(status, 400)
            status, _body, _headers = h.raw("POST", "/api/sync", b"", {"Content-Type": "application/json", "Content-Length": str(h.server_mod.MAX_BODY + 1)})
            self.assertEqual(status, 400)
            if hasattr(os, "symlink"):
                link = Path(_data_dir) / "escape.js"
                os.symlink("/etc/passwd", link)
                rel = link.relative_to(ROOT)
                h.server_mod.STATIC_ALLOWLIST["/escape.js"] = str(rel)
                try:
                    h.request("GET", "/escape.js", status=404)
                finally:
                    h.server_mod.STATIC_ALLOWLIST.pop("/escape.js", None)

        with_server(run)


    def test_explicit_data_dir_ignores_external_database_url(self):
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{ROOT / 'data' / 'dmr.sqlite3'}"
        try:
            def run(h, _data_dir):
                state = h.request("GET", "/api/state")
                self.assertEqual(state["records"], [])
                batch = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-ENV_V1.0-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-ENV", "V1.0", "W20", "深圳金茉", "env"))}]})
                self.assertEqual(h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})["committed"], 1)
                scoped = h.request("GET", "/api/state")
                self.assertEqual(len(scoped["records"]), 1)
            with_server(run)
        finally:
            if old_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_url

    def test_local_reader_cannot_write_and_enterprise_mode_fails_closed(self):
        old_roles = os.environ.get("DMR_LOCAL_ROLES")
        old_mode = os.environ.get("DMR_AUTH_MODE")
        try:
            os.environ["DMR_LOCAL_ROLES"] = "reader"
            def reader_run(h, _data_dir):
                self.assertEqual(h.request("GET", "/api/state")["records"], [])
                h.request("POST", "/api/analyze", {"files": []}, status=403)
            with_server(reader_run)

            os.environ["DMR_AUTH_MODE"] = "enterprise"
            os.environ["DMR_LOCAL_ROLES"] = "admin"
            def enterprise_run(h, _data_dir):
                h.request("GET", "/api/state", status=503)
                caps = h.request("GET", "/api/capabilities")
                self.assertFalse(caps["auth"]["localMode"])
            with_server(enterprise_run)
        finally:
            if old_roles is None:
                os.environ.pop("DMR_LOCAL_ROLES", None)
            else:
                os.environ["DMR_LOCAL_ROLES"] = old_roles
            if old_mode is None:
                os.environ.pop("DMR_AUTH_MODE", None)
            else:
                os.environ["DMR_AUTH_MODE"] = old_mode


    def test_parse_error_cannot_be_bypassed_by_review(self):
        def run(h, _data_dir):
            analyzed = h.request("POST", "/api/analyze", {"files": [{"name": "坏文件_V1.0.xlsx", "data": b64(b"not a zip")}]})
            item = analyzed["items"][0]
            self.assertEqual(item["status"], "unmatched")
            self.assertIn("parse_error", item["metadata"])
            reviewed = h.request(
                "POST",
                "/api/review",
                {
                    "batch_id": analyzed["batch_id"],
                    "item_id": item["id"],
                    "code": "BAD-XLSX",
                    "registrant": "深圳金茉",
                    "product": "W20",
                    "module": "BOM",
                    "type": "BOM",
                    "version": "V1.0",
                },
            )
            self.assertEqual(reviewed["item"]["status"], "unmatched")
            committed = h.request("POST", "/api/commit", {"batch_id": analyzed["batch_id"]})
            self.assertEqual(committed["committed"], 0)
            self.assertEqual(h.request("GET", "/api/state")["records"], [])

        with_server(run)

    def test_same_batch_same_version_different_hash_conflicts_during_analyze(self):
        def run(h, _data_dir):
            analyzed = h.request(
                "POST",
                "/api/analyze",
                {
                    "files": [
                        {"name": "BOM-W20_V1.0-W20【深圳金茉】-20260901.xlsx", "data": b64(xlsx_bom("BOM-W20", "V1.0", "W20", "深圳金茉", "a"))},
                        {"name": "BOM-W20_V1.0-W20【深圳金茉】-20260902.xlsx", "data": b64(xlsx_bom("BOM-W20", "V1.0", "W20", "深圳金茉", "b"))},
                    ]
                },
            )
            self.assertEqual([item["status"] for item in analyzed["items"]], ["conflict", "conflict"])
            state = h.request("GET", "/api/state")
            self.assertEqual(len([e for e in state["exceptions"] if e["batchId"] == analyzed["batch_id"]]), 2)
            committed = h.request("POST", "/api/commit", {"batch_id": analyzed["batch_id"]})
            self.assertEqual(committed["committed"], 0)

        with_server(run)

    def test_new_letter_version_commits_without_v0_orphan_and_tamper_rolls_back(self):
        def run(h, data_dir):
            reviewed = h.request("POST", "/api/analyze", {"files": [{"name": "INS-AIR3-027_B04.pdf", "data": b64(b"%PDF-1.4 inspection")}]})
            item = reviewed["items"][0]
            fixed = h.request(
                "POST",
                "/api/review",
                {
                    "batch_id": reviewed["batch_id"],
                    "item_id": item["id"],
                    "code": "INS-AIR3-027",
                    "registrant": "深圳金茉",
                    "product": "Air 3",
                    "module": "检验文件",
                    "type": "检验文件",
                    "version": "B04",
                },
            )
            self.assertEqual(fixed["item"]["status"], "matched")
            self.assertEqual(h.request("POST", "/api/commit", {"batch_id": reviewed["batch_id"]})["committed"], 1)
            state = h.request("GET", "/api/state")
            record = next(r for r in state["records"] if r["no"] == "INS-AIR3-027")
            self.assertEqual(record["version"], "B04")
            self.assertNotEqual(record["previous"], "V0")

            tampered = h.request("POST", "/api/analyze", {"files": [{"name": "INS-AIR3-028_B04.pdf", "data": b64(b"%PDF-1.4 new")}]})
            tamper_item = tampered["items"][0]
            h.request(
                "POST",
                "/api/review",
                {
                    "batch_id": tampered["batch_id"],
                    "item_id": tamper_item["id"],
                    "code": "INS-AIR3-028",
                    "registrant": "深圳金茉",
                    "product": "Air 3",
                    "module": "检验文件",
                    "type": "检验文件",
                    "version": "B04",
                },
            )
            blob = data_dir / tamper_item["metadata"]["blob_path"]
            blob.write_bytes(b"tampered")
            blocked = h.request("POST", "/api/commit", {"batch_id": tampered["batch_id"]})
            self.assertEqual(blocked["committed"], 0)
            self.assertEqual(blocked["blocked"], 1)
            failed_item = h.request("GET", "/api/batches/" + tampered["batch_id"])["items"][0]
            self.assertEqual(failed_item["status"], "conflict")
            self.assertIn("校验失败", failed_item["result"])
            state_after = h.request("GET", "/api/state")
            self.assertFalse(any(r["no"] == "INS-AIR3-028" for r in state_after["records"]))

        with_server(run)

    def test_download_preview_hash_and_preview_xml_bounds(self):
        def run(h, data_dir):
            first = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-PREVIEW_V1.0-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-PREVIEW", "V1.0", "W20", "深圳金茉", "preview"))}]})
            h.request("POST", "/api/commit", {"batch_id": first["batch_id"]})
            state = h.request("GET", "/api/state")
            file_id = next(f for f in state["libraryFiles"] if f["name"].startswith("BOM-PREVIEW"))["id"]
            preview, headers = h.request("GET", f"/api/preview/{file_id}")
            self.assertIn(b"<table", preview)
            self.assertIn("text/html", dict(headers)["Content-Type"])

            blob = next((data_dir / "files").iterdir())
            blob.write_bytes(b"tampered")
            h.request("GET", f"/api/download/{file_id}", status=409)
            h.request("GET", f"/api/preview/{file_id}", status=409)

            unsafe = xlsx_with_sheet_xml('<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"></worksheet>')
            direct = h.server_mod.preview_xlsx(unsafe, "unsafe.xlsx")
            self.assertIn("DOCTYPE", direct)

        with_server(run)

    def test_sync_detects_tamper_and_requires_real_fix_before_close(self):
        def run(h, data_dir):
            batch = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-TAMPER_V1.0-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-TAMPER", "V1.0", "W20", "深圳金茉", "ok"))}]})
            h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})
            state = h.request("GET", "/api/state")
            file_id = next(f for f in state["libraryFiles"] if f["name"].startswith("BOM-TAMPER"))["id"]
            blob = next((data_dir / "files").iterdir())
            original = blob.read_bytes()
            blob.write_bytes(b"tampered")

            sync = h.request("POST", "/api/sync", {})
            self.assertTrue(any(change["detail"] == "本地附件校验失败" for change in sync["changes"]))
            ex = next(e for e in h.request("GET", "/api/state")["exceptions"] if e["itemId"] == file_id and e["type"] == "文件校验失败")
            h.request("POST", f"/api/exceptions/{ex['id']}/resolve", {}, status=400)

            blob.write_bytes(original)
            h.request("POST", "/api/sync", {})
            closed = h.request("POST", f"/api/exceptions/{ex['id']}/resolve", {})
            self.assertTrue(closed["resolved"])
            ex_after = next(e for e in h.request("GET", "/api/state")["exceptions"] if e["id"] == ex["id"])
            self.assertEqual(ex_after["status"], "resolved")

        with_server(run)

    def test_sync_refreshes_bom_link_after_matching_bom_import(self):
        def run(h, _data_dir):
            sw = h.request("POST", "/api/analyze", {"files": [{"name": "SOFT-W20_V1.0.pdf", "data": b64(b"%PDF-1.4 software")}]})
            item = sw["items"][0]
            h.request(
                "POST",
                "/api/review",
                {
                    "batch_id": sw["batch_id"],
                    "item_id": item["id"],
                    "code": "SOFT-W20",
                    "registrant": "深圳金茉",
                    "product": "W20",
                    "module": "软件",
                    "type": "软件资料",
                    "version": "V1.0",
                    "linked_bom": "BOM-LATE",
                },
            )
            h.request("POST", "/api/commit", {"batch_id": sw["batch_id"]})
            first_sync = h.request("POST", "/api/sync", {})
            self.assertTrue(any(change["before"] == "BOM-LATE" and change["detail"] == "关联BOM不存在" for change in first_sync["changes"]))
            state = h.request("GET", "/api/state")
            soft_file = next(f for f in state["libraryFiles"] if f["recordNo"] == "SOFT-W20")
            ex = next(e for e in state["exceptions"] if e["itemId"] == soft_file["id"] and e["type"] == "BOM关联异常")
            self.assertEqual(soft_file["link_status"], "BOM缺失")

            bom = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-LATE_V1.0-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-LATE", "V1.0", "W20", "深圳金茉", "late"))}]})
            h.request("POST", "/api/commit", {"batch_id": bom["batch_id"]})
            h.request("POST", "/api/sync", {})
            state2 = h.request("GET", "/api/state")
            soft_file2 = next(f for f in state2["libraryFiles"] if f["recordNo"] == "SOFT-W20")
            self.assertEqual(soft_file2["link_status"], "已关联")
            ex_after = next(e for e in state2["exceptions"] if e["id"] == ex["id"])
            self.assertEqual(ex_after["status"], "resolved")

        with_server(run)

    def test_sync_bom_link_rejects_cross_subject_mismatch(self):
        def run(h, _data_dir):
            bom = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-CROSS_V1.0-ZZ99【广西由莱】-部件.xlsx", "data": b64(xlsx_bom("BOM-CROSS", "V1.0", "ZZ99", "广西由莱", "cross"))}]})
            h.request("POST", "/api/commit", {"batch_id": bom["batch_id"]})
            sw = h.request("POST", "/api/analyze", {"files": [{"name": "SOFT-CROSS_V1.0.pdf", "data": b64(b"%PDF-1.4 software")}]})
            item = sw["items"][0]
            h.request(
                "POST",
                "/api/review",
                {
                    "batch_id": sw["batch_id"],
                    "item_id": item["id"],
                    "code": "SOFT-CROSS",
                    "registrant": "深圳金茉",
                    "product": "ZZ99",
                    "module": "软件",
                    "type": "软件资料",
                    "version": "V1.0",
                    "linked_bom": "BOM-CROSS",
                },
            )
            h.request("POST", "/api/commit", {"batch_id": sw["batch_id"]})
            sync = h.request("POST", "/api/sync", {})
            self.assertTrue(any(change["before"] == "BOM-CROSS" and change["detail"] == "BOM主体/产品不匹配" for change in sync["changes"]))
            state = h.request("GET", "/api/state")
            soft_file = next(f for f in state["libraryFiles"] if f["recordNo"] == "SOFT-CROSS")
            self.assertEqual(soft_file["link_status"], "BOM主体/产品不匹配")
            ex = next(e for e in state["exceptions"] if e["itemId"] == soft_file["id"] and e["type"] == "BOM关联异常")
            h.request("POST", f"/api/exceptions/{ex['id']}/resolve", {}, status=400)

        with_server(run)


if __name__ == "__main__":
    unittest.main()
