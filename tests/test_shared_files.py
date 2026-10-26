import json
import unittest

from test_backend import b64, xlsx_bom
from test_feishu_catalog import sample_snapshot
from test_permissions import PermissionsHarness


class SharedFilesTests(unittest.TestCase):
    def setUp(self):
        self.h = PermissionsHarness()
        self.admin = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def share_payload(self, client, source_file_id, **fields):
        details = client.get(f"/api/files/{source_file_id}/share")
        self.assertEqual(details.status_code, 200, details.text)
        return {"expected_edit_token": details.json()["edit_token"], "reason": "同一附件适用于目标主体机型，按清单复用归档", **fields}

    def write_catalog_snapshot(self):
        (self.h.data_dir / "feishu_catalog.json").write_text(json.dumps(sample_snapshot(), ensure_ascii=False), encoding="utf-8")

    def test_share_file_reuses_blob_for_independent_target_current_version(self):
        source_record, source_file = self.h.seed_bom(self.admin, "BOM-SHARE", "V1.0", "W20", "深圳金茉", "家电", "source")
        source_bytes = self.h.store.read_blob(source_file["metadata"].get("blob_path", ""))
        response = self.admin.post(
            f"/api/files/{source_file['id']}/share",
            json=self.share_payload(self.admin, source_file["id"], **{
                "code": "BOM-SHARE",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "committed")
        state = self.admin.get("/api/state").json()
        target_record = next(row for row in state["records"] if row["id"] == body["record_id"])
        self.assertEqual(target_record["registrant"], "广西由莱")
        self.assertEqual(target_record["product"], "UI20S")
        source_after = next(row for row in state["records"] if row["id"] == source_record["id"])
        self.assertEqual(source_after["version"], "V1.0")
        target_file = next(row for row in state["libraryFiles"] if row["id"] == body["file_id"])
        self.assertEqual(target_file["recordId"], target_record["id"])
        self.assertEqual(target_file["status"], "当前有效")
        self.assertEqual(target_file["sha256"], source_file["sha256"])
        self.assertEqual(self.admin.get(f"/api/download/{body['file_id']}").content, source_bytes)
        self.assertIn("shared_from", target_file["metadata"])
        self.assertEqual(target_file["metadata"]["shared_from"]["file_id"], source_file["id"])

    def test_share_file_creates_manual_catalog_update_for_target_record(self):
        self.write_catalog_snapshot()
        _source_record, source_file = self.h.seed_bom(self.admin, "BOM-SHARE-CAT", "V1.0", "W20", "深圳金茉", "家电", "source")
        response = self.admin.post(
            f"/api/files/{source_file['id']}/share",
            json=self.share_payload(self.admin, source_file["id"], **{
                "code": "BOM-SHARE-CAT",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["manual_updates"]), 1)
        row = self.h.store.execute("SELECT payload FROM manual_catalog_updates WHERE id=?", (body["manual_updates"][0],)).fetchone()
        item = json.loads(row["payload"])
        self.assertEqual(item["file_id"], body["file_id"])
        self.assertEqual(item["record_id"], body["record_id"])
        self.assertEqual(item["code"], "BOM-SHARE-CAT")
        self.assertEqual(item["registrant"], "广西由莱")
        self.assertEqual(item["product"], "UI20S")
        self.assertEqual(item["reason"], "first_archive")
        self.assertEqual(item["status"], "pending")

    def test_share_file_obeys_target_scope_and_source_download_permission(self):
        _record, source_file = self.h.seed_bom(self.admin, "BOM-SCOPE", "V1.0", "W20", "深圳金茉", "家电", "source")
        _user, password = self.h.create_user(
            self.admin,
            "share-doc",
            "document_control",
            [{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}],
        )
        scoped = self.h.login("share-doc", password)
        response = scoped.post(
            f"/api/files/{source_file['id']}/share",
            json=self.share_payload(scoped, source_file["id"], **{
                "code": "BOM-SCOPE",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(response.status_code, 403, response.text)

        _user, no_download_password = self.h.create_user(
            self.admin,
            "share-nodownload",
            "document_control",
            can_download=False,
        )
        no_download = self.h.login("share-nodownload", no_download_password)
        response = no_download.post(
            f"/api/files/{source_file['id']}/share",
            json={
                "expected_edit_token": "missing-download-cannot-fetch-share-details",
                "reason": "测试无下载权限",
                "code": "BOM-SCOPE",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            },
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_share_file_rejects_unapproved_new_subject_without_touching_history(self):
        _record, source_file = self.h.seed_bom(self.admin, "BOM-SUBJECT", "V1.0", "W20", "深圳金茉", "家电", "source")
        response = self.admin.post(
            f"/api/files/{source_file['id']}/share",
            json=self.share_payload(self.admin, source_file["id"], **{
                "code": "BOM-SUBJECT",
                "registrant": "未配置主体",
                "project_category": "家电",
                "product": "W20",
                "module": "BOM",
            }),
        )
        self.assertEqual(response.status_code, 400, response.text)
        state = self.admin.get("/api/state").json()
        self.assertFalse(any(row["registrant"] == "未配置主体" for row in state["records"]))

    def test_share_file_rejects_category_conflict_and_client_category_source(self):
        _source_record, source_file = self.h.seed_bom(self.admin, "BOM-CAT-SHARE", "V1.0", "W20", "深圳金茉", "家电", "source")
        self.h.seed_bom(self.admin, "BOM-CAT-EXISTING", "V1.0", "UI20S", "广西由莱", "医疗", "existing")
        conflict = self.admin.post(
            f"/api/files/{source_file['id']}/share",
            json=self.share_payload(self.admin, source_file["id"], **{
                "code": "BOM-CAT-SHARE",
                "registrant": "广西由莱",
                "project_category": "家电",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("项目类别冲突", conflict.text)

        source_override = self.admin.post(
            f"/api/files/{source_file['id']}/share",
            json=self.share_payload(self.admin, source_file["id"], **{
                "code": "BOM-CAT-SHARE",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "project_category_source": "客户端伪造",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(source_override.status_code, 400, source_override.text)

    def test_share_dated_history_skips_existing_same_date_same_sha(self):
        source_record, source_file = self.h.seed_bom(self.admin, "BOM-DATE-SHARE", "V1.0", "W20", "深圳金茉", "家电", "source")
        self.h.seed_bom(self.admin, "BOM-DATE-SHARE", "V1.0", "UI20S", "广西由莱", "医疗", "target")
        target_record = dict(self.h.store.execute(
            "SELECT * FROM records WHERE business_code=? AND registrant=? AND product=? AND module=?",
            ("BOM-DATE-SHARE", "广西由莱", "UI20S", "BOM"),
        ).fetchone())
        source_meta = {"version_rule": "filename_date", "version_date": "20260901", "source_bom_code": "BOM-DATE-SHARE"}
        target_meta = {"version_rule": "filename_date", "version_date": "20260902", "source_bom_code": "BOM-DATE-SHARE"}
        with self.h.store.conn:
            self.h.store.execute("UPDATE files SET version=?,metadata=? WHERE id=?", ("20260901", json.dumps(source_meta), source_file["id"]))
            self.h.store.execute("UPDATE records SET version=?,metadata=? WHERE id=?", ("20260901", json.dumps(source_meta), source_record["id"]))
            self.h.store.execute("UPDATE files SET version=?,metadata=? WHERE record_id=?", ("20260902", json.dumps(target_meta), target_record["id"]))
            self.h.store.execute("UPDATE records SET version=?,metadata=? WHERE id=?", ("20260902", json.dumps(target_meta), target_record["id"]))

        payload = {
            "code": "BOM-DATE-SHARE",
            "registrant": "广西由莱",
            "project_category": "医疗",
            "product": "UI20S",
            "module": "BOM",
        }
        first = self.admin.post(f"/api/files/{source_file['id']}/share", json=self.share_payload(self.admin, source_file["id"], **payload))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "committed")
        inserted = dict(self.h.store.execute("SELECT * FROM files WHERE id=?", (first.json()["file_id"],)).fetchone())
        self.assertEqual(inserted["status"], "历史版本")

        second = self.admin.post(f"/api/files/{source_file['id']}/share", json=self.share_payload(self.admin, source_file["id"], **payload))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["status"], "skipped")
        count = self.h.store.execute(
            "SELECT COUNT(*) AS n FROM files WHERE record_id=? AND version=? AND sha256=?",
            (target_record["id"], "20260901", source_file["sha256"]),
        ).fetchone()["n"]
        self.assertEqual(count, 1)
        with self.h.store.conn:
            existing = dict(self.h.store.execute(
                "SELECT * FROM files WHERE record_id=? AND version=?",
                (target_record["id"], "20260901"),
            ).fetchone())
            self.h.store.execute(
                """INSERT INTO files(id,record_id,name,version,status,date,source,size,type,storage,sha256,blob_path,metadata,linked_bom,link_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "mixed-sha-history",
                    existing["record_id"],
                    existing["name"],
                    existing["version"],
                    "历史版本",
                    existing["date"],
                    existing["source"],
                    existing["size"],
                    existing["type"],
                    existing["storage"],
                    "0" * 64,
                    existing["blob_path"],
                    existing["metadata"],
                    existing["linked_bom"],
                    existing["link_status"],
                    existing["created_at"],
                ),
            )
        mixed = self.admin.post(f"/api/files/{source_file['id']}/share", json=self.share_payload(self.admin, source_file["id"], **payload))
        self.assertEqual(mixed.status_code, 409, mixed.text)
        self.assertIn("同版本已有不同文件内容", mixed.text)

    def test_share_file_blocks_same_version_different_content_and_downgrade(self):
        _source_record, source_file = self.h.seed_bom(self.admin, "BOM-SAME-DIFF", "V1.0", "W20", "深圳金茉", "家电", "source")
        self.h.seed_bom(self.admin, "BOM-SAME-DIFF", "V1.0", "UI20S", "广西由莱", "医疗", "different")
        same_version = self.admin.post(
            f"/api/files/{source_file['id']}/share",
            json=self.share_payload(self.admin, source_file["id"], **{
                "code": "BOM-SAME-DIFF",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(same_version.status_code, 409, same_version.text)
        self.assertIn("同版本已有不同文件内容", same_version.text)

        _low_record, low_file = self.h.seed_bom(self.admin, "BOM-DOWN", "V1.0", "W20", "深圳金茉", "家电", "low")
        self.h.seed_bom(self.admin, "BOM-DOWN", "V2.0", "UI20S", "广西由莱", "医疗", "current")
        downgrade = self.admin.post(
            f"/api/files/{low_file['id']}/share",
            json=self.share_payload(self.admin, low_file["id"], **{
                "code": "BOM-DOWN",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(downgrade.status_code, 409, downgrade.text)
        self.assertIn("低于当前版本", downgrade.text)

    def test_share_file_rejects_stale_token_tampered_blob_and_reader_post(self):
        _source_record, source_file = self.h.seed_bom(self.admin, "BOM-STALE", "V1.0", "W20", "深圳金茉", "家电", "source")
        stale_payload = self.share_payload(self.admin, source_file["id"], **{
            "code": "BOM-STALE",
            "registrant": "广西由莱",
            "project_category": "医疗",
            "product": "UI20S",
            "module": "BOM",
        })
        self.h.seed_bom(self.admin, "BOM-TOKEN-CHANGE", "V1.0", "W20", "深圳金茉", "家电", "changes-token")
        stale = self.admin.post(f"/api/files/{source_file['id']}/share", json=stale_payload)
        self.assertEqual(stale.status_code, 409, stale.text)

        _tamper_record, tamper_file = self.h.seed_bom(self.admin, "BOM-TAMPER-SHARE", "V1.0", "W20", "深圳金茉", "家电", "source")
        blob_path = tamper_file["metadata"]["blob_path"]
        (self.h.store.data_dir / blob_path).write_bytes(b"tampered")
        tampered = self.admin.post(
            f"/api/files/{tamper_file['id']}/share",
            json=self.share_payload(self.admin, tamper_file["id"], **{
                "code": "BOM-TAMPER-SHARE",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            }),
        )
        self.assertEqual(tampered.status_code, 409, tampered.text)
        self.assertIn("校验失败", tampered.text)

        _reader_user, reader_password = self.h.create_user(self.admin, "share-reader", "reader")
        reader = self.h.login("share-reader", reader_password)
        reader_post = reader.post(
            f"/api/files/{source_file['id']}/share",
            json={
                "expected_edit_token": "reader-has-no-commit",
                "reason": "reader should be blocked",
                "code": "BOM-STALE",
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "module": "BOM",
            },
        )
        self.assertEqual(reader_post.status_code, 403, reader_post.text)

    def test_commit_initial_latest_only_must_be_boolean(self):
        upload = self.admin.post(
            "/api/analyze",
            json={"files": [{"name": "BOM-BOOL_V1.0-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-BOOL", "V1.0", "W20", "深圳金茉"))}]},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        response = self.admin.post("/api/commit", json={"batch_id": upload.json()["batch_id"], "initial_latest_only": "true"})
        self.assertEqual(response.status_code, 400, response.text)

    def test_new_subjects_archive_and_precheck(self):
        for subject in ("新加坡由莱", "越南由莱", "新加坡实业", "未配置主体"):
            response = self.admin.post("/api/analyze", json={
                "restrict_new_subject": True,
                "files": [{"name": f"BOM-NEW_V1.0-UI04【{subject}】-部件.xlsx",
                           "data": b64(xlsx_bom("BOM-NEW", "V1.0", "UI04", subject, subject))}]})
            self.assertEqual(response.status_code, 200, response.text)
            batch = response.json()
            item = batch["items"][0]
            expected = "conflict" if subject == "未配置主体" else "matched"
            self.assertEqual(item["status"], expected, item)
            self.assertEqual(batch["summary"][expected], 1)
            if expected == "conflict":
                reviewed = self.admin.post("/api/review", json={
                    "batch_id": batch["batch_id"], "item_id": item["id"],
                    "expected_edit_token": item["edit_token"], "restrict_new_subject": True})
                self.assertEqual(reviewed.status_code, 200, reviewed.text)
                self.assertEqual(reviewed.json()["item"]["status"], "conflict")
            result = self.admin.post("/api/commit", json={"batch_id": batch["batch_id"], "restrict_new_subject": True})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["committed"], 0 if expected == "conflict" else 1)

    def test_archive_subjects_and_restrict_new_subject_commit(self):
        subjects = self.admin.get("/api/archive-subjects")
        self.assertEqual(subjects.status_code, 200, subjects.text)
        self.assertEqual(subjects.json()["subjects"], sorted(["广西由莱", "深圳由莱", "深圳金茉", "新加坡由莱", "越南由莱", "新加坡实业"]))

        legacy = self.admin.post(
            "/api/analyze",
            json={"files": [{"name": "BOM-LEGACY_V1.0-W20【未配置主体】-部件.xlsx", "data": b64(xlsx_bom("BOM-LEGACY", "V1.0", "W20", "未配置主体", "legacy"))}]},
        )
        self.assertEqual(legacy.status_code, 200, legacy.text)
        unrestricted = self.admin.post("/api/commit", json={"batch_id": legacy.json()["batch_id"]})
        self.assertEqual(unrestricted.status_code, 200, unrestricted.text)
        self.assertEqual(unrestricted.json()["committed"], 1)
        legacy_update = self.admin.post(
            "/api/analyze",
            json={"files": [{"name": "BOM-LEGACY_V1.1-W20【未配置主体】-部件.xlsx", "data": b64(xlsx_bom("BOM-LEGACY", "V1.1", "W20", "未配置主体", "legacy-update"))}]},
        )
        self.assertEqual(legacy_update.status_code, 200, legacy_update.text)
        allowed_existing = self.admin.post("/api/commit", json={"batch_id": legacy_update.json()["batch_id"], "restrict_new_subject": True})
        self.assertEqual(allowed_existing.status_code, 200, allowed_existing.text)
        self.assertEqual(allowed_existing.json()["committed"], 1)

        blocked = self.admin.post(
            "/api/analyze",
            json={"files": [{"name": "BOM-BLOCK_V1.0-W21【未配置主体】-部件.xlsx", "data": b64(xlsx_bom("BOM-BLOCK", "V1.0", "W21", "未配置主体", "blocked"))}]},
        )
        self.assertEqual(blocked.status_code, 200, blocked.text)
        restricted = self.admin.post("/api/commit", json={"batch_id": blocked.json()["batch_id"], "restrict_new_subject": True})
        self.assertEqual(restricted.status_code, 200, restricted.text)
        self.assertEqual(restricted.json()["committed"], 0)
        self.assertEqual(restricted.json()["blocked"], 1)


if __name__ == "__main__":
    unittest.main()
