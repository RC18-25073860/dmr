import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = ROOT / "tests" / ".runtime"
sys.path.insert(0, str(ROOT))

from backend.config import AppConfig
from backend.service import DMRStore
from server import create_app


ADMIN_PASSWORD = "AdminPass123"
ROOT_TOKEN = "ExampleRootFolderToken000001"
PROJECT_TOKEN = "ExampleProjectFolderToken0001"
PRODUCT_TOKEN = "W20ProductFolderToken001"
SHEET_TOKEN = "ExampleSpreadsheetToken00001"


def sample_snapshot():
    return {
        "schema_version": 1,
        "root_token": ROOT_TOKEN,
        "root_url": f"https://example.feishu.cn/drive/folder/{ROOT_TOKEN}",
        "fetched_at": "2026-09-24T08:30:00+08:00",
        "writeback_enabled": True,
        "folders": [
            {"token": ROOT_TOKEN, "parent_token": None, "name": "DMR清单", "path": ["DMR清单"]},
            {"token": PROJECT_TOKEN, "parent_token": ROOT_TOKEN, "name": "2.家电项目", "path": ["DMR清单", "2.家电项目"]},
            {"token": PRODUCT_TOKEN, "parent_token": PROJECT_TOKEN, "name": "W20", "path": ["DMR清单", "2.家电项目", "W20"]},
        ],
        "documents": [
            {
                "token": SHEET_TOKEN,
                "parent_token": PRODUCT_TOKEN,
                "name": "W20 DMR清单",
                "url": f"https://example.feishu.cn/sheets/{SHEET_TOKEN}",
                "path": ["DMR清单", "2.家电项目", "W20", "W20 DMR清单"],
                "type": "sheet",
                "revision": "rev-20260924",
                "read_at": "2026-09-24T08:31:00+08:00",
                "sheets": [
                    {
                        "sheet_id": "f7c9d1",
                        "sheet_name": "清单",
                        "row_count": 2,
                        "column_count": 3,
                        "ranges": [
                            {
                                "range": "清单!A1:C2",
                                "actual_range": "清单!A1:C2",
                                "row_indices": [1, 2],
                                "col_indices": ["A", "B", "C"],
                                "cells": [["编号", "产品", "版本"], ["11W2002C12B", "W20AI大师白", "V1.0"]],
                                "truncated": False,
                            }
                        ],
                    }
                ],
            }
        ],
    }


