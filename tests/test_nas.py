
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = ROOT / "tests" / ".runtime"
sys.path.insert(0, str(ROOT))

from backend.config import AppConfig
from backend.identity import IdentityService
from backend.nas import SYNC_TOKEN_ENV, READ_BASE_URL_ENV, install_nas
from backend.service import DMRStore, json_loads, sha256_bytes

ADMIN_PASSWORD = "AdminPass123"
WORKER_HEADERS = {"Authorization": "Bearer worker-token"}


class NasHarness:
    def __init__(self):
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="nas-", dir=TEST_RUNTIME)
        self.data_dir = Path(self.tmp.name)
        self.store = DMRStore(self.data_dir, None)
        self.identity = IdentityService(self.store)
        self.config = AppConfig(root=ROOT, data_dir=self.data_dir, database_url=None, auth_mode="local", static_root=ROOT / "frontend" / "dist")
        self.app = FastAPI()
        self.nas = install_nas(self.app, self.store, self.identity, self.config)
        self.client = TestClient(self.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        self.admin_token, self.admin_user = self.identity.setup("佳佳", "jiajia", ADMIN_PASSWORD)
        self.admin = TestClient(self.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        self.admin.cookies.set("dmr_session", self.admin_token)

    def close(self):
        self.admin.close()
        self.client.close()
        self.store.close()
        self.tmp.cleanup()

    def create_user(self, username="owner-one", role="uploader"):
        password = f"{username.title().replace('-', '')}123A"
        user = self.identity.create_user(
            {
                "username": username,
                "display_name": username,
                "password": password,
                "role": role,
                "active": True,
                "scopes": [{"registrant": "*", "project_category": "*", "product": "*"}],
                "can_download": True,
                "can_history": True,
            },
            self.admin_user,
        )
        self.store.execute("UPDATE identity_users SET must_change_password=0 WHERE id=?", (user["id"],))
        return user

    def seed_file(self, owner_user_id=None, content=b"hello nas", record_id=None):
        now = "2026-09-22T10:00:00"
        record_id = record_id or "REC-1"
        if not self.store.execute("SELECT id FROM records WHERE id=?", (record_id,)).fetchone():
            self.store.execute(
                """INSERT INTO records(id,business_code,name,registrant,type,project_category,project_category_source,product,module,version,previous,file_name,date,controlled,source,owner,size,source_type,linked_bom,metadata,created_at,updated_at,edit_revision)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record_id, "BOM-001", "W20 BOM", "深圳金茉", "成品", "家电", "人工确认", "W20", "BOM", "20260922", "", "", "", "", "seed", "tester", "", "本地上传", "", "{}", now, now, 0),
            )
        batch_id = f"UPLOAD-SEED-{record_id}"
        self.store.execute("INSERT OR IGNORE INTO batches(id,status,created_at,updated_at,summary,owner_user_id) VALUES(?,?,?,?,?,?)", (batch_id, "committed", now, now, "{}", owner_user_id or ""))
        blob_id, rel_path, size = self.store.store_blob(content, f"{record_id}.xlsx")
        file_id = f"FILE-{record_id}"
        sha = sha256_bytes(content)
        self.store.execute(
            """INSERT INTO files(id,record_id,name,version,status,date,source,size,type,storage,sha256,blob_path,metadata,linked_bom,link_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, record_id, f"{record_id}.xlsx", "20260922", "当前有效", "2026-09-22", batch_id, "9 B", "XLS", "本地保存", sha, rel_path, "{}", "", "未关联", now),
        )
        return file_id, record_id, sha


class NasTests(unittest.TestCase):
    def setUp(self):
        self.h = NasHarness()
        self.env = patch.dict(os.environ, {SYNC_TOKEN_ENV: "worker-token", READ_BASE_URL_ENV: "https://nas-reader.local:8910"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.h.close()

    def test_admin_saves_encrypted_personal_account_and_worker_check_reports_failure(self):
        user = self.h.create_user()
        save = self.h.admin.put(f"/api/users/{user['id']}/nas-account", json={"account": "nas-owner", "password": "Secret192Pwd"})
        self.assertEqual(save.status_code, 200, save.text)
        body = save.json()
        self.assertEqual(body["account"], "nas-owner")
        self.assertTrue(body["password_configured"])
        self.assertNotIn("password", body)
        stored = self.h.store.execute("SELECT * FROM nas_credentials WHERE user_id=?", (user["id"],)).fetchone()
        self.assertNotIn("Secret192Pwd", stored["password_encrypted"])

        check = self.h.admin.post(f"/api/users/{user['id']}/nas-account/check")
        self.assertEqual(check.status_code, 200, check.text)
        claim = self.h.client.post("/api/storage-sync/account-checks/claim", headers=WORKER_HEADERS, json={}).json()
        self.assertEqual(claim[0]["user_id"], user["id"])
        creds = self.h.client.post(f"/api/storage-sync/account-checks/{user['id']}/credentials", headers=WORKER_HEADERS, json={"lease_token": claim[0]["lease_token"]})
        self.assertEqual(creds.status_code, 200, creds.text)
        self.assertEqual(creds.json(), {"account": "nas-owner", "password": "Secret192Pwd"})
        result = self.h.client.post(
            f"/api/storage-sync/account-checks/{user['id']}/result",
            headers=WORKER_HEADERS,
            json={"lease_token": claim[0]["lease_token"], "success": False, "error_code": "permission", "diagnostic": {"api": "SYNO.FileStation.Info", "nas_code": 105}},
        )
        self.assertEqual(result.status_code, 200, result.text)
        status = self.h.admin.get(f"/api/users/{user['id']}/nas-account").json()
        self.assertEqual(status["check_status"], "failed")
        self.assertIn("NAS错误码 105", status["check_error"])

    def test_changing_account_name_requires_new_password_but_same_account_can_keep_password(self):
        user = self.h.create_user("account-change")
        first = self.h.admin.put(f"/api/users/{user['id']}/nas-account", json={"account": "nas-original", "password": "Secret192Pwd"})
        self.assertEqual(first.status_code, 200, first.text)
        same = self.h.admin.put(f"/api/users/{user['id']}/nas-account", json={"account": "nas-original"})
        self.assertEqual(same.status_code, 200, same.text)
        changed = self.h.admin.put(f"/api/users/{user['id']}/nas-account", json={"account": "nas-new"})
        self.assertEqual(changed.status_code, 422, changed.text)
        current = self.h.admin.get(f"/api/users/{user['id']}/nas-account")
        self.assertEqual(current.json()["account"], "nas-original")


    def test_account_check_does_not_fake_success_without_service_configuration(self):
        self.env.stop()
        user = self.h.create_user("no-service")
        save = self.h.admin.put(f"/api/users/{user['id']}/nas-account", json={"account": "nas-no-service", "password": "Secret192Pwd"})
        self.assertEqual(save.status_code, 200, save.text)
        check = self.h.admin.post(f"/api/users/{user['id']}/nas-account/check")
        self.assertEqual(check.status_code, 503, check.text)
        self.assertIn("不能显示验证成功", check.text)
        self.env.start()

    def test_enqueue_requires_exact_record_mapping_then_claims_owner_job(self):
        owner = self.h.create_user("owner-sync")
        self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "Secret192Pwd"})
        file_id, record_id, sha = self.h.seed_file(owner["id"])
        blocked = self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("缺少NAS目录映射", blocked["last_error"])

        mapping = self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"})
        self.assertEqual(mapping.status_code, 200, mapping.text)
        job = self.h.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        self.assertEqual(job["status"], "blocked")
        retry = self.h.admin.post(f"/api/admin/storage-sync/files/{file_id}/retry", json={})
        self.assertEqual(retry.status_code, 200, retry.text)
        job = self.h.store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        self.assertEqual(job["status"], "pending")
        claim = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={"limit": 2})
        self.assertEqual(claim.status_code, 200, claim.text)
        job_claim = claim.json()[0]
        self.assertEqual(job_claim["file_id"], file_id)
        self.assertIn(file_id, job_claim["target_path"])
        self.assertIn(sha[:12], job_claim["target_path"])
        self.assertIn("REC-1.xlsx", job_claim["target_path"])
        self.assertEqual(job_claim["size"], len(b"hello nas"))
        self.assertEqual(job_claim["sha256"], sha)
        creds = self.h.client.post(f"/api/storage-sync/files/{file_id}/credentials", headers=WORKER_HEADERS, json={"lease_token": job_claim["lease_token"]})
        self.assertEqual(creds.status_code, 200, creds.text)
        self.assertEqual(creds.json()["account"], "nas-owner")

    def test_directory_creation_job_binds_mapping_only_after_worker_success(self):
        self.h.admin.put(f"/api/users/{self.h.admin_user['id']}/nas-account", json={"account": "nas-admin", "password": "AdminNasPwd"})
        file_id, record_id, _sha = self.h.seed_file(self.h.admin_user["id"], record_id="REC-DIR")
        blocked = self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.assertEqual(blocked["status"], "blocked")

        created = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件/2、首次量产BOM", "folder_name": "W20新产品"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        job = created.json()["job"]
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["nas_path"], "/体系文件/2、首次量产BOM/W20新产品")
        self.assertFalse(self.h.store.execute("SELECT * FROM nas_directory_mappings WHERE record_id=?", (record_id,)).fetchone())

        duplicate = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件/2、首次量产BOM", "folder_name": "W20新产品"},
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json()["job"]["id"], job["id"])

        claim = self.h.client.post("/api/storage-sync/directory-creation-jobs/claim", headers=WORKER_HEADERS, json={})
        self.assertEqual(claim.status_code, 200, claim.text)
        task = claim.json()[0]
        self.assertEqual(task["id"], job["id"])
        creds = self.h.client.post(f"/api/storage-sync/directory-creation-jobs/{job['id']}/credentials", headers=WORKER_HEADERS, json={"lease_token": task["lease_token"]})
        self.assertEqual(creds.status_code, 200, creds.text)
        self.assertEqual(creds.json(), {"account": "nas-admin", "password": "AdminNasPwd"})

        result = self.h.client.post(
            f"/api/storage-sync/directory-creation-jobs/{job['id']}/result",
            headers=WORKER_HEADERS,
            json={"lease_token": task["lease_token"], "success": True, "nas_path": task["nas_path"]},
        )
        self.assertEqual(result.status_code, 200, result.text)
        mapping = self.h.store.execute("SELECT * FROM nas_directory_mappings WHERE record_id=?", (record_id,)).fetchone()
        self.assertEqual(mapping["nas_path"], task["nas_path"])
        refreshed = self.h.store.execute("SELECT status,target_path FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        self.assertEqual(refreshed["status"], "pending")
        self.assertIn(task["nas_path"], refreshed["target_path"])

    def test_directory_creation_requires_admin_browser_permission(self):
        user = self.h.create_user("dir-reader", role="reader")
        self.h.admin.put(f"/api/users/{user['id']}/nas-account", json={"account": "reader-nas", "password": "ReaderNasPwd"})
        _file_id, record_id, _sha = self.h.seed_file(user["id"], record_id="REC-DIR-READER")
        token = self.h.identity.login("dir-reader", "DirReader123A")[0]
        reader = TestClient(self.h.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        reader.cookies.set("dmr_session", token)
        try:
            response = reader.post(
                "/api/nas/directory-creation-jobs",
                json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "读者目录"},
            )
            self.assertEqual(response.status_code, 403, response.text)
        finally:
            reader.close()

    def test_directory_creation_stale_mapping_and_account_change_are_rejected(self):
        self.h.admin.put(f"/api/users/{self.h.admin_user['id']}/nas-account", json={"account": "nas-admin", "password": "AdminNasPwd"})
        _file_id, record_id, _sha = self.h.seed_file(self.h.admin_user["id"], record_id="REC-DIR-STALE")
        job = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "新目录"},
        ).json()["job"]
        self.h.admin.put(f"/api/users/{self.h.admin_user['id']}/nas-account", json={"account": "nas-admin", "password": "NewAdminNasPwd"})
        claim = self.h.client.post("/api/storage-sync/directory-creation-jobs/claim", headers=WORKER_HEADERS, json={})
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertEqual(claim.json(), [])
        row = self.h.store.execute("SELECT status,last_error FROM nas_directory_creation_jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("账号", row["last_error"])

        second = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "新目录"},
        ).json()["job"]
        self.assertNotEqual(second["id"], job["id"])
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/既有目录"})
        claim = self.h.client.post("/api/storage-sync/directory-creation-jobs/claim", headers=WORKER_HEADERS, json={})
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertEqual(claim.json(), [])
        row = self.h.store.execute("SELECT status,last_error FROM nas_directory_creation_jobs WHERE id=?", (second["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("映射已变化", row["last_error"])

    def test_directory_creation_result_persists_failed_status_when_mapping_changes_midflight(self):
        self.h.admin.put(f"/api/users/{self.h.admin_user['id']}/nas-account", json={"account": "nas-admin", "password": "AdminNasPwd"})
        _file_id, record_id, _sha = self.h.seed_file(self.h.admin_user["id"], record_id="REC-DIR-MIDFLIGHT")
        job = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "新目录"},
        ).json()["job"]
        task = self.h.client.post("/api/storage-sync/directory-creation-jobs/claim", headers=WORKER_HEADERS, json={}).json()[0]
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/别的目录"})
        result = self.h.client.post(
            f"/api/storage-sync/directory-creation-jobs/{job['id']}/result",
            headers=WORKER_HEADERS,
            json={"lease_token": task["lease_token"], "success": True, "nas_path": task["nas_path"]},
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "failed")
        row = self.h.store.execute("SELECT status,last_error FROM nas_directory_creation_jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("映射已变化", row["last_error"])
        mapping = self.h.store.execute("SELECT nas_path FROM nas_directory_mappings WHERE record_id=? AND active=1", (record_id,)).fetchone()
        self.assertEqual(mapping["nas_path"], "/体系文件/别的目录")

    def test_directory_creation_credentials_recheck_authority_and_record_snapshot(self):
        self.h.admin.put(f"/api/users/{self.h.admin_user['id']}/nas-account", json={"account": "nas-admin", "password": "AdminNasPwd"})
        _file_id, record_id, _sha = self.h.seed_file(self.h.admin_user["id"], record_id="REC-DIR-AUTH")
        job = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "权限目录"},
        ).json()["job"]
        task = self.h.client.post("/api/storage-sync/directory-creation-jobs/claim", headers=WORKER_HEADERS, json={}).json()[0]
        self.h.store.execute("UPDATE records SET product='W20改动',edit_revision=edit_revision+1 WHERE id=?", (record_id,))
        creds = self.h.client.post(f"/api/storage-sync/directory-creation-jobs/{job['id']}/credentials", headers=WORKER_HEADERS, json={"lease_token": task["lease_token"]})
        self.assertEqual(creds.status_code, 409, creds.text)
        row = self.h.store.execute("SELECT status,last_error FROM nas_directory_creation_jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("已变化", row["last_error"])

        self.h.store.execute("UPDATE records SET product='W20',edit_revision=edit_revision+1 WHERE id=?", (record_id,))
        job = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "权限目录"},
        ).json()["job"]
        self.h.store.execute("UPDATE identity_users SET role='document_control' WHERE id=?", (self.h.admin_user["id"],))
        claim = self.h.client.post("/api/storage-sync/directory-creation-jobs/claim", headers=WORKER_HEADERS, json={})
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertEqual(claim.json(), [])
        row = self.h.store.execute("SELECT status,last_error FROM nas_directory_creation_jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("无权管理", row["last_error"])

    def test_directory_creation_pending_idempotency_is_per_actor_and_credential_revision(self):
        other = self.h.create_user("other-admin", role="admin")
        self.h.admin.put(f"/api/users/{self.h.admin_user['id']}/nas-account", json={"account": "nas-admin", "password": "AdminNasPwd"})
        self.h.admin.put(f"/api/users/{other['id']}/nas-account", json={"account": "other-nas", "password": "OtherNasPwd"})
        _file_id, record_id, _sha = self.h.seed_file(self.h.admin_user["id"], record_id="REC-DIR-IDEMP")
        first = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "同名目录"},
        ).json()["job"]
        again = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "同名目录"},
        ).json()["job"]
        self.assertEqual(again["id"], first["id"])
        self.h.admin.put(f"/api/users/{self.h.admin_user['id']}/nas-account", json={"account": "nas-admin", "password": "AdminNasPwd2"})
        changed = self.h.admin.post(
            "/api/nas/directory-creation-jobs",
            json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "同名目录"},
        ).json()["job"]
        self.assertNotEqual(changed["id"], first["id"])

        other_token = self.h.identity.login("other-admin", "OtherAdmin123A")[0]
        other_client = TestClient(self.h.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        other_client.cookies.set("dmr_session", other_token)
        try:
            other_job = other_client.post(
                "/api/nas/directory-creation-jobs",
                json={"record_id": record_id, "parent_path": "/体系文件", "folder_name": "同名目录"},
            ).json()["job"]
        finally:
            other_client.close()
        self.assertNotEqual(other_job["id"], changed["id"])

    def test_worker_lease_result_retry_and_stale_rejection(self):
        owner = self.h.create_user("owner-retry")
        self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "Secret192Pwd"})
        file_id, record_id, _sha = self.h.seed_file(owner["id"], record_id="REC-2")
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"})
        self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.h.admin.post(f"/api/admin/storage-sync/files/{file_id}/retry", json={})
        claim = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={}).json()[0]
        failed = self.h.client.post(f"/api/storage-sync/files/{file_id}/result", headers=WORKER_HEADERS, json={"lease_token": claim["lease_token"], "success": False, "error_code": "network"})
        self.assertEqual(failed.status_code, 200, failed.text)
        immediate = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={})
        self.assertEqual(immediate.json(), [])
        self.h.store.execute("UPDATE nas_sync_jobs SET next_attempt_at='2026-01-01T00:00:00' WHERE file_id=?", (file_id,))
        second = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={}).json()[0]
        ok = self.h.client.post(f"/api/storage-sync/files/{file_id}/result", headers=WORKER_HEADERS, json={"lease_token": second["lease_token"], "success": True, "target_path": second["target_path"], "sha256": self.h.store.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()["sha256"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        duplicate = self.h.client.post(f"/api/storage-sync/files/{file_id}/result", headers=WORKER_HEADERS, json={"lease_token": second["lease_token"], "success": True, "target_path": second["target_path"], "sha256": self.h.store.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()["sha256"]})
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        mismatched = self.h.client.post(f"/api/storage-sync/files/{file_id}/result", headers=WORKER_HEADERS, json={"lease_token": second["lease_token"], "success": True, "target_path": "/wrong", "sha256": "bad"})
        self.assertEqual(mismatched.status_code, 409, mismatched.text)
        stale = self.h.client.post(f"/api/storage-sync/files/{file_id}/heartbeat", headers=WORKER_HEADERS, json={"lease_token": claim["lease_token"]})
        self.assertEqual(stale.status_code, 409, stale.text)
        file_row = self.h.store.execute("SELECT storage FROM files WHERE id=?", (file_id,)).fetchone()
        self.assertEqual(file_row["storage"], "本地保存 + NAS已同步")

    def test_read_ticket_uses_current_viewer_account_and_redeems_once(self):
        owner = self.h.create_user("owner-read")
        viewer = self.h.create_user("viewer-read", role="reader")
        self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "OwnerPwd192"})
        self.h.admin.put(f"/api/users/{viewer['id']}/nas-account", json={"account": "nas-viewer", "password": "ViewerPwd192"})
        file_id, record_id, _sha = self.h.seed_file(owner["id"], record_id="REC-3")
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"})
        self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.h.admin.post(f"/api/admin/storage-sync/files/{file_id}/retry", json={})
        claim = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={}).json()[0]
        self.h.client.post(f"/api/storage-sync/files/{file_id}/result", headers=WORKER_HEADERS, json={"lease_token": claim["lease_token"], "success": True, "target_path": claim["target_path"], "sha256": self.h.store.execute("SELECT sha256 FROM files WHERE id=?", (file_id,)).fetchone()["sha256"]})

        viewer_token = self.h.identity.login("viewer-read", "ViewerRead123A")[0]
        viewer_client = TestClient(self.h.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        viewer_client.cookies.set("dmr_session", viewer_token)
        ticket = viewer_client.post(f"/api/nas/files/{file_id}/read-ticket")
        self.assertEqual(ticket.status_code, 200, ticket.text)
        self.assertEqual(ticket.json()["mode"], "nas")
        auth = self.h.client.post("/api/storage-sync/read/authorize", headers=WORKER_HEADERS, json={"ticket": ticket.json()["ticket"]})
        self.assertEqual(auth.status_code, 200, auth.text)
        self.assertEqual(auth.json()["account"], "nas-viewer")
        self.assertEqual(auth.json()["password"], "ViewerPwd192")
        self.assertEqual(auth.json()["size"], len(b"hello nas"))
        self.assertEqual(auth.json()["action"], "download")
        again = self.h.client.post("/api/storage-sync/read/authorize", headers=WORKER_HEADERS, json={"ticket": ticket.json()["ticket"]})
        self.assertEqual(again.status_code, 409, again.text)
        viewer_client.close()

    def test_missing_original_uploader_blocks_nas_enqueue_without_actor_fallback(self):
        file_id, _record_id, _sha = self.h.seed_file(owner_user_id="", record_id="REC-NOOWNER")
        job = self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("缺少原始上传人", job["last_error"])
        self.assertEqual(job["owner_user_id"], "")


    def test_parallel_worker_claims_do_not_duplicate_same_file(self):
        import concurrent.futures

        owner = self.h.create_user("owner-parallel")
        self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "Secret192Pwd"})
        file_id, record_id, _sha = self.h.seed_file(owner["id"], record_id="REC-5")
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"})
        self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.h.admin.post(f"/api/admin/storage-sync/files/{file_id}/retry", json={})

        def claim_once():
            client = TestClient(self.h.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
            try:
                response = client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={"limit": 1})
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()
            finally:
                client.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim_once(), range(2)))
        claimed = [job for batch in results for job in batch]
        self.assertEqual([job["file_id"] for job in claimed], [file_id])


    def test_clear_and_recreate_account_invalidates_old_worker_lease(self):
        owner = self.h.create_user("owner-aba")
        self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "Secret192Pwd"})
        file_id, record_id, _sha = self.h.seed_file(owner["id"], record_id="REC-ABA")
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"})
        self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.h.admin.post(f"/api/admin/storage-sync/files/{file_id}/retry", json={})
        claim = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={}).json()[0]
        self.h.client.post(f"/api/storage-sync/files/{file_id}/credentials", headers=WORKER_HEADERS, json={"lease_token": claim["lease_token"]})
        cleared = self.h.admin.delete(f"/api/users/{owner['id']}/nas-account")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        recreated = self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "NewSecret192Pwd"})
        self.assertEqual(recreated.status_code, 200, recreated.text)
        old_result = self.h.client.post(f"/api/storage-sync/files/{file_id}/result", headers=WORKER_HEADERS, json={"lease_token": claim["lease_token"], "success": True, "target_path": claim["target_path"], "sha256": claim["sha256"]})
        self.assertEqual(old_result.status_code, 409, old_result.text)
        row = self.h.store.execute("SELECT status,last_error,lease_token FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertIsNone(row["lease_token"])
        self.assertIn("已清除", row["last_error"])


    def test_worker_and_read_operations_write_password_free_audit(self):
        owner = self.h.create_user("owner-audit")
        viewer = self.h.create_user("viewer-audit", role="reader")
        self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "OwnerPwd192"})
        self.h.admin.put(f"/api/users/{viewer['id']}/nas-account", json={"account": "nas-viewer", "password": "ViewerPwd192"})
        file_id, record_id, _sha = self.h.seed_file(owner["id"], record_id="REC-AUDIT")
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"})
        self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.h.admin.post(f"/api/admin/storage-sync/files/{file_id}/retry", json={})
        claim = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={}).json()[0]
        self.h.client.post(f"/api/storage-sync/files/{file_id}/credentials", headers=WORKER_HEADERS, json={"lease_token": claim["lease_token"]})
        self.h.client.post(f"/api/storage-sync/files/{file_id}/result", headers=WORKER_HEADERS, json={"lease_token": claim["lease_token"], "success": True, "target_path": claim["target_path"], "sha256": claim["sha256"]})
        viewer_token = self.h.identity.login("viewer-audit", "ViewerAudit123A")[0]
        viewer_client = TestClient(self.h.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        viewer_client.cookies.set("dmr_session", viewer_token)
        ticket = viewer_client.post(f"/api/nas/files/{file_id}/read-ticket", json={"action": "preview"}).json()["ticket"]
        self.h.client.post("/api/storage-sync/read/authorize", headers=WORKER_HEADERS, json={"ticket": ticket})
        viewer_client.close()
        rows = self.h.store.execute("SELECT action,detail FROM identity_audit WHERE action IN ('worker_read_nas_credentials','nas_sync_result','nas_read_redeem')").fetchall()
        actions = {row["action"] for row in rows}
        self.assertTrue({'worker_read_nas_credentials','nas_sync_result','nas_read_redeem'} <= actions)
        joined = " ".join(row["detail"] for row in rows)
        self.assertNotIn("OwnerPwd192", joined)
        self.assertNotIn("ViewerPwd192", joined)


    def test_disabled_mapping_blocks_pending_claim_until_explicit_retry_after_remap(self):
        owner = self.h.create_user("owner-disable")
        self.h.admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "nas-owner", "password": "Secret192Pwd"})
        file_id, record_id, _sha = self.h.seed_file(owner["id"], record_id="REC-4")
        self.h.admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"})
        self.h.nas.enqueue_file(file_id, self.h.admin_user)
        self.h.admin.post(f"/api/admin/storage-sync/files/{file_id}/retry", json={})
        disabled = self.h.admin.delete(f"/api/nas/directory-mappings/{record_id}")
        self.assertEqual(disabled.status_code, 200, disabled.text)
        claim = self.h.client.post("/api/storage-sync/claim", headers=WORKER_HEADERS, json={})
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertEqual(claim.json(), [])
        row = self.h.store.execute("SELECT status,last_error FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertIn("映射", row["last_error"])


if __name__ == "__main__":
    unittest.main()
