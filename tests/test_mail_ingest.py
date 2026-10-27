import base64
import datetime as dt
import hashlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = ROOT / "tests" / ".runtime"
REAL_BOM = Path(os.environ.get("DMR_REAL_BOM_SAMPLE", "tests/fixtures/real-bom.xlsx"))

sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from backend.config import AppConfig
from backend.identity import IdentityService
from backend.service import DMRStore
import server as server_module
from server import create_app
from test_backend import b64, xlsx_bom


ADMIN_PASSWORD = "AdminPass123"
MAIL_TOKEN = "mail-ingest-token-for-tests"


def future_iso(days=2):
    return (dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(days=days)).isoformat()


def past_iso(days=2):
    return (dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(days=days)).isoformat()


class MailIngestHarness:
    def __init__(self, *, production=False):
        self.production = production
        self.base_url = "https://dmr.example.test" if production else "http://127.0.0.1"
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="mail-ingest-", dir=TEST_RUNTIME)
        self.data_dir = Path(self.tmp.name)
        self.store = DMRStore(self.data_dir, None)
        if production:
            IdentityService(self.store).setup("佳佳", "jiajia", ADMIN_PASSWORD)
        self.config = AppConfig(
            root=ROOT,
            data_dir=self.data_dir,
            database_url=None,
            auth_mode="local",
            static_root=ROOT / "frontend" / "dist",
            local_user="",
            local_roles="",
            environment="production" if production else "local",
            allowed_hosts=("127.0.0.1", "127.0.0.1:443", "dmr.example.test"),
            trusted_origins=("https://dmr.example.test",) if production else ("http://127.0.0.1", "http://localhost"),
            public_origin="https://dmr.example.test" if production else None,
            cookie_secure=production,
            setup_allowed=not production,
        )
        self.app = create_app(self.store, self.config)
        self.clients = []
        self.mail_tokens = {}

    def close(self):
        for client in self.clients:
            client.close()
        self.store.close()
        self.tmp.cleanup()

    def client(self, *, origin=None):
        client = TestClient(self.app, base_url=self.base_url, raise_server_exceptions=False)
        if origin is not None:
            client.headers.update({"Origin": origin})
        self.clients.append(client)
        return client

    def setup_admin(self):
        client = self.client(origin="https://dmr.example.test" if self.production else None)
        status = client.get("/api/auth/status")
        self.assert_ok(status)
        if status.json().get("setup_required"):
            response = client.post("/api/auth/setup", json={"display_name": "佳佳", "username": "jiajia", "password": ADMIN_PASSWORD})
        else:
            response = client.post("/api/auth/login", json={"username": "jiajia", "password": ADMIN_PASSWORD})
        self.assert_ok(response)
        return client, response.json()["user"]

    def login(self, username, password):
        client = self.client(origin="https://dmr.example.test" if self.production else None)
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assert_ok(response)
        return client

    def create_user(self, admin, username, role="document_control", scopes=None, *, active=True):
        password = f"{username.title().replace('-', '')}Pass123"
        response = admin.post(
            "/api/users",
            json={
                "username": username,
                "display_name": username,
                "password": password,
                "role": role,
                "active": active,
                "scopes": scopes if scopes is not None else [{"registrant": "*", "project_category": "*", "product": "*"}],
                "can_download": True,
                "can_history": True,
            },
        )
        self.assert_ok(response)
        user = response.json()["user"]
        if active:
            first_login = self.login(username, password)
            final_password = f"{username.title().replace('-', '')}Ready123"
            change = first_login.post("/api/auth/change-password", json={"current_password": password, "new_password": final_password})
            self.assert_ok(change)
            return user, final_password
        return user, password

    def create_mail_client(self, admin, name="DCC mailbox", owner_user_id=None, mailbox="dcc@example.test", expires_at=None):
        if owner_user_id is None:
            owner_user_id = admin.get("/api/auth/me").json()["user"]["id"]
        response = admin.post(
            "/api/integrations/mail/clients",
            json={
                "name": name,
                "owner_user_id": owner_user_id,
                "mailbox": mailbox,
                "expires_at": expires_at or future_iso(),
            },
        )
        self.assert_ok(response)
        body = response.json()
        client_body = body.get("client", body)
        if "bearer_token" in client_body:
            self.mail_tokens[client_body["mailbox"]] = client_body["bearer_token"]
        return client_body

    def bearer_client(self, token=None, mailbox="dcc@example.test"):
        client = self.client()
        if token is None:
            token = self.mail_tokens.get(mailbox, MAIL_TOKEN)
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client

    def assert_ok(self, response):
        if response.status_code >= 400:
            raise AssertionError((response.status_code, response.text[:500]))


class MailIngestTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"DMR_MAIL_INGEST_TOKEN": MAIL_TOKEN, "TMPDIR": str(TEST_RUNTIME), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.h = MailIngestHarness()
        self.admin, self.admin_user = self.h.setup_admin()

    def tearDown(self):
        self.h.close()
        self.env.stop()

    def content_payload(self, content, filename="AUTO001_V1.0-W20【深圳金茉】-部件.xlsx", attachment_id="att-1", **overrides):
        sha = hashlib.sha256(content).hexdigest()
        payload = {
            "mailbox": "dcc@example.test",
            "message_id": "msg-1",
            "subject": "DCC BOM update",
            "sender": "dcc-sender@example.test",
            "received_at": "2026-09-22T10:00:00+00:00",
            "attachment": {
                "attachment_id": attachment_id,
                "filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "size": len(content),
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "sha256": sha,
            },
            "biz_keyword": "W20",
            "test_case": "mail-ingest",
        }
        for key, value in overrides.items():
            if key == "attachment":
                payload["attachment"].update(value)
            else:
                payload[key] = value
        return payload

    def ingest(self, payload, status=201, token=None):
        response = self.h.bearer_client(token, mailbox=payload.get("mailbox", "dcc@example.test")).post("/api/integrations/mail/attachments", json=payload)
        self.assertEqual(response.status_code, status, response.text)
        return response

    def test_ecn_attachment_is_rejected_without_storing_file(self):
        self.h.create_mail_client(self.admin)
        for marker in ("ECN", "ENC"):
            payload = self.content_payload(b"sample", subject=f"深圳由莱-{marker}（2026100005）BOM 变更")
            self.ingest(payload, status=422)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS n FROM mail_receipts").fetchone()["n"], 0)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS n FROM batch_items").fetchone()["n"], 0)
        self.assertEqual(list(self.h.store.files_dir.iterdir()), [])

    def test_admin_can_create_list_and_delete_mail_clients(self):
        owner, _password = self.h.create_user(self.admin, "dcc-owner", role="document_control")
        created = self.h.create_mail_client(self.admin, owner_user_id=owner["id"], mailbox="dcc@example.test")
        self.assertEqual(created["name"], "DCC mailbox")
        self.assertEqual(created["owner_user_id"], owner["id"])
        self.assertEqual(created["mailbox"], "dcc@example.test")
        self.assertIn("id", created)

        listed = self.admin.get("/api/integrations/mail/clients")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertTrue(any(row["id"] == created["id"] and row["mailbox"] == "dcc@example.test" for row in listed.json()))

        deleted = self.admin.delete(f"/api/integrations/mail/clients/{created['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["revoked_at"])
        relisted = self.admin.get("/api/integrations/mail/clients")
        self.assertEqual(relisted.status_code, 200, relisted.text)
        revoked = next(row for row in relisted.json() if row["id"] == created["id"])
        self.assertTrue(revoked["revoked_at"])

    def test_bearer_ingest_creates_reviewable_batch_and_receipt(self):
        owner, _password = self.h.create_user(self.admin, "mail-owner", role="document_control")
        self.h.create_mail_client(self.admin, owner_user_id=owner["id"], mailbox="dcc@example.test")
        content = xlsx_bom("AUTO001", "V1.0", "W20", "深圳金茉", "mail")

        response = self.ingest(self.content_payload(content))
        body = response.json()
        self.assertEqual(body["status"], "awaiting_confirmation")
        self.assertEqual(body["classification"], "matched")
        self.assertIn("receipt_id", body)
        self.assertIn("batch_id", body)
        self.assertIn("item_id", body)

        batch = self.admin.get(f"/api/batches/{body['batch_id']}")
        self.assertEqual(batch.status_code, 200, batch.text)
        item = next(row for row in batch.json()["items"] if row["id"] == body["item_id"])
        self.assertEqual(item["fileName"], "AUTO001_V1.0-W20【深圳金茉】-部件.xlsx")
        self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(batch.json()["owner_user_id"], owner["id"])

        receipt = self.h.bearer_client().get(f"/api/integrations/mail/receipts/{body['receipt_id']}")
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["batch_id"], body["batch_id"])
        self.assertEqual(receipt.json()["item_id"], body["item_id"])
        self.assertEqual(receipt.json()["sha256"], hashlib.sha256(content).hexdigest())

        commit = self.admin.post("/api/commit", json={"batch_id": body["batch_id"]})
        self.assertEqual(commit.status_code, 200, commit.text)
        self.assertEqual(commit.json()["committed"], 1)
        self.assertEqual(commit.json()["blocked"], 0)
        final = self.h.bearer_client().get(f"/api/integrations/mail/receipts/{body['receipt_id']}")
        self.assertEqual(final.json()["status"], "archived")

    def test_mail_subject_can_fill_missing_matching_fields_without_using_title_version(self):
        self.h.create_mail_client(self.admin)
        payload = self.content_payload(
            b"mail title evidence",
            filename="MAILTITLE001_V1.0.hex",
            attachment_id="mail-title-fill",
            subject="广西由莱-医疗项目-UI06S PR-DMR 软件资料 V9.9",
            attachment={"content_type": "application/octet-stream"},
        )

        response = self.ingest(payload)
        body = response.json()
        self.assertEqual(body["classification"], "matched", body)
        batch = self.admin.get(f"/api/batches/{body['batch_id']}").json()
        item = batch["items"][0]
        self.assertEqual(item["registrant"], "广西由莱")
        self.assertEqual(item["project_category"], "医疗")
        self.assertEqual(item["project_category_source"], "邮件标题")
        self.assertEqual(item["product"], "UI06S PR")
        self.assertEqual(item["module"], "软件")
        self.assertEqual(item["version"], "V1.0")
        self.assertEqual(item["metadata"]["mail_title"]["applied"], ["registrant", "project_category", "product"])
        self.assertNotIn("version", item["metadata"]["mail_title"]["fields"])

    def test_mail_subject_conflict_requires_manual_review_and_does_not_overwrite_file_evidence(self):
        self.h.create_mail_client(self.admin)
        content = xlsx_bom("MAILTITLE002", "V1.0", "W20", "深圳金茉", "title-conflict")
        payload = self.content_payload(
            content,
            filename="MAILTITLE002_V1.0-W20【深圳金茉】-成品.xlsx",
            attachment_id="mail-title-conflict",
            subject="广西由莱-医疗项目-W20-DMR BOM",
        )

        response = self.ingest(payload)
        body = response.json()
        self.assertEqual(body["classification"], "conflict", body)
        batch = self.admin.get(f"/api/batches/{body['batch_id']}").json()
        item = batch["items"][0]
        self.assertEqual(item["registrant"], "深圳金茉")
        self.assertEqual(item["project_category"], "家电")
        self.assertIn("邮件标题与文件解析结果不一致", item["recommendation"])
        conflicts = {row["field"]: row for row in item["metadata"]["mail_title"]["conflicts"]}
        self.assertEqual(conflicts["registrant"]["mail_title"], "广西由莱")
        self.assertEqual(conflicts["project_category"]["mail_title"], "医疗")

        commit = self.admin.post("/api/commit", json={"batch_id": body["batch_id"]})
        self.assertEqual(commit.status_code, 200, commit.text)
        self.assertEqual(commit.json()["committed"], 0)
        self.assertEqual(commit.json()["blocked"], 1)

    def test_mail_subject_filled_fields_still_obey_owner_scope(self):
        owner, _password = self.h.create_user(
            self.admin,
            "mail-scope-owner",
            role="document_control",
            scopes=[{"registrant": "深圳金茉", "project_category": "*", "product": "*"}],
        )
        client = self.h.create_mail_client(self.admin, owner_user_id=owner["id"], mailbox="scoped@example.test")
        payload = self.content_payload(
            b"mail title evidence",
            filename="MAILTITLE-SCOPE_V1.0.hex",
            attachment_id="mail-title-scope",
            mailbox="scoped@example.test",
            subject="广西由莱-医疗项目-UI06S PR-DMR 软件资料",
            attachment={"content_type": "application/octet-stream"},
        )

        response = self.ingest(payload, status=403, token=client["bearer_token"])
        self.assertIn("超出当前人员获授权的范围", response.text)

    def test_manual_review_commit_and_receipt_follow_real_workflow(self):
        self.h.create_mail_client(self.admin)
        payload = self.content_payload(xlsx_bom("AUTO-LIFE", "V1.0", "W20", "深圳金茉"),
                                       message_id="mail/id+=", attachment_id="att/id+=")
        receipt = self.ingest(payload).json()
        batch_id = receipt["batch_id"]
        item = self.admin.get(f"/api/batches/{batch_id}").json()["items"][0]
        pending = self.admin.post("/api/review", json={"batch_id": batch_id,
            "item_id": item["id"], "expected_edit_token": item["edit_token"],
            "save_pending": True, "review_note": "测试：暂待文控确认"})
        self.assertEqual(pending.status_code, 200, pending.text)
        path = f"/api/integrations/mail/receipts/{receipt['receipt_id']}"
        self.assertEqual(self.h.bearer_client().get(path).json()["status"], "needs_review")
        item = self.admin.get(f"/api/batches/{batch_id}").json()["items"][0]
        reviewed = self.admin.post("/api/review", json={"batch_id": batch_id,
            "item_id": item["id"], "expected_edit_token": item["edit_token"],
            "code": "AUTO-LIFE", "registrant": "深圳金茉", "product": "W20", "module": "BOM",
            "type": "部件", "version": "V1.0", "project_category": "家电"})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(self.h.bearer_client().get(path).json()["status"], "awaiting_confirmation")
        committed = self.admin.post("/api/commit", json={"batch_id": batch_id})
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(committed.json()["committed"], 1, committed.text)
        status = self.h.bearer_client().get(path)
        self.assertEqual(status.json()["status"], "archived", status.text)
        self.assertEqual(self.ingest(payload, status=200).json()["status"], "archived")
        record = self.h.store.execute("SELECT metadata FROM files").fetchone()
        self.assertIn("email_source", record["metadata"])

    def test_post_analysis_failure_rolls_back_database_and_staged_blob(self):
        self.h.create_mail_client(self.admin)
        original_execute = self.h.store.execute
        def fail_receipt(sql, params=()):
            if "INSERT INTO mail_receipts" in sql:
                raise RuntimeError("simulated receipt failure")
            return original_execute(sql, params)
        with patch.object(self.h.store, "execute", side_effect=fail_receipt):
            self.ingest(self.content_payload(xlsx_bom("AUTO-ROLLBACK", "V1.0", "W20", "深圳金茉")), status=500)
        for table in ("batches", "batch_items", "mail_receipts"):
            self.assertEqual(self.h.store.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], 0)
        self.assertEqual([p for p in (self.h.data_dir / "files").rglob("*") if p.is_file()], [])

    def test_retry_is_idempotent_but_same_attachment_id_different_bytes_conflicts(self):
        self.h.create_mail_client(self.admin)
        first_bytes = xlsx_bom("AUTO002", "V1.0", "W20", "深圳金茉", "first")
        first = self.content_payload(first_bytes, filename="AUTO002_V1.0-W20【深圳金茉】-部件.xlsx")

        created = self.ingest(first).json()
        retried = self.ingest(first, status=200).json()
        self.assertEqual(retried["receipt_id"], created["receipt_id"])
        self.assertEqual(retried["batch_id"], created["batch_id"])
        self.assertEqual(retried["item_id"], created["item_id"])
        rows = self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()
        self.assertEqual(rows["count"], 1)

        changed = self.content_payload(
            xlsx_bom("AUTO002", "V1.0", "W20", "深圳金茉", "changed"),
            filename="AUTO002_V1.0-W20【深圳金茉】-部件.xlsx",
        )
        self.ingest(changed, status=409)
        rows = self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()
        self.assertEqual(rows["count"], 1)

    def test_distinct_attachment_from_same_message_is_accepted(self):
        self.h.create_mail_client(self.admin)
        first = self.content_payload(xlsx_bom("AUTO003", "V1.0", "W20", "深圳金茉", "one"), filename="AUTO003_V1.0-W20【深圳金茉】-部件.xlsx", attachment_id="att-1")
        second = self.content_payload(
            xlsx_bom("AUTO004", "V1.0", "W20", "深圳金茉", "two"),
            filename="AUTO004_V1.0-W20【深圳金茉】-部件.xlsx",
            attachment_id="att-2",
        )
        one = self.ingest(first).json()
        two = self.ingest(second).json()
        self.assertNotEqual(one["receipt_id"], two["receipt_id"])
        self.assertNotEqual(one["item_id"], two["item_id"])
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 2)

    def test_missing_wrong_revoked_and_expired_bearer_tokens_are_rejected(self):
        self.h.create_mail_client(self.admin)
        payload = self.content_payload(xlsx_bom("AUTO005", "V1.0", "W20", "深圳金茉"))
        plain = self.h.client().post("/api/integrations/mail/attachments", json=payload)
        self.assertEqual(plain.status_code, 401, plain.text)
        self.ingest(payload, status=401, token="wrong-token")

        self.h.store.execute("UPDATE mail_clients SET revoked_at=? WHERE mailbox=?", (dt.datetime.now(dt.timezone.utc).isoformat(), "dcc@example.test"))
        self.ingest(payload, status=401)

        self.h.store.execute("UPDATE mail_clients SET revoked_at=NULL, expires_at=? WHERE mailbox=?", (past_iso(), "dcc@example.test"))
        self.ingest(payload, status=401)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 0)

    def test_ui_session_cannot_use_machine_endpoint_and_bearer_token_cannot_commit(self):
        self.h.create_mail_client(self.admin)
        payload = self.content_payload(xlsx_bom("AUTO006", "V1.0", "W20", "深圳金茉"))
        cookie_only = self.admin.post("/api/integrations/mail/attachments", json=payload)
        self.assertEqual(cookie_only.status_code, 401, cookie_only.text)

        bearer_commit = self.h.bearer_client().post("/api/commit", json={"batch_id": "anything"})
        self.assertEqual(bearer_commit.status_code, 401, bearer_commit.text)

    def test_valid_bearer_still_rejects_bad_origin(self):
        self.h.create_mail_client(self.admin)
        payload = self.content_payload(xlsx_bom("AUTO006A", "V1.0", "W20", "深圳金茉"))
        response = self.h.bearer_client().post(
            "/api/integrations/mail/attachments",
            json=payload,
            headers={"Origin": "https://evil.example.test"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 0)

    def test_valid_bearer_machine_post_allows_missing_origin_in_production(self):
        prod = MailIngestHarness(production=True)
        try:
            admin, _admin_user = prod.setup_admin()
            prod.create_mail_client(admin)
            payload = self.content_payload(xlsx_bom("AUTO006B", "V1.0", "W20", "深圳金茉"))
            response = prod.bearer_client().post("/api/integrations/mail/attachments", json=payload)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(prod.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 1)
        finally:
            prod.close()

    def test_chunked_mail_ingest_body_over_max_body_without_content_length_is_rejected(self):
        self.h.create_mail_client(self.admin)
        old_max = server_module.MAX_BODY
        server_module.MAX_BODY = 128
        try:
            payload = b'{"mailbox":"dcc@example.test","message_id":"chunked","subject":"oversize","sender":"dcc@example.test","received_at":"2026-09-22T10:00:00+00:00","attachment":{"attachment_id":"chunked-att","filename":"chunked.xlsx","content_base64":"' + (b"A" * 256) + b'","size":192}}'
            response = self.h.bearer_client().post(
                "/api/integrations/mail/attachments",
                content=iter([payload[:64], payload[64:160], payload[160:]]),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 413, response.text)
            self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 0)
        finally:
            server_module.MAX_BODY = old_max

    def test_mailbox_must_match_an_active_client(self):
        self.h.create_mail_client(self.admin, mailbox="dcc@example.test")
        payload = self.content_payload(xlsx_bom("AUTO007", "V1.0", "W20", "深圳金茉"), mailbox="other@example.test")
        self.ingest(payload, status=403, token=self.h.mail_tokens["dcc@example.test"])
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 0)

    def test_invalid_base64_hash_size_path_and_time_do_not_write(self):
        self.h.create_mail_client(self.admin)
        good = xlsx_bom("AUTO008", "V1.0", "W20", "深圳金茉")
        invalid_cases = [
            self.content_payload(good, attachment={"content_base64": "not base64 !!!"}),
            self.content_payload(good, attachment={"sha256": "0" * 64}),
            self.content_payload(good, attachment={"size": len(good) + 1}),
            self.content_payload(good, filename="../escape.xlsx"),
            self.content_payload(good, received_at="2026-09-22T10:00:00"),
            self.content_payload(good, received_at="not-a-time"),
        ]
        for index, payload in enumerate(invalid_cases):
            with self.subTest(index=index):
                response = self.ingest(payload, status=422)
                self.assertNotIn("traceback", response.text.lower())
                self.assertNotIn(payload["attachment"]["content_base64"], response.text)
                self.assertTrue(all("input" not in error for error in response.json().get("errors", [])))
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 0)
        files_dir = self.h.data_dir / "files"
        self.assertFalse(files_dir.exists() and any(files_dir.iterdir()))

    def test_unparseable_attachment_creates_needs_review_receipt(self):
        self.h.create_mail_client(self.admin)
        payload = self.content_payload(
            b"not an xlsx archive",
            filename="ROLLBACK_V1.0-W20【深圳金茉】-部件.xlsx",
            attachment_id="rollback-att",
        )
        response = self.ingest(payload, status=201)
        body = response.json()
        self.assertEqual(body["status"], "needs_review")
        self.assertEqual(body["classification"], "unmatched")
        self.assertIn("解析失败", body["reason"])
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM mail_receipts").fetchone()["count"], 1)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 1)

    def test_concurrent_identical_requests_create_one_receipt_and_batch_item(self):
        self.h.create_mail_client(self.admin)
        payload = self.content_payload(
            xlsx_bom("AUTO012", "V1.0", "W20", "深圳金茉", "concurrent"),
            filename="AUTO012_V1.0-W20【深圳金茉】-部件.xlsx",
            attachment_id="concurrent-att",
        )
        results = []
        lock = threading.Lock()

        def submit():
            response = self.h.bearer_client().post("/api/integrations/mail/attachments", json=payload)
            with lock:
                results.append((response.status_code, response.json()))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(status for status, _body in results), [200, 201])
        self.assertEqual(len({body["receipt_id"] for _status, body in results}), 1)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM mail_receipts").fetchone()["count"], 1)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 1)

    def test_cross_client_receipts_are_isolated_by_mailbox_and_owner_scope(self):
        owner_a, _password_a = self.h.create_user(self.admin, "mail-owner-a", role="document_control", scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}])
        owner_b, password_b = self.h.create_user(self.admin, "mail-owner-b", role="document_control", scopes=[{"registrant": "广西由莱", "project_category": "医疗", "product": "UI06S PRU"}])
        self.h.create_mail_client(self.admin, name="Client A", owner_user_id=owner_a["id"], mailbox="dcc-a@example.test")
        self.h.create_mail_client(self.admin, name="Client B", owner_user_id=owner_b["id"], mailbox="dcc-b@example.test")

        receipt_a = self.ingest(
            self.content_payload(
                xlsx_bom("AUTO009", "V1.0", "W20", "深圳金茉"),
                filename="AUTO009_V1.0-W20【深圳金茉】-部件.xlsx",
                mailbox="dcc-a@example.test",
                attachment_id="att-a",
            )
        ).json()
        self.ingest(
            self.content_payload(
                xlsx_bom("1106S05901B", "V1.0", "UI06S PRU", "广西由莱"),
                filename="1106S05901B_V1.0-UI06S PRU【广西由莱】-部件.xlsx",
                mailbox="dcc-b@example.test",
                message_id="msg-b",
                attachment_id="att-b",
                biz_keyword="UI06S PRU",
            )
        )

        owner_b_client = self.h.login("mail-owner-b", password_b)
        batch_a = owner_b_client.get(f"/api/batches/{receipt_a['batch_id']}")
        self.assertIn(batch_a.status_code, (403, 404), batch_a.text)

        leaked = self.h.bearer_client(mailbox="dcc-b@example.test").get(f"/api/integrations/mail/receipts/{receipt_a['receipt_id']}")
        self.assertEqual(leaked.status_code, 404, leaked.text)

    def test_scoped_owner_denied_for_forbidden_business_and_inactive_owner(self):
        forbidden_owner, _password = self.h.create_user(
            self.admin,
            "mail-forbidden-owner",
            role="document_control",
            scopes=[{"registrant": "广西由莱", "project_category": "医疗", "product": "UI06S PRU"}],
        )
        self.h.create_mail_client(self.admin, owner_user_id=forbidden_owner["id"], mailbox="dcc@example.test")
        forbidden = self.content_payload(xlsx_bom("AUTO010", "V1.0", "W20", "深圳金茉"), filename="AUTO010_V1.0-W20【深圳金茉】-部件.xlsx")
        self.ingest(forbidden, status=403)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 0)
        files_dir = self.h.data_dir / "files"
        self.assertFalse(files_dir.exists() and any(files_dir.iterdir()))

        inactive_owner, _password = self.h.create_user(self.admin, "mail-inactive-owner", role="document_control", active=False)
        denied = self.admin.post(
            "/api/integrations/mail/clients",
            json={"name": "Inactive", "owner_user_id": inactive_owner["id"], "mailbox": "inactive@example.test", "expires_at": future_iso()},
        )
        self.assertEqual(denied.status_code, 400, denied.text)
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS count FROM batch_items").fetchone()["count"], 0)

    def test_real_read_only_bom_file_can_be_ingested_without_modifying_source(self):
        if not REAL_BOM.exists():
            self.skipTest(f"real BOM fixture missing: {REAL_BOM}")
        before_stat = REAL_BOM.stat()
        content = REAL_BOM.read_bytes()
        self.h.create_mail_client(self.admin, mailbox="real@example.test")
        payload = self.content_payload(
            content,
            filename=REAL_BOM.name,
            mailbox="real@example.test",
            message_id="real-msg",
            attachment_id="real-att",
            biz_keyword="UI06S PRU",
        )

        response = self.ingest(payload)
        batch = self.admin.get(f"/api/batches/{response.json()['batch_id']}")
        self.assertEqual(batch.status_code, 200, batch.text)
        self.assertEqual(batch.json()["items"][0]["fileName"], REAL_BOM.name)
        self.assertEqual(response.json()["status"], "awaiting_confirmation")
        self.assertEqual(response.json()["project_category"], "医疗")
        self.assertEqual(response.json()["version"], "20260324")
        self.assertEqual(batch.json()["items"][0]["sha256"], hashlib.sha256(content).hexdigest())
        after_stat = REAL_BOM.stat()
        self.assertEqual((after_stat.st_size, int(after_stat.st_mtime)), (before_stat.st_size, int(before_stat.st_mtime)))


if __name__ == "__main__":
    unittest.main()
