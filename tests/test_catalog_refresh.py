import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.catalog_refresh import CatalogDirectoryReader, CatalogRefreshService, ROOT_TOKEN, build_directory_snapshot
from tests.test_feishu_catalog import FeishuCatalogHarness, PROJECT_TOKEN, SHEET_TOKEN, sample_snapshot


PRODUCT_TOKEN = "ProductFolderToken001"
NEW_SHEET_TOKEN = "NewSheetToken000000001"


class FakeReader(CatalogDirectoryReader):
    def __init__(self, tree=None, *, error=None):
        self.tree = tree if tree is not None else default_tree()
        self.error = error
        self.calls = 0

    def folder_children(self, folder, *, deadline_at=None):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.tree.get(folder["token"], []))


class BlockingReader(FakeReader):
    def __init__(self, tree=None):
        super().__init__(tree)
        self.entered = threading.Event()
        self.release = threading.Event()

    def folder_children(self, folder, *, deadline_at=None):
        self.calls += 1
        self.entered.set()
        self.release.wait(2)
        return list(self.tree.get(folder["token"], []))


def default_tree():
    return {
        ROOT_TOKEN: [
            {"token": PROJECT_TOKEN, "name": "1.医疗项目", "type": "folder"},
        ],
        PROJECT_TOKEN: [
            {"token": PRODUCT_TOKEN, "name": "UI04（A1）", "type": "folder"},
        ],
        PRODUCT_TOKEN: [
            {"token": SHEET_TOKEN, "name": "广西由莱-DMR清单-UI04（A1）", "type": "sheet"},
            {"token": NEW_SHEET_TOKEN, "name": "深圳由莱-DMR清单-UI04（A1）", "type": "sheet"},
        ],
    }


class CatalogRefreshServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="catalog-refresh-")
        self.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_old_snapshot(self):
        old = sample_snapshot()
        old["documents"][0]["parent_token"] = PRODUCT_TOKEN
        old["folders"].append({"token": PRODUCT_TOKEN, "parent_token": PROJECT_TOKEN, "name": "旧产品", "path": ["DMR清单", "2.家电项目", "旧产品"]})
        (self.data_dir / "feishu_catalog.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        return old

    def test_directory_refresh_preserves_existing_cell_cache_and_new_docs_are_empty(self):
        self.write_old_snapshot()
        service = CatalogRefreshService(self.data_dir, reader=FakeReader(), ttl_seconds=60)

        status = service.ensure_fresh(force=True, wait=True)

        self.assertEqual(status["status"], "success")
        self.assertFalse(status["sheets_live"])
        self.assertFalse(status["cell_cache"]["live"])
        snapshot = json.loads((self.data_dir / "feishu_catalog.json").read_text(encoding="utf-8"))
        docs = {doc["token"]: doc for doc in snapshot["documents"]}
        self.assertEqual(docs[SHEET_TOKEN]["name"], "广西由莱-DMR清单-UI04（A1）")
        self.assertEqual(docs[SHEET_TOKEN]["revision"], "rev-20260924")
        self.assertEqual(docs[SHEET_TOKEN]["sheets"][0]["ranges"][0]["cells"][1][0], "11W2002C12B")
        self.assertEqual(docs[NEW_SHEET_TOKEN]["sheets"], [])
        self.assertEqual(docs[NEW_SHEET_TOKEN]["revision"], "")

    def test_ttl_skips_non_forced_refresh_but_force_refreshes_again(self):
        reader = FakeReader()
        service = CatalogRefreshService(self.data_dir, reader=reader, ttl_seconds=60)

        first = service.ensure_fresh(force=True, wait=True)
        calls_after_first = reader.calls
        second = service.ensure_fresh(force=False, wait=True)
        third = service.ensure_fresh(force=True, wait=True)

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["reason"], "ttl")
        self.assertEqual(reader.calls, calls_after_first * 2)
        self.assertEqual(third["status"], "success")

    def test_single_flight_does_not_start_parallel_refresh(self):
        reader = BlockingReader()
        service = CatalogRefreshService(self.data_dir, reader=reader, ttl_seconds=60)

        try:
            first = service.ensure_fresh(force=True, wait=False)
            self.assertTrue(reader.entered.wait(1))
            second = service.ensure_fresh(force=True, wait=False)
        finally:
            reader.release.set()
            if service._thread:
                service._thread.join(2)

        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        self.assertEqual(reader.calls, 3)
        self.assertEqual(service.status()["status"], "success")

    def test_failure_preserves_old_snapshot_and_does_not_leak_reader_details(self):
        old = self.write_old_snapshot()
        service = CatalogRefreshService(self.data_dir, reader=FakeReader(error=RuntimeError("SECRET stderr token")))

        status = service.ensure_fresh(force=True, wait=True)

        self.assertEqual(status["status"], "failed")
        self.assertNotIn("SECRET", status["message"])
        snapshot = json.loads((self.data_dir / "feishu_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["fetched_at"], old["fetched_at"])
        self.assertEqual(snapshot["documents"][0]["sheets"][0]["sheet_name"], "清单")

    def test_service_only_refreshes_when_requested(self):
        reader = FakeReader()
        service = CatalogRefreshService(self.data_dir, reader=reader, ttl_seconds=60)

        self.assertEqual(reader.calls, 0)
        self.assertFalse(service.running())
        service.ensure_fresh(force=True, wait=True)
        self.assertEqual(reader.calls, 3)
        time.sleep(0.12)
        self.assertEqual(reader.calls, 3)

    def test_build_directory_snapshot_rejects_timeout_before_mutating(self):
        with self.assertRaisesRegex(RuntimeError, "超时"):
            build_directory_snapshot(FakeReader(), deadline_at=time.monotonic() - 1)


class CatalogRefreshRouteTests(unittest.TestCase):
    def setUp(self):
        self.h = FeishuCatalogHarness()
        self.h.app.state.catalog_refresh = CatalogRefreshService(self.h.data_dir, reader=FakeReader(), ttl_seconds=60)

    def tearDown(self):
        self.h.close()

    def test_refresh_route_requires_login_and_global_scope(self):
        self.h.write_snapshot()
        admin = self.h.setup_admin()
        anonymous = self.h.client()
        self.assertEqual(anonymous.post("/api/dmr-catalog/refresh?wait=true", json={}).status_code, 401)

        reader = self.h.create_user(admin, "catalogreader", [{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}])
        forbidden = reader.post("/api/dmr-catalog/refresh?wait=true", json={})
        self.assertEqual(forbidden.status_code, 403)

    def test_refresh_route_updates_snapshot_and_summary_reports_status(self):
        admin = self.h.setup_admin()

        refresh = admin.post("/api/dmr-catalog/refresh?force=true&wait=true", json={})
        summary = admin.get("/api/dmr-catalog")

        self.assertEqual(refresh.status_code, 200, refresh.text)
        self.assertEqual(refresh.json()["status"], "success")
        self.assertEqual(summary.status_code, 200, summary.text)
        payload = summary.json()
        self.assertEqual(payload["refresh_status"]["status"], "success")
        self.assertFalse(payload["refresh_status"]["sheets_live"])
        self.assertEqual(payload["sync_status"]["status"], "success")
        self.assertEqual(len(payload["documents"]), 2)
        self.assertNotIn("sheets", payload["documents"][0])

    def test_search_refreshes_directory_on_each_use(self):
        admin = self.h.setup_admin()
        reader = FakeReader()
        self.h.app.state.catalog_refresh = CatalogRefreshService(self.h.data_dir, reader=reader, ttl_seconds=60)

        first = admin.get("/api/dmr-catalog-search?query=UI04")
        second = admin.get("/api/dmr-catalog-search?query=UI04")

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(first.json()["documents"]), 2)
        self.assertEqual(reader.calls, 6)


if __name__ == "__main__":
    unittest.main()