class FeishuCatalogHarness:
    def __init__(self):
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="feishu-catalog-", dir=TEST_RUNTIME)
        self.data_dir = Path(self.tmp.name)
        self.store = DMRStore(self.data_dir, None)
        self.config = AppConfig(
            root=ROOT,
            data_dir=self.data_dir,
            database_url=None,
            auth_mode="local",
            static_root=ROOT / "frontend" / "dist",
            local_user="",
            local_roles="",
        )
        self.app = create_app(self.store, self.config)
        self.clients = []

    def close(self):
        for client in self.clients:
            client.close()
        self.store.close()
        self.tmp.cleanup()

    def client(self):
        client = TestClient(self.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        self.clients.append(client)
        return client

    def write_snapshot(self, snapshot=None):
        payload = sample_snapshot() if snapshot is None else snapshot
        (self.data_dir / "feishu_catalog.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def setup_admin(self):
        client = self.client()
        response = client.post(
            "/api/auth/setup",
            json={"display_name": "管理员", "username": "admin-user", "password": ADMIN_PASSWORD},
        )
        self.assert_ok(response)
        return client

    def create_user(self, admin, username, scopes):
        response = admin.post(
            "/api/users",
            json={
                "username": username,
                "display_name": username,
                "password": f"{username.title()}Pass123",
                "role": "reader",
                "active": True,
                "scopes": scopes,
                "can_download": True,
                "can_history": True,
            },
        )
        self.assert_ok(response)
        password = f"{username.title()}Ready123"
        first = self.client()
        self.assert_ok(first.post("/api/auth/login", json={"username": username, "password": f"{username.title()}Pass123"}))
        self.assert_ok(first.post("/api/auth/change-password", json={"current_password": f"{username.title()}Pass123", "new_password": password}))
        reader = self.client()
        self.assert_ok(reader.post("/api/auth/login", json={"username": username, "password": password}))
        return reader

    def assert_ok(self, response):
        if response.status_code >= 400:
            raise AssertionError((response.status_code, response.text[:500]))


class FeishuCatalogTests(unittest.TestCase):
    def setUp(self):
        self.h = FeishuCatalogHarness()

    def tearDown(self):
        self.h.close()

    def test_summary_requires_login(self):
        self.h.write_snapshot()
        self.h.setup_admin()
        response = self.h.client().get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 401)

    def test_summary_hides_cells_and_disables_writeback(self):
        self.h.write_snapshot()
        admin = self.h.setup_admin()
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["writeback_enabled"])
        self.assertEqual(payload["root_token"], ROOT_TOKEN)
        self.assertEqual(len(payload["folders"]), 3)
        self.assertEqual(payload["documents"][0]["sheet_count"], 1)
        self.assertNotIn("sheets", payload["documents"][0])
        self.assertEqual(payload["documents"][0]["url"], f"https://example.feishu.cn/sheets/{SHEET_TOKEN}")

    def test_document_detail_includes_cells_and_coordinates(self):
        self.h.write_snapshot()
        admin = self.h.setup_admin()
        response = admin.get(f"/api/dmr-catalog/{SHEET_TOKEN}")
        self.assertEqual(response.status_code, 200, response.text)
        document = response.json()
        self.assertFalse(document["writeback_enabled"])
        self.assertEqual(document["token"], SHEET_TOKEN)
        self.assertEqual(document["revision"], "rev-20260924")
        self.assertEqual(document["sheets"][0]["sheet_id"], "f7c9d1")
        grid = document["sheets"][0]["ranges"][0]
        self.assertEqual(grid["row_indices"], [1, 2])
        self.assertEqual(grid["col_indices"], ["A", "B", "C"])
        self.assertEqual(grid["cells"][1][0], "11W2002C12B")

    def test_scoped_user_cannot_read_catalog(self):
        self.h.write_snapshot()
        admin = self.h.setup_admin()
        reader = self.h.create_user(admin, "readerw20", [{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}])
        response = reader.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 403)
        self.assertIn("全局授权", response.json()["error"])

    def test_missing_snapshot_is_service_unavailable(self):
        admin = self.h.setup_admin()
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("快照尚未生成", response.json()["error"])

    def test_rejects_invalid_links_and_duplicate_tokens(self):
        admin = self.h.setup_admin()
        invalid = sample_snapshot()
        invalid["documents"][0]["url"] = "https://example.com/sheets/" + SHEET_TOKEN
        self.h.write_snapshot(invalid)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("飞书表格链接", response.json()["error"])

        duplicate = sample_snapshot()
        duplicate["documents"].append(copy.deepcopy(duplicate["documents"][0]))
        self.h.write_snapshot(duplicate)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("重复", response.json()["error"])

    def test_rejects_incomplete_ranges_and_unsafe_folder_trees(self):
        admin = self.h.setup_admin()
        truncated = sample_snapshot()
        truncated["documents"][0]["sheets"][0]["ranges"][0]["truncated"] = True
        self.h.write_snapshot(truncated)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("truncated", response.json()["error"])

        duplicate_row = sample_snapshot()
        duplicate_row["documents"][0]["sheets"][0]["ranges"][0]["row_indices"] = [1, 1]
        self.h.write_snapshot(duplicate_row)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("不能重复", response.json()["error"])

        missing_last_row = sample_snapshot()
        missing_last_row["documents"][0]["sheets"][0]["ranges"][0]["row_indices"] = [1]
        missing_last_row["documents"][0]["sheets"][0]["ranges"][0]["cells"] = [["编号", "产品", "版本"]]
        self.h.write_snapshot(missing_last_row)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("行号必须完整覆盖", response.json()["error"])

        duplicate_across_ranges = sample_snapshot()
        duplicate_across_ranges["documents"][0]["sheets"][0]["ranges"].append(
            {
                "range": "清单!A2:C2",
                "actual_range": "清单!A2:C2",
                "row_indices": [2],
                "col_indices": ["A", "B", "C"],
                "cells": [["11W2002C12B", "W20AI大师白", "V1.0"]],
                "truncated": False,
            }
        )
        self.h.write_snapshot(duplicate_across_ranges)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("跨range重复", response.json()["error"])

        missing_last_column = sample_snapshot()
        missing_last_column["documents"][0]["sheets"][0]["ranges"][0]["col_indices"] = ["A", "B"]
        missing_last_column["documents"][0]["sheets"][0]["ranges"][0]["cells"] = [["编号", "产品"], ["11W2002C12B", "W20AI大师白"]]
        self.h.write_snapshot(missing_last_column)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("完整覆盖A", response.json()["error"])

        multiple_roots = sample_snapshot()
        multiple_roots["folders"].append({"token": "SecondRootToken001", "parent_token": None, "name": "另一个根", "path": ["另一个根"]})
        self.h.write_snapshot(multiple_roots)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("只有一个根目录", response.json()["error"])

        cycle = sample_snapshot()
        cycle["folders"][1]["parent_token"] = PRODUCT_TOKEN
        self.h.write_snapshot(cycle)
        response = admin.get("/api/dmr-catalog")
        self.assertEqual(response.status_code, 503)
        self.assertIn("循环引用", response.json()["error"])

    def test_unknown_document_and_write_methods_are_not_available(self):
        self.h.write_snapshot()
        admin = self.h.setup_admin()
        self.assertEqual(admin.get("/api/dmr-catalog/not-a-token").status_code, 404)
        self.assertEqual(admin.post("/api/dmr-catalog", json={}).status_code, 405)
        self.assertEqual(admin.put(f"/api/dmr-catalog/{SHEET_TOKEN}", json={}).status_code, 405)


if __name__ == "__main__":
    unittest.main()
