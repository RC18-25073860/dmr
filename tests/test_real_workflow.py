from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
# Keep the existing sibling fixture layout as the default, while allowing the
# same real samples to live anywhere on another machine.
REAL_DATA = Path(os.environ.get("DMR_REAL_WORKFLOW_DATA_DIR") or ROOT.parent / "测试").expanduser().resolve()
ARTIFACT = Path(tempfile.gettempdir()) / f"dmr-real-workflow-{os.getpid()}.json"
PORT = 0  # The child binds an OS-assigned port; never reuse an existing service.

BOM_DIR = REAL_DATA / "新增BOM-测试用"
W20_DIR = REAL_DATA / "W20 BOM-不同版本update"
UI20S_OLD = REAL_DATA / "UI20S国内老版本-测试用" / "旧版" / "UI20S_CH_JZ_V1.0.2.00.hex 2025.09.26生效"
UI20S_NEW = REAL_DATA / "UI20S国内老版本-测试用" / "UI20S_CH_JZ_V1.0.2.02.hex 2026.09.18生效"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def generated_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def manifest(paths: list[Path]) -> list[dict]:
    return [{"path": str(path), "name": path.name, "sha256": sha256(path), "size": path.stat().st_size} for path in paths]


class Api:
    def __init__(self, port: int):
        self.port = port
        self.cookie = ''

    def request(self, method: str, path: str, body=None, status=200):
        if method == 'POST' and isinstance(body, dict) and 'expected_edit_token' not in body:
            if path == '/api/review' and body.get('batch_id') and body.get('item_id') and status == 200:
                batch = self.request('GET', '/api/batches/' + body['batch_id'])
                item = next((item for item in batch['items'] if item['id'] == body['item_id']), None)
                if item: body = body | {'expected_edit_token': item['edit_token']}
            elif path.startswith('/api/records/') and path.endswith('/classification') and status != 403:
                record_id = path.split('/')[3]
                record = next((row for row in self.request('GET', '/api/state')['records'] if row['id'] == record_id), None)
                if record: body = body | {'expected_edit_token': record['edit_token']}
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie: headers['Cookie'] = self.cookie
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.getheader('Set-Cookie'): self.cookie = resp.getheader('Set-Cookie').split(';',1)[0]
        conn.close()
        if resp.status != status:
            raise AssertionError({"path": path, "actual_status": resp.status, "expected_status": status, "body": data[:1000].decode("utf-8", "replace")})
        ctype = resp.getheader("Content-Type") or ""
        if "application/json" in ctype:
            return json.loads(data.decode("utf-8"))
        headers = {}
        for key, value in resp.getheaders():
            headers[key] = value
            headers[key.lower()] = value
            headers["-".join(part.capitalize() for part in key.split("-"))] = value
            if key.lower() == "etag":
                headers["ETag"] = value
        return data, headers

    def analyze(self, paths: list[Path]):
        return self.request("POST", "/api/analyze", {"files": [{"name": path.name, "data": b64(path)} for path in paths]})

    def analyze_generated(self, files: list[tuple[str, bytes]]):
        return self.request("POST", "/api/analyze", {"files": [{"name": name, "data": generated_b64(data)} for name, data in files]})

    def commit(self, batch_id: str):
        return self.request("POST", "/api/commit", {"batch_id": batch_id})

    def state(self):
        return self.request("GET", "/api/state")

    def review(self, payload: dict):
        return self.request("POST", "/api/review", payload)

    def sync(self):
        return self.request("POST", "/api/sync", {})

    def download(self, file_id: str):
        return self.request("GET", f"/api/download/{file_id}")


class RealWorkflowTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        missing = [path for path in (REAL_DATA, BOM_DIR, W20_DIR, UI20S_OLD, UI20S_NEW) if not path.is_dir()]
        if missing:
            self.skipTest(
                "真实流程样本目录不存在："
                + ", ".join(str(path) for path in missing)
                + "。请设置 DMR_REAL_WORKFLOW_DATA_DIR 为包含 BOM、W20 和 UI20S 样本的目录。"
            )
        runtime_root = ROOT / "tests" / ".runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(prefix="dmr-real-workflow-", dir=runtime_root)
        self.data_dir = Path(self.temp_dir.name)
        self.log = self.data_dir / "server.log"
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = str(self.data_dir / "pycache")
        self.log_handle = None
        self.proc = self._start_server("wb", env)
        self.report: dict = {"port": PORT, "data_dir": str(self.data_dir), "steps": [], "failures": [], "manifest": {}}
        self._wait_ready()
        self.assertEqual(self.api.state()["records"], [], "workflow must start with its own empty data store")

    def tearDown(self):
        if getattr(self, "proc", None) and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if getattr(self, "log_handle", None) and not self.log_handle.closed:
            self.log_handle.close()
        self.report["server_log"] = str(self.log)
        ARTIFACT.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.temp_dir.cleanup()

    def test_full_api_real_data_workflow(self):
        failures: list[str] = []

        def check(label: str, condition: bool, detail: dict | str):
            entry = {"label": label, "passed": bool(condition), "detail": detail}
            self.report["steps"].append(entry)
            if not condition:
                failures.append(label)
                self.report["failures"].append(entry)

        bom_files = sorted(BOM_DIR.glob("*.xlsx"))
        w20_files = sorted(W20_DIR.glob("*.xlsx"))
        old_files = sorted(path for path in UI20S_OLD.iterdir() if path.is_file() and path.name != ".DS_Store")
        new_files = sorted(path for path in UI20S_NEW.iterdir() if path.is_file() and path.name != ".DS_Store")
        real_files = [path for path in REAL_DATA.rglob("*") if path.is_file() and path.name != ".DS_Store"]
        self.report["manifest"] = {
            "real40": manifest(sorted(real_files)),
            "bom13": manifest(bom_files),
            "w20_6": manifest(w20_files),
            "ui20s_old7": manifest(old_files),
            "ui20s_new7": manifest(new_files),
        }
        check("real manifest is read-only 40 files", len(real_files) == 40, {"count": len(real_files)})
        check("input groups present", (len(bom_files), len(w20_files), len(old_files), len(new_files)) == (13, 6, 7, 7), {"bom": len(bom_files), "w20": len(w20_files), "old": len(old_files), "new": len(new_files)})

        bom_batch = self.api.analyze(bom_files)
        check("real BOM: 12 matched, MI01 LP catalog/filename conflict requires review", bom_batch["summary"] == {"total": 13, "matched": 12, "conflict": 1, "unmatched": 0, "duplicate": 0} and any(i["product"] == "MI01 LP" and i["status"] == "conflict" and i["project_category"] == "待确认" for i in bom_batch["items"]), bom_batch["summary"])
        check("13 finished-product BOMs use trailing filename dates, retaining V1.0 as document version",
              all(item["version"] == re.search(r"(20\d{6})\.xlsx$", item["fileName"]).group(1) for item in bom_batch["items"]),
              {"versions": {item["fileName"]: item["version"] for item in bom_batch["items"]}})
        bom_commit = self.api.commit(bom_batch["batch_id"])
        check("12 real BOM committed; conflicting MI01 LP blocked", bom_commit["committed"] == 12 and bom_commit["blocked"] == 1, bom_commit)

        w20_batch = self.api.analyze(w20_files)
        w20_commit = self.api.commit(w20_batch["batch_id"])
        state_after_w20 = self.api.state()
        w20_record = next((record for record in state_after_w20["records"] if record["no"] == "11W2002C12B" and record["module"] == "BOM"), None)
        w20_files_after_commit = [file for file in state_after_w20["libraryFiles"] if file["recordNo"] == "11W2002C12B"]
        check(
            "W20 C12B archives all filename-date versions and keeps 20260907 current",
            w20_commit["committed"] == 6
            and bool(w20_record)
            and w20_record["version"] == "20260907"
            and w20_record["metadata"].get("version_rule") == "filename_date"
            and w20_record["metadata"].get("document_version") == "V1.0"
            and w20_record["metadata"].get("version_date") == "20260907"
            and sorted(file["version"] for file in w20_files_after_commit) == ["20260814", "20260819", "20260825", "20260826", "20260903", "20260907"]
            and [file["status"] for file in w20_files_after_commit if file["version"] == "20260907"] == ["当前有效"]
            and sum(1 for file in w20_files_after_commit if file["status"] == "历史版本") == 5,
            {"items": _items_summary(w20_batch["items"]), "summary": w20_batch["summary"], "commit": w20_commit, "record": w20_record, "files": w20_files_after_commit},
        )

        old_batch = self.api.analyze(old_files)
        old_commit = self.api.commit(old_batch["batch_id"])
        check("old UI20S commits firmware and holds history plus 5 files for review", _only_firmware_commits(old_batch, old_commit), {"summary": old_batch["summary"], "items": _items_summary(old_batch["items"]), "commit": old_commit})

        new_batch = self.api.analyze(new_files)
        new_commit = self.api.commit(new_batch["batch_id"])
        check("new UI20S commits firmware and holds history plus 5 files for review", _only_firmware_commits(new_batch, new_commit), {"summary": new_batch["summary"], "items": _items_summary(new_batch["items"]), "commit": new_commit})

        dup_batch = self.api.analyze(new_files)
        dup_commit = self.api.commit(dup_batch["batch_id"])
        check("duplicate new UI20S skips firmware and keeps 6 unverified files blocked", dup_commit["committed"] == 0 and _count_result_status(dup_commit, "skipped") == 1 and _count_result_status(dup_commit, "blocked") == 6, {"summary": dup_batch["summary"], "items": _items_summary(dup_batch["items"]), "commit": dup_commit})

        state = self.api.state()
        hex_files = [file for file in state["libraryFiles"] if file["name"] in {"UI20S_CH_JZ_V1.0.2.00.hex", "UI20S_CH_JZ_V1.0.2.02.hex"}]
        current_hex = [file for file in hex_files if file["status"] == "当前有效"]
        historical_hex = [file for file in hex_files if file["status"] == "历史版本"]
        check("main UI20S .00 historical and .02 current", any(f["version"] == "V1.0.2.00" for f in historical_hex) and any(f["version"] == "V1.0.2.02" for f in current_hex), {"hex_files": hex_files})
        check("UI20S old/new linked BOMs preserved per file", _hex_link_map(hex_files) == {"V1.0.2.00": "1120SC6C05", "V1.0.2.02": "1120S09C02B"}, {"link_map": _hex_link_map(hex_files)})

        sync = self.api.sync()
        missing_bom_objects = [change for change in sync["changes"] if change.get("detail") == "关联BOM不存在"]
        check("missing BOM exceptions are visible for linked software records", any(change.get("before") in {"1120SC6C05", "1120S09C02B"} for change in missing_bom_objects), {"changes": missing_bom_objects})

        self._manual_config_check(check)
        self._unsupported_and_malformed_check(check)
        self._download_hash_check(check)
        self._restart_persistence_check(check)
        final_state = self.api.state()
        self.report["final_counts"] = {"records": len(final_state["records"]), "files": len(final_state["libraryFiles"]), "exceptions": len(final_state["exceptions"])}

        self.assertFalse(failures, f"real workflow failures written to {ARTIFACT}: {failures}")

    def _manual_config_check(self, check):
        state = self.api.state()
        context = next((record for record in state["records"] if record["no"] == "1120S09C01B" and record["module"] == "BOM"), None)
        check("context BOM record exists for manual config", context is not None, {"context": context})
        if not context:
            return
        config_file = UI20S_NEW / "UI20S_G2(G3_HardWare)_Config_IC=PY32F031xx8_20260106.V16PY32Config"
        batch = self.api.analyze([config_file])
        item = batch["items"][0]
        reviewed = self.api.review(
            {
                "batch_id": batch["batch_id"],
                "item_id": item["id"],
                "context_record_id": context["id"],
                "code": "CFG-UI20S-PY32-G2",
                "module": "软件",
                "type": "配置文件",
                "version": "V16.0",
                "notes": "generated test review: preserve independent config code while using BOM context",
            }
        )
        commit = self.api.commit(batch["batch_id"])
        state = self.api.state()
        record = next((row for row in state["records"] if row["no"] == "CFG-UI20S-PY32-G2"), None)
        check(
            "manual config context preserves independent code and does not overwrite BOM",
            bool(record and record["no"] != context["no"] and record["linked_bom"] == context["no"] and commit["committed"] == 1),
            {"reviewed": reviewed, "commit": commit, "record": record, "context": context},
        )

    def _unsupported_and_malformed_check(self, check):
        malformed_xlsx = b"generated fixture: not a zip xlsx"
        unsupported = b"generated fixture: unsupported binary"
        batch = self.api.analyze_generated(
            [
                ("generated-malformed.xlsx", malformed_xlsx),
                ("generated-unsupported.bin", unsupported),
            ]
        )
        statuses = {item["fileName"]: item["status"] for item in batch["items"]}
        recommendations = {item["fileName"]: item["recommendation"] for item in batch["items"]}
        check(
            "unsupported and malformed generated files are explicitly blocked",
            statuses == {"generated-malformed.xlsx": "unmatched", "generated-unsupported.bin": "unmatched"} and "解析失败" in recommendations["generated-malformed.xlsx"],
            {"statuses": statuses, "recommendations": recommendations},
        )

    def _download_hash_check(self, check):
        state = self.api.state()
        source_by_name: dict[str, list[str]] = {}
        for group in ("bom13", "w20_6", "ui20s_old7", "ui20s_new7"):
            for entry in self.report["manifest"][group]:
                source_by_name.setdefault(entry["name"], []).append(entry["sha256"])
        mismatches = []
        checked = 0
        for file in state["libraryFiles"]:
            expected = source_by_name.get(file["name"])
            if not expected:
                continue
            payload, headers = self.api.download(file["id"])
            actual = hashlib.sha256(payload).hexdigest()
            checked += 1
            if actual not in expected or headers.get("ETag") != actual:
                mismatches.append({"file": file, "actual": actual, "expected": expected, "etag": headers.get("ETag")})
        check("all committed downloads hash to original bytes", checked == len(state["libraryFiles"]) == 21 and not mismatches, {"checked": checked, "mismatches": mismatches})

    def _restart_persistence_check(self, check):
        before = self.api.state()
        record_count = len(before["records"])
        file_count = len(before["libraryFiles"])
        self.proc.terminate()
        self.proc.wait(timeout=10)
        if self.log_handle and not self.log_handle.closed:
            self.log_handle.close()
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = str(self.data_dir / "pycache")
        self.proc = self._start_server("ab", env)
        self._wait_ready()
        after = self.api.state()
        check("restart preserves SQLite store records and files", len(after["records"]) == record_count and len(after["libraryFiles"]) == file_count, {"before": {"records": record_count, "files": file_count}, "after": {"records": len(after["records"]), "files": len(after["libraryFiles"])}})

    def _wait_ready(self):
        deadline = time.time() + 20
        last_error = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early; log={self.log}")
            try:
                startup = self.log.read_bytes()[self.log_offset:].decode("utf-8", "replace")
                announced = re.search(r"backend running at http://127\.0\.0\.1:(\d+)/", startup)
                if not announced:
                    time.sleep(0.1)
                    continue
                self.api = Api(int(announced.group(1)))
                auth = self.api.request('GET','/api/auth/status')
                if auth['setup_required']:
                    self.api.request('POST','/api/auth/setup',{'display_name':'真实资料测试','username':'workflow-admin','password':'Workflow-2026-Test'})
                else:
                    self.api.request('POST','/api/auth/login',{'username':'workflow-admin','password':'Workflow-2026-Test'})
                self.api.state()
                self.report["port"] = self.api.port
                self.report.setdefault("child_ports", []).append(self.api.port)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError(f"server did not become ready: {last_error}; log={self.log}")

    def _start_server(self, log_mode: str, env: dict):
        self.log_offset = self.log.stat().st_size if log_mode == "ab" and self.log.exists() else 0
        self.log_handle = self.log.open(log_mode)
        return subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(PORT), "--data-dir", str(self.data_dir)],
            cwd=str(ROOT),
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )


def _hex_link_map(files: list[dict]) -> dict[str, str]:
    result = {}
    for file in files:
        version = file.get("version")
        linked = file.get("linked_bom")
        if version and linked:
            result[version] = linked
    return result


def _items_summary(items: list[dict]) -> list[dict]:
    keys = ("fileName", "status", "code", "registrant", "product", "module", "type", "version", "linked_bom", "recommendation")
    return [{key: item.get(key, "") for key in keys} for item in items]


def _only_firmware_commits(batch: dict, commit: dict) -> bool:
    committed = _count_result_status(commit, "committed")
    blocked = _count_result_status(commit, "blocked")
    evidence_backed = [item for item in batch["items"] if item["fileName"].endswith(".hex")]
    manual_pending = [item for item in batch["items"] if item not in evidence_backed]
    return (
        commit["committed"] == 1
        and commit["blocked"] == 6
        and committed == 1
        and blocked == 6
        and len(evidence_backed) == 1
        and all(item["status"] == "matched" for item in evidence_backed)
        and len(manual_pending) == 6
        and all(item["status"] == "unmatched" for item in manual_pending)
    )


def _count_result_status(commit: dict, status: str) -> int:
    return sum(1 for result in commit["results"] if result["status"] == status)


if __name__ == "__main__":
    unittest.main()
