from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.config import AppConfig, ROOT, load_config
from backend.db import create_dmr_engine
from backend.service import DMRStore
from server import create_app


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
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rows)}</sheetData></worksheet>",
        )
    return out.getvalue()


def config_for(data_dir: Path, *, user: str = "atomic-user", roles: str = "admin,document_control,reader,uploader") -> AppConfig:
    return AppConfig(
        root=ROOT,
        data_dir=data_dir,
        database_url=None,
        auth_mode="local",
        static_root=ROOT / "frontend" / "dist",
        local_user=user,
        local_roles=roles,
    )


def authenticate(client, user="atomic-user", roles="admin,document_control,reader,uploader"):
    credentials = {"username":"atomic-admin","password":"Atomic-Admin-2026"}
    if client.get("/api/auth/status").json()["setup_required"]:
        response = client.post("/api/auth/setup",json={**credentials,"display_name":"测试管理员"})
    else:
        response = client.post("/api/auth/login",json=credentials)
    assert response.status_code == 200, response.text
    role = next((r for r in ("admin","document_control","uploader","reader") if r in roles.split(",")), "reader")
    users = client.get("/api/users").json()["users"]
    if not any(row["username"] == user for row in users):
        response=client.post("/api/users",json={"username":user,"display_name":user,"password":"Atomic-Initial-2026","role":role,"active":True,"scopes":[{"registrant":"*","project_category":"*","product":"*"}],"can_download":True,"can_history":True})
        assert response.status_code == 200,response.text
        response=client.post("/api/auth/login",json={"username":user,"password":"Atomic-Initial-2026"})
        assert response.status_code == 200,response.text
        response=client.post("/api/auth/change-password",json={"current_password":"Atomic-Initial-2026","new_password":"Atomic-User-2026"})
        assert response.status_code == 200,response.text
    response=client.post("/api/auth/login",json={"username":user,"password":"Atomic-User-2026"})
    assert response.status_code == 200,response.text


@contextmanager
def store_client(data_dir: Path, *, user: str = "atomic-user", roles: str = "admin,document_control,reader,uploader"):
    store = DMRStore(data_dir)
    client = TestClient(create_app(store, config_for(data_dir, user=user, roles=roles)), base_url="http://127.0.0.1", raise_server_exceptions=False)
    try:
        authenticate(client, user, roles)
        yield store, client
    finally:
        client.close()
        store.close()


class MigrationAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="atomic-", dir=ROOT / "tests")
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def analyze(self, client: TestClient, code: str, version: str, salt: str):
        response = client.post(
            "/api/analyze",
            json={"files": [{"name": f"{code}_{version}-W20【测试主体】-部件.xlsx", "data": b64(xlsx_bom(code, version, "W20", "测试主体", salt))}]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["batch_id"]

    def test_two_independent_apps_concurrent_same_version_conflict_is_serialized(self):
        store_a = DMRStore(self.data_dir)
        store_b = DMRStore(self.data_dir)
        client_a = TestClient(create_app(store_a, config_for(self.data_dir, user="committer-a")), base_url="http://127.0.0.1", raise_server_exceptions=False)
        client_b = TestClient(create_app(store_b, config_for(self.data_dir, user="committer-b")), base_url="http://127.0.0.1", raise_server_exceptions=False)
        try:
            authenticate(client_a,"committer-a")
            authenticate(client_b,"committer-b")
            batch_a = self.analyze(client_a, "BOM-RACE", "V1.0", "a")
            batch_b = self.analyze(client_b, "BOM-RACE", "V1.0", "b")
            barrier = threading.Barrier(2)
            results = []

            def commit(client, batch_id):
                barrier.wait(timeout=10)
                results.append(client.post("/api/commit", json={"batch_id": batch_id}).json())

            threads = [threading.Thread(target=commit, args=(client_a, batch_a)), threading.Thread(target=commit, args=(client_b, batch_b))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(len(results), 2)
            self.assertEqual(sorted(result["committed"] for result in results), [0, 1])
            state = store_a.state()
            record = next(row for row in state["records"] if row["no"] == "BOM-RACE")
            files = [row for row in state["libraryFiles"] if row["recordId"] == record["id"]]
            self.assertEqual(len([row for row in files if row["status"] == "当前有效"]), 1)
            self.assertEqual(record["version"], "V1.0")
            self.assertTrue(any("同版本但文件内容不同" in item["message"] for result in results for item in result["results"]))
        finally:
            client_a.close()
            client_b.close()
            store_a.close()
            store_b.close()

    def test_two_independent_apps_concurrent_upgrade_never_leaves_two_current_versions(self):
        store_a = DMRStore(self.data_dir)
        store_b = DMRStore(self.data_dir)
        client_a = TestClient(create_app(store_a, config_for(self.data_dir, user="upgrade-a")), base_url="http://127.0.0.1", raise_server_exceptions=False)
        client_b = TestClient(create_app(store_b, config_for(self.data_dir, user="upgrade-b")), base_url="http://127.0.0.1", raise_server_exceptions=False)
        try:
            authenticate(client_a,"upgrade-a")
            authenticate(client_b,"upgrade-b")
            batch_a = self.analyze(client_a, "BOM-UPGRADE", "V1.0", "base")
            batch_b = self.analyze(client_b, "BOM-UPGRADE", "V1.1", "next")
            barrier = threading.Barrier(2)
            results = []

            def commit(client, batch_id):
                barrier.wait(timeout=10)
                results.append(client.post("/api/commit", json={"batch_id": batch_id}).json())

            threads = [threading.Thread(target=commit, args=(client_a, batch_a)), threading.Thread(target=commit, args=(client_b, batch_b))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            state = store_a.state()
            record = next(row for row in state["records"] if row["no"] == "BOM-UPGRADE")
            files = [row for row in state["libraryFiles"] if row["recordId"] == record["id"]]
            self.assertEqual(len([row for row in files if row["status"] == "当前有效"]), 1)
            self.assertIn(record["version"], {"V1.0", "V1.1"})
            if record["version"] == "V1.0":
                self.assertTrue(any("低于当前版本" in item["message"] for result in results for item in result["results"]))
            else:
                self.assertLessEqual(len(files), 2)
        finally:
            client_a.close()
            client_b.close()
            store_a.close()
            store_b.close()

    def test_failed_analyze_rolls_back_database_and_removes_new_staging_blobs(self):
        with store_client(self.data_dir) as (store, client):
            response = client.post(
                "/api/analyze",
                json={
                    "files": [
                        {"name": "BOM-CLEAN_V1.0-W20【测试主体】-部件.xlsx", "data": b64(xlsx_bom("BOM-CLEAN", "V1.0", "W20", "测试主体", "ok"))},
                        {"name": "bad-second.xlsx", "data": "not base64"},
                    ]
                },
            )
            self.assertEqual(response.status_code, 400)
            state = store.state()
            self.assertEqual(state["batches"], [])
            self.assertEqual(state["records"], [])
            self.assertEqual(list((self.data_dir / "files").iterdir()), [])

    def test_commit_database_error_rolls_back_current_and_history_statuses(self):
        with store_client(self.data_dir) as (store, client):
            batch_1 = self.analyze(client, "BOM-ROLLBACK", "V1.0", "first")
            self.assertEqual(client.post("/api/commit", json={"batch_id": batch_1}).json()["committed"], 1)
            before = store.state()
            batch_2 = self.analyze(client, "BOM-ROLLBACK", "V1.1", "second")
            original_execute = store.conn.execute

            def failing_execute(sql, params=()):
                if str(sql).startswith("INSERT INTO files"):
                    raise RuntimeError("forced insert failure")
                return original_execute(sql, params)

            with patch.object(store.conn, "execute", side_effect=failing_execute):
                response = client.post("/api/commit", json={"batch_id": batch_2})
            self.assertEqual(response.status_code, 500)
            after = store.state()
            before_files = [{k: row[k] for k in ("name", "version", "status", "sha256")} for row in before["libraryFiles"]]
            after_files = [{k: row[k] for k in ("name", "version", "status", "sha256")} for row in after["libraryFiles"]]
            self.assertEqual(after_files, before_files)
            self.assertEqual(after["records"][0]["version"], before["records"][0]["version"])

    def test_sqlite_url_escape_is_rejected_and_default_data_is_not_migrated(self):
        default_db = ROOT / "data" / "dmr.sqlite3"
        before_mtime = default_db.stat().st_mtime_ns if default_db.exists() else None
        outside = ROOT.parent / "outside-dmr.sqlite3"
        with patch.dict(os.environ, {"DATABASE_URL": f"sqlite:///{outside}", "DMR_DATA_DIR": str(self.data_dir)}):
            with self.assertRaises(RuntimeError):
                create_dmr_engine(self.data_dir, os.environ["DATABASE_URL"])
        after_mtime = default_db.stat().st_mtime_ns if default_db.exists() else None
        self.assertEqual(after_mtime, before_mtime)

    def test_two_apps_keep_roles_actor_and_store_isolated(self):
        dir_a = self.data_dir / "a"
        dir_b = self.data_dir / "b"
        store_a = DMRStore(dir_a)
        store_b = DMRStore(dir_b)
        client_a = TestClient(create_app(store_a, config_for(dir_a, user="alice-uploader", roles="uploader,reader")), base_url="http://127.0.0.1", raise_server_exceptions=False)
        client_b = TestClient(create_app(store_b, config_for(dir_b, user="bob-control", roles="document_control,reader")), base_url="http://127.0.0.1", raise_server_exceptions=False)
        try:
            authenticate(client_a,"alice-uploader","uploader,reader")
            authenticate(client_b,"bob-control","document_control,reader")
            batch_a = self.analyze(client_a, "BOM-APP-A", "V1.0", "a")
            self.assertEqual(client_a.post("/api/commit", json={"batch_id": batch_a}).status_code, 403)
            self.assertEqual(store_a.state()["records"], [])
            self.assertTrue(any(log["user"].startswith("alice-uploader [") for log in store_a.state()["operationLogs"]))

            batch_b = self.analyze(client_b, "BOM-APP-B", "V1.0", "b")
            self.assertEqual(client_b.post("/api/commit", json={"batch_id": batch_b}).json()["committed"], 1)
            self.assertEqual([row["no"] for row in store_b.state()["records"]], ["BOM-APP-B"])
            self.assertEqual(store_a.state()["records"], [])
            self.assertTrue(any(log["user"].startswith("bob-control [") for log in store_b.state()["operationLogs"]))
        finally:
            client_a.close()
            client_b.close()
            store_a.close()
            store_b.close()


if __name__ == "__main__":
    unittest.main()
