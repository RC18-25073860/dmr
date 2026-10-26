from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.bom_date_migration import RULE_CHANGE, migrate_bom_dates
from backend.service import DMRStore, json_dumps, json_loads
from tests.test_backend import xlsx_bom


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "bom-date-20260921"
CODE = "11W2002C12B"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class BomDateMigrationTests(unittest.TestCase):
    def setUp(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="case-", dir=ARTIFACTS)
        self.store = DMRStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def candidate(self, date: str, salt: str = "", code: str = CODE) -> dict:
        name = f"{code}_V1.0-W20AI【深圳金茉】-成品-{date}.xlsx"
        data = xlsx_bom(code, "V1.0", "W20AI", "深圳金茉", salt)
        return {"name": name, "data": b64(data)}

    def commit_candidates(self, *files: dict) -> None:
        batch = self.store.analyze(list(files))
        result = self.store.commit_batch(batch["batch_id"])
        self.assertEqual(result["blocked"], 0)

    def make_files_look_legacy_v1(self, code: str = CODE) -> None:
        with self.store.lock, self.store.conn:
            for row in self.store.execute("SELECT id,metadata FROM files").fetchall():
                metadata = json_loads(row["metadata"], {})
                metadata.pop("version_rule", None)
                metadata.pop("version_scope", None)
                metadata.pop("version_date", None)
                self.store.execute("UPDATE files SET version='V1.0',metadata=? WHERE id=?", (json_dumps(metadata), row["id"]))
            self.store.execute("UPDATE records SET version='V1.0',previous='',metadata=? WHERE business_code=?", (json_dumps({"document_version": "V1.0"}), code))

    def make_batch_items_look_legacy_v1(self) -> None:
        with self.store.lock, self.store.conn:
            for row in self.store.execute("SELECT id,metadata FROM batch_items").fetchall():
                metadata = json_loads(row["metadata"], {})
                metadata.pop("version_rule", None)
                metadata.pop("version_scope", None)
                metadata.pop("version_date", None)
                self.store.execute("UPDATE batch_items SET version='V1.0',detected_version='V1.0',metadata=? WHERE id=?", (json_dumps(metadata), row["id"]))

    def test_backfills_committed_files_and_is_idempotent(self):
        self.commit_candidates(self.candidate("20260825", "old"), self.candidate("20260907", "new"))
        file_ids_before = [row["id"] for row in self.store.execute("SELECT id FROM files ORDER BY id").fetchall()]
        blob_paths_before = [row["blob_path"] for row in self.store.execute("SELECT blob_path FROM files ORDER BY id").fetchall()]
        self.make_files_look_legacy_v1()

        report = migrate_bom_dates(self.store)

        self.assertEqual(report["rule_change"], RULE_CHANGE)
        self.assertEqual(report["files_changed"], 2)
        self.assertEqual(report["records_changed"], 1)
        record = self.store.execute("SELECT * FROM records WHERE business_code=?", (CODE,)).fetchone()
        self.assertEqual(record["version"], "20260907")
        self.assertEqual(record["previous"], "20260825")
        self.assertEqual(json_loads(record["metadata"], {})["document_version"], "V1.0")
        files = [dict(row) for row in self.store.execute("SELECT * FROM files ORDER BY version").fetchall()]
        self.assertEqual([row["version"] for row in files], ["20260825", "20260907"])
        self.assertEqual([row["status"] for row in files], ["历史版本", "当前有效"])
        self.assertEqual([row["id"] for row in self.store.execute("SELECT id FROM files ORDER BY id").fetchall()], file_ids_before)
        self.assertEqual([row["blob_path"] for row in self.store.execute("SELECT blob_path FROM files ORDER BY id").fetchall()], blob_paths_before)
        logs = [row["change"] for row in self.store.execute("SELECT change FROM logs WHERE action='BOM日期版本规则回填'").fetchall()]
        self.assertTrue(any(RULE_CHANGE in change and "V1.0 → 20260907" in change for change in logs))

        second = migrate_bom_dates(self.store)
        self.assertEqual(second["files_changed"], 0)
        self.assertEqual(second["records_changed"], 0)
        self.assertEqual(len(self.store.execute("SELECT * FROM files").fetchall()), 2)

    def test_updates_committed_batch_snapshots_after_file_backfill(self):
        self.commit_candidates(self.candidate("20260825", "old"), self.candidate("20260907", "new"))
        self.make_files_look_legacy_v1()
        self.make_batch_items_look_legacy_v1()

        report = migrate_bom_dates(self.store)

        self.assertEqual(report["files_changed"], 2)
        self.assertEqual(report["batch_items_changed"], 2)
        items = [dict(row) for row in self.store.execute("SELECT version,detected_version,status FROM batch_items ORDER BY file_name").fetchall()]
        self.assertEqual([row["version"] for row in items], ["20260825", "20260907"])
        self.assertEqual([row["detected_version"] for row in items], ["20260825", "20260907"])
        self.assertEqual({row["status"] for row in items}, {"committed"})

        second = migrate_bom_dates(self.store)
        self.assertEqual(second["files_changed"], 0)
        self.assertEqual(second["batch_items_changed"], 0)

    def test_rolls_back_file_updates_when_record_refresh_fails(self):
        self.commit_candidates(self.candidate("20260825", "old"), self.candidate("20260907", "new"))
        self.make_files_look_legacy_v1()
        before_files = [(row["id"], row["version"], row["status"], row["metadata"]) for row in self.store.execute("SELECT * FROM files ORDER BY id").fetchall()]
        before_records = [(row["id"], row["version"], row["previous"], row["metadata"]) for row in self.store.execute("SELECT * FROM records ORDER BY id").fetchall()]
        before_logs = self.store.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]
        original_execute = self.store.execute

        def failing_execute(sql, params=()):
            if str(sql).startswith("UPDATE records SET version="):
                raise RuntimeError("forced record refresh failure")
            return original_execute(sql, params)

        with patch.object(self.store, "execute", side_effect=failing_execute):
            with self.assertRaises(RuntimeError):
                migrate_bom_dates(self.store)

        after_files = [(row["id"], row["version"], row["status"], row["metadata"]) for row in self.store.execute("SELECT * FROM files ORDER BY id").fetchall()]
        after_records = [(row["id"], row["version"], row["previous"], row["metadata"]) for row in self.store.execute("SELECT * FROM records ORDER BY id").fetchall()]
        after_logs = self.store.execute("SELECT COUNT(*) AS n FROM logs").fetchone()["n"]
        self.assertEqual(before_files, after_files)
        self.assertEqual(before_records, after_records)
        self.assertEqual(before_logs, after_logs)

    def test_blocks_whole_record_group_when_one_member_is_contradictory(self):
        self.commit_candidates(self.candidate("20260825", "old"), self.candidate("20260907", "new"))
        self.make_files_look_legacy_v1()
        self.make_batch_items_look_legacy_v1()
        bad = self.store.execute("SELECT * FROM files WHERE name LIKE '%20260907%'").fetchone()
        with self.store.lock, self.store.conn:
            self.store.execute("UPDATE files SET name=? WHERE id=?", (bad["name"].replace(CODE, "11W2002C13B", 1), bad["id"]))
            self.store.execute("UPDATE batch_items SET record_id=''")

        before = [(row["id"], row["name"], row["version"], row["status"]) for row in self.store.execute("SELECT * FROM files ORDER BY id").fetchall()]
        before_record = [dict(row) for row in self.store.execute("SELECT business_code,version,previous,file_name FROM records ORDER BY id").fetchall()]
        before_items = [(row["id"], row["record_id"], row["version"], row["detected_version"], row["status"], row["metadata"]) for row in self.store.execute("SELECT * FROM batch_items ORDER BY id").fetchall()]
        report = migrate_bom_dates(self.store)
        after = [(row["id"], row["name"], row["version"], row["status"]) for row in self.store.execute("SELECT * FROM files ORDER BY id").fetchall()]
        after_record = [dict(row) for row in self.store.execute("SELECT business_code,version,previous,file_name FROM records ORDER BY id").fetchall()]
        after_items = [(row["id"], row["record_id"], row["version"], row["detected_version"], row["status"], row["metadata"]) for row in self.store.execute("SELECT * FROM batch_items ORDER BY id").fetchall()]

        self.assertEqual(before, after)
        self.assertEqual(before_record, after_record)
        self.assertEqual(before_items, after_items)
        self.assertEqual(report["files_changed"], 0)
        self.assertEqual(report["records_changed"], 0)
        self.assertEqual(report["batch_items_changed"], 0)
        self.assertEqual(report["blocked"][0]["kind"], "record")
        self.assertTrue(all(entry["kind"] == "batch_item_blocked_by_record" for entry in report["unchanged"] if entry.get("batch_id")))

    def test_mixed_date_and_non_date_files_block_without_status_changes(self):
        code = "BOMMIX12"
        self.commit_candidates(self.candidate("20260907", "dated", code=code))
        self.make_files_look_legacy_v1(code)
        record = self.store.execute("SELECT * FROM records WHERE business_code=?", (code,)).fetchone()
        data = xlsx_bom(code, "V1.0", "W20AI", "深圳金茉", "manual")
        _blob_id, blob_path, _size = self.store.store_blob(data, f"{code}_V1.0-W20AI【深圳金茉】-部件.xlsx")
        with self.store.lock, self.store.conn:
            self.store.execute(
                """INSERT INTO files(id,record_id,name,version,status,date,source,size,type,storage,sha256,blob_path,metadata,linked_bom,link_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "MIXED-NON-DATE",
                    record["id"],
                    f"{code}_V1.0-W20AI【深圳金茉】-部件.xlsx",
                    "V1.0",
                    "当前有效",
                    "",
                    "manual",
                    "1 KB",
                    "BOM",
                    "本地保存",
                    hashlib.sha256(data).hexdigest(),
                    blob_path,
                    json_dumps({"document_version": "V1.0"}),
                    code,
                    "已关联",
                    "2026-09-21T00:00:00",
                ),
            )
        before = [(row["id"], row["version"], row["status"]) for row in self.store.execute("SELECT * FROM files ORDER BY id").fetchall()]

        report = migrate_bom_dates(self.store)

        after = [(row["id"], row["version"], row["status"]) for row in self.store.execute("SELECT * FROM files ORDER BY id").fetchall()]
        self.assertEqual(before, after)
        self.assertEqual(report["files_changed"], 0)
        self.assertEqual(report["blocked"][0]["reason"], "同一BOM记录同时存在日期版本文件和非日期版本文件，需人工确认后再迁移")

    def test_updates_pending_batch_items_without_archiving(self):
        batch = self.store.analyze([self.candidate("20260907", "pending")])
        item_id = batch["items"][0]["id"]
        with self.store.lock, self.store.conn:
            metadata = json_loads(self.store.execute("SELECT metadata FROM batch_items WHERE id=?", (item_id,)).fetchone()["metadata"], {})
            metadata.pop("version_rule", None)
            metadata.pop("version_scope", None)
            metadata.pop("version_date", None)
            self.store.execute(
                """UPDATE batch_items SET version='V1.0',detected_version='V1.0',status='conflict',
                          recommendation='同版本但文件内容不同，需人工判断，禁止直接覆盖。',
                          project_category='医疗',project_category_source='人工核对',metadata=?
                   WHERE id=?""",
                (json_dumps(metadata), item_id),
            )
            self.store.create_exception_for_item(batch["batch_id"], item_id, batch["items"][0]["fileName"], "conflict", "同版本但文件内容不同，需人工判断，禁止直接覆盖。", "", CODE)

        report = migrate_bom_dates(self.store)

        self.assertEqual(report["batch_items_changed"], 1)
        item = self.store.execute("SELECT * FROM batch_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(item["version"], "20260907")
        self.assertEqual(item["detected_version"], "20260907")
        self.assertEqual(item["status"], "matched")
        self.assertEqual(item["project_category"], "医疗")
        self.assertEqual(item["project_category_source"], "人工核对")
        self.assertEqual(self.store.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"], 0)
        self.assertEqual(self.store.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"], 0)
        self.assertEqual(self.store.execute("SELECT status FROM exceptions WHERE item_id=?", (item_id,)).fetchone()["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
