import base64
import os
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_backend import Harness, b64, xlsx_bom

ROOT = Path(__file__).resolve().parents[1]


def xlsx_bom_with_category(code: str, version: str, product: str, registrant: str, category: str, salt: str = "") -> bytes:
    headers = ["BOM层级", "父项编码", "BOM版本", "规格型号", "物料类别", "项目类别", "备注"]
    root = ["0", code, version, f"{product}[测试产品]【{registrant}】", "BOM", category, salt]

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


def xlsx_unknown_category(code: str, version: str, product: str, registrant: str) -> bytes:
    return xlsx_bom(code, version, product, registrant, "unknown")


class ProjectCategoryTests(unittest.TestCase):
    def fixture(self, roles="admin,document_control,reader,uploader"):
        tmp = tempfile.TemporaryDirectory(prefix="category-", dir=ROOT / "tests")
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"DMR_AUTH_MODE": "local", "DMR_LOCAL_ROLES": roles, "DMR_LOCAL_USER": "category-test-user"})
        env.start()
        self.addCleanup(env.stop)
        harness = Harness(Path(tmp.name))
        self.addCleanup(harness.close)
        return harness

    def test_project_category_is_separate_from_file_type_and_unknown_stays_pending(self):
        h = self.fixture()
        batch = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-CAT_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom_with_category("BOM-CAT", "V1.0", "ZZ99", "深圳金茉", "医疗"))}]})
        self.assertEqual(batch["items"][0]["type"], "BOM")
        self.assertEqual(batch["items"][0]["project_category"], "医疗")
        self.assertEqual(batch["items"][0]["project_category_source"], "项目类别")
        h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})
        record = next(row for row in h.request("GET", "/api/state")["records"] if row["no"] == "BOM-CAT")
        self.assertEqual(record["type"], "BOM")
        self.assertEqual(record["project_category"], "医疗")

        unknown = h.request("POST", "/api/analyze", {"files": [{"name": "治疗仪附件-待确认.bin", "data": base64.b64encode(b"raw").decode()}]})
        self.assertEqual(unknown["items"][0]["project_category"], "待确认")
        self.assertNotEqual(unknown["items"][0]["project_category"], "家电")

    def test_old_cached_link_status_does_not_claim_unconfirmed_category(self):
        h = self.fixture()
        batch = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-CACHED_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_unknown_category("BOM-CACHED", "V1.0", "ZZ99", "深圳金茉"))}]})
        h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})
        store = h.server_mod.STORE
        with store.conn:
            store.conn.execute("UPDATE files SET link_status='已关联'")
        state = h.request("GET", "/api/state")
        self.assertEqual(state["libraryFiles"][0]["link_status"], "主体产品已匹配，类别待确认")
        self.assertEqual(store.execute("SELECT link_status FROM files").fetchone()["link_status"], "已关联")

    def test_manual_classification_updates_same_registrant_product_scope_and_persists(self):
        h = self.fixture()
        batch = h.request(
            "POST",
            "/api/analyze",
            {
                "files": [
                    {"name": "BOM-SCOPE-A_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_unknown_category("BOM-SCOPE-A", "V1.0", "ZZ99", "深圳金茉"))},
                    {"name": "BOM-SCOPE-B_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_unknown_category("BOM-SCOPE-B", "V1.0", "ZZ99", "深圳金茉"))},
                ]
            },
        )
        h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})
        state = h.request("GET", "/api/state")
        first = next(row for row in state["records"] if row["no"] == "BOM-SCOPE-A")
        result = h.request("POST", f"/api/records/{first['id']}/classification", {"project_category": "家电"})
        self.assertEqual(result["scope"]["record_count"], 2)
        state = h.request("GET", "/api/state")
        scoped = [row for row in state["records"] if row["product"] == "ZZ99" and row["registrant"] == "深圳金茉"]
        self.assertTrue(scoped)
        self.assertTrue(all(row["project_category"] == "家电" for row in scoped))
        self.assertTrue(any("待确认" in log["change"] and "→ 家电" in log["change"] and "本地确认不代表企业审批" in log["change"] for log in state["operationLogs"]))
        committed_items = h.server_mod.STORE.execute("SELECT project_category FROM batch_items WHERE status='committed'").fetchall()
        self.assertTrue(committed_items)
        self.assertTrue(all(row["project_category"] == "待确认" for row in committed_items))

        pending = h.request("POST", "/api/analyze", {"files": [{"name": "SOFT-DECLARED_V1.0.pdf", "data": b64(b"%PDF-1.4 declared")}]})
        pending_item = pending["items"][0]
        reviewed = h.request(
            "POST",
            "/api/review",
            {
                "batch_id": pending["batch_id"],
                "item_id": pending_item["id"],
                "code": "SOFT-DECLARED",
                "registrant": "深圳金茉",
                "product": "ZZ99",
                "module": "软件",
                "type": "软件资料",
                "version": "V1.0",
                "project_category": "医疗",
            },
        )
        self.assertEqual(reviewed["item"]["project_category"], "医疗")
        h.request("POST", f"/api/records/{first['id']}/classification", {"project_category": "家电"})
        declared = h.server_mod.STORE.execute("SELECT project_category,project_category_source FROM batch_items WHERE id=?", (pending_item["id"],)).fetchone()
        self.assertEqual(declared["project_category"], "医疗")
        self.assertEqual(declared["project_category_source"], "人工核对")

        h.close()
        h2 = Harness(Path(h.server_mod.STORE.data_dir))
        self.addCleanup(h2.close)
        persisted = h2.request("GET", "/api/state")
        self.assertTrue(all(row["project_category"] == "家电" for row in persisted["records"] if row["product"] == "ZZ99"))

    def test_invalid_enum_and_reader_role_are_rejected(self):
        h = self.fixture()
        batch = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-ENUM_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_unknown_category("BOM-ENUM", "V1.0", "ZZ99", "深圳金茉"))}]})
        h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]})
        record = h.request("GET", "/api/state")["records"][0]
        h.request("POST", f"/api/records/{record['id']}/classification", {"project_category": "消费电子"}, status=400)
        h.request("POST", f"/api/records/{record['id']}/classification", {"project_category": "医美"}, status=400)
        h.request("POST", f"/api/records/{record['id']}/classification", {"project_category": "电子电器"}, status=400)

        for category in ("医美", "电子电器"):
            analyzed = h.request(
                "POST",
                "/api/analyze",
                {"files": [{"name": f"BOM-ALIAS-{category}_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom_with_category(f"BOM-ALIAS-{category}", "V1.0", "ZZ99", "深圳金茉", category))}]},
            )
            self.assertEqual(analyzed["items"][0]["project_category"], "待确认")
            self.assertEqual(analyzed["items"][0]["project_category_source"], "未提供")

        reader = self.fixture("reader")
        h_state = h.request("GET", "/api/state")
        reader.request("POST", f"/api/records/{record['id']}/classification", {"project_category": "医疗"}, status=403)
        self.assertEqual(h_state["records"][0]["project_category"], "待确认")

    def test_confirmed_category_conflict_blocks_existing_record_update_and_bom_link(self):
        h = self.fixture()
        medical = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-CONFLICT_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom_with_category("BOM-CONFLICT", "V1.0", "ZZ99", "深圳金茉", "医疗", "medical"))}]})
        h.request("POST", "/api/commit", {"batch_id": medical["batch_id"]})

        home_update = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-CONFLICT_V1.1-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom_with_category("BOM-CONFLICT", "V1.1", "ZZ99", "深圳金茉", "家电", "home"))}]})
        self.assertEqual(home_update["items"][0]["status"], "conflict")
        self.assertIn("项目类别冲突", home_update["items"][0]["recommendation"])

        linked = h.request(
            "POST",
            "/api/analyze",
            {
                "files": [
                    {
                        "name": "SOFT-CONFLICT_V1.0.hex",
                        "data": b64(b":020000040000FA"),
                    }
                ]
            },
        )
        item = linked["items"][0]
        reviewed = h.request(
            "POST",
            "/api/review",
            {
                "batch_id": linked["batch_id"],
                "item_id": item["id"],
                "code": "SOFT-CONFLICT",
                "registrant": "深圳金茉",
                "product": "ZZ99",
                "module": "软件",
                "type": "固件",
                "version": "V1.0",
                "linked_bom": "BOM-CONFLICT",
                "project_category": "家电",
            },
        )
        self.assertEqual(reviewed["item"]["status"], "conflict")
        self.assertIn("BOM项目类别冲突", reviewed["item"]["recommendation"])

    def test_same_project_confirmed_category_blocks_new_code_and_commit_race(self):
        h = self.fixture()
        seed = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-PROJECT-MED_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom_with_category("BOM-PROJECT-MED", "V1.0", "ZZ99", "深圳金茉", "医疗", "seed"))}]})
        h.request("POST", "/api/commit", {"batch_id": seed["batch_id"]})

        other_code = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-PROJECT-HOME_V1.0-ZZ99【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom_with_category("BOM-PROJECT-HOME", "V1.0", "ZZ99", "深圳金茉", "家电", "other"))}]})
        self.assertEqual(other_code["items"][0]["status"], "conflict")
        self.assertIn("同主体同产品项目类别冲突", other_code["items"][0]["recommendation"])

        pending = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-RACE-CAT_V1.0-X1【测试主体】-部件.xlsx", "data": b64(xlsx_bom_with_category("BOM-RACE-CAT", "V1.0", "X1", "测试主体", "家电", "race"))}]})
        self.assertEqual(pending["items"][0]["status"], "matched")
        seed_race = h.request("POST", "/api/analyze", {"files": [{"name": "BOM-RACE-SEED_V1.0-X1【测试主体】-部件.xlsx", "data": b64(xlsx_bom_with_category("BOM-RACE-SEED", "V1.0", "X1", "测试主体", "医疗", "seed-race"))}]})
        self.assertEqual(h.request("POST", "/api/commit", {"batch_id": seed_race["batch_id"]})["committed"], 1)
        committed = h.request("POST", "/api/commit", {"batch_id": pending["batch_id"]})
        self.assertEqual(committed["committed"], 0)
        self.assertEqual(committed["blocked"], 1)
        self.assertIn("同主体同产品项目类别冲突", committed["results"][0]["message"])


if __name__ == "__main__":
    unittest.main()
