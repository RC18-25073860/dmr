import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = ROOT / "tests" / ".runtime"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from backend.config import AppConfig
from backend.service import DMRStore
from server import create_app
from test_backend import b64, xlsx_bom


ADMIN_PASSWORD = "AdminPass123"


class PermissionsHarness:
    def __init__(self):
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="permissions-", dir=TEST_RUNTIME)
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

    def setup_admin(self):
        client = self.client()
        response = client.post(
            "/api/auth/setup",
            json={"display_name": "佳佳", "username": "jiajia", "password": ADMIN_PASSWORD},
        )
        self.assert_ok(response)
        return client

    def login(self, username, password):
        client = self.client()
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assert_ok(response)
        return client

    def create_user(
        self,
        admin,
        username,
        role="reader",
        scopes=None,
        *,
        active=True,
        can_download=True,
        can_history=True,
        password=None,
    ):
        payload = {
            "username": username,
            "display_name": username,
            "password": password or f"{username.title()}Pass123",
            "role": role,
            "active": active,
            "scopes": scopes if scopes is not None else [{"registrant": "*", "project_category": "*", "product": "*"}],
            "can_download": can_download,
            "can_history": can_history,
        }
        response = admin.post("/api/users", json=payload)
        self.assert_ok(response)
        initial_password = payload["password"]
        final_password = f"{username.title()}Ready123"
        first_login = self.login(username, initial_password)
        change = first_login.post(
            "/api/auth/change-password",
            json={"current_password": initial_password, "new_password": final_password},
        )
        self.assert_ok(change)
        return response.json()["user"], final_password

    def user(self, admin, user_id):
        response = admin.get("/api/users")
        self.assert_ok(response)
        return next(row for row in response.json()["users"] if row["id"] == user_id)

    def update_user(self, admin, user_id, payload):
        current = self.user(admin, user_id)
        response = admin.post(f"/api/users/{user_id}", json=payload | {"expected_edit_token": current["edit_token"]})
        return response

    def seed_bom(self, client, code, version, product, registrant, project_category="家电", salt=""):
        upload = client.post(
            "/api/analyze",
            json={
                "files": [
                    {
                        "name": f"{code}_{version}-{product}【{registrant}】-部件.xlsx",
                        "data": b64(xlsx_bom(code, version, product, registrant, salt)),
                    }
                ]
            },
        )
        self.assert_ok(upload)
        item = upload.json()["items"][0]
        if item.get("project_category") != project_category:
            review = client.post(
                "/api/review",
                json={"batch_id": upload.json()["batch_id"], "item_id": item["id"], "project_category": project_category, "expected_edit_token": item["edit_token"]},
            )
            self.assert_ok(review)
        commit = client.post("/api/commit", json={"batch_id": upload.json()["batch_id"]})
        self.assert_ok(commit)
        if commit.json().get("committed") != 1:
            raise AssertionError(commit.text)
        state = client.get("/api/state").json()
        record = next(row for row in state["records"] if row["no"] == code and row["version"] == version)
        file_row = next(row for row in state["libraryFiles"] if row["recordId"] == record["id"] and row["version"] == version)
        return record, file_row

    def assert_ok(self, response):
        if response.status_code >= 400:
            raise AssertionError((response.status_code, response.text[:500]))


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.h = PermissionsHarness()

    def tearDown(self):
        self.h.close()

    def test_auth_status_is_public_before_setup(self):
        response = self.h.client().get("/api/auth/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["setup_required"], True)
        self.assertEqual(response.json()["mode"], "local")

    def test_setup_sets_httponly_samesite_session_cookie(self):
        response = self.h.client().post(
            "/api/auth/setup",
            json={"display_name": "佳佳", "username": "jiajia", "password": ADMIN_PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite", cookie)

    def test_setup_is_one_time_only(self):
        self.h.setup_admin()
        response = self.h.client().post(
            "/api/auth/setup",
            json={"display_name": "另一个管理员", "username": "second-admin", "password": "SecondAdmin123"},
        )
        self.assertIn(response.status_code, (400, 403, 409))

    def test_business_endpoints_require_authenticated_user(self):
        self.h.setup_admin()
        anonymous = self.h.client()
        protected = [
            ("GET", "/api/health", None),
            ("GET", "/api/state", None),
            ("POST", "/api/analyze", {"files": []}),
            ("GET", "/api/download/missing-file", None),
        ]
        for method, path, body in protected:
            response = anonymous.request(method, path, json=body)
            self.assertEqual(response.status_code, 401, (method, path, response.status_code, response.text[:200]))

    def test_admin_can_create_users_and_audit_the_grant(self):
        admin = self.h.setup_admin()
        user, _password = self.h.create_user(admin, "reader-w20", scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}])
        self.assertEqual(user["username"], "reader-w20")
        users = admin.get("/api/users")
        self.assertEqual(users.status_code, 200, users.text)
        self.assertIn("reader-w20", {row["username"] for row in users.json()["users"]})
        audit = admin.get("/api/users/audit")
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertTrue(
            any(
                entry["actor"]["id"] == admin.get("/api/auth/me").json()["user"]["id"]
                and entry["actor"]["display_name"] == "佳佳"
                and entry["target"]["id"] == user["id"]
                and entry["target"]["display_name"] == "reader-w20"
                for entry in audit.json()["entries"]
            )
        )

    def test_reader_cannot_write_business_data(self):
        admin = self.h.setup_admin()
        _user, password = self.h.create_user(admin, "reader-only", role="reader")
        reader = self.h.login("reader-only", password)
        response = reader.post("/api/analyze", json={"files": []})
        self.assertEqual(response.status_code, 403, response.text)

    def test_non_admin_cannot_escalate_user_permissions(self):
        admin = self.h.setup_admin()
        user, password = self.h.create_user(admin, "control-one", role="document_control")
        controller = self.h.login("control-one", password)
        response = controller.post(f"/api/users/{user['id']}", json={"role": "admin"})
        self.assertIn(response.status_code, (401, 403))

    def test_scope_filters_state_counts_logs_notifications_and_source_health(self):
        admin = self.h.setup_admin()
        self.h.seed_bom(admin, "BOM-W20", "V1.0", "W20", "深圳金茉", "家电", "allowed")
        self.h.seed_bom(admin, "BOM-UI20S", "V1.0", "UI20S", "广西由莱", "医疗", "blocked")
        _user, password = self.h.create_user(admin, "reader-scoped", scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}])
        scoped = self.h.login("reader-scoped", password)
        state = scoped.get("/api/state").json()
        payload = json.dumps(state, ensure_ascii=False)
        self.assertIn("BOM-W20", payload)
        self.assertNotIn("BOM-UI20S", payload)
        self.assertNotIn("UI20S", payload)
        self.assertEqual(state["sourceHealth"][0]["detail"], "1 条清单 · 1 份附件")

    def test_unauthorized_file_preview_and_download_are_blocked(self):
        admin = self.h.setup_admin()
        _allowed_record, _allowed_file = self.h.seed_bom(admin, "BOM-W20", "V1.0", "W20", "深圳金茉", "家电", "allowed")
        _foreign_record, foreign_file = self.h.seed_bom(admin, "BOM-UI20S", "V1.0", "UI20S", "广西由莱", "医疗", "blocked")
        _user, password = self.h.create_user(admin, "reader-w20-file", scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}])
        reader = self.h.login("reader-w20-file", password)
        for path in (f"/api/preview/{foreign_file['id']}", f"/api/download/{foreign_file['id']}"):
            self.assertIn(reader.get(path).status_code, (403, 404), path)

    def test_download_permission_flag_blocks_file_download(self):
        admin = self.h.setup_admin()
        _record, file_row = self.h.seed_bom(admin, "BOM-NO-DL", "V1.0", "W20", "深圳金茉", "家电")
        _user, password = self.h.create_user(
            admin,
            "reader-no-download",
            scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}],
            can_download=False,
        )
        reader = self.h.login("reader-no-download", password)
        self.assertEqual(reader.get(f"/api/download/{file_row['id']}").status_code, 403)

    def test_history_permission_off_hides_old_versions(self):
        admin = self.h.setup_admin()
        _record_v1, old_file = self.h.seed_bom(admin, "BOM-HISTORY", "V1.0", "W20", "深圳金茉", "家电", "old")
        self.h.seed_bom(admin, "BOM-HISTORY", "V1.1", "W20", "深圳金茉", "家电", "new")
        _user, password = self.h.create_user(
            admin,
            "reader-no-history",
            scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}],
            can_history=False,
        )
        reader = self.h.login("reader-no-history", password)
        state = reader.get("/api/state").json()
        payload = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("V1.0", payload)
        self.assertNotIn("历史版本", payload)
        self.assertIn(reader.get(f"/api/download/{old_file['id']}").status_code, (403, 404))

    def test_disabled_user_loses_access_immediately(self):
        admin = self.h.setup_admin()
        user, password = self.h.create_user(admin, "reader-disable", role="reader")
        reader = self.h.login("reader-disable", password)
        self.assertEqual(reader.get("/api/auth/me").status_code, 200)
        response = self.h.update_user(admin, user["id"], {"active": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(reader.get("/api/auth/me").status_code, 401)

    def test_logout_revokes_session(self):
        admin = self.h.setup_admin()
        _user, password = self.h.create_user(admin, "reader-logout", role="reader")
        reader = self.h.login("reader-logout", password)
        self.assertEqual(reader.post("/api/auth/logout", json={}).status_code, 200)
        self.assertEqual(reader.get("/api/auth/me").status_code, 401)

    def test_password_reset_revokes_existing_session(self):
        admin = self.h.setup_admin()
        user, password = self.h.create_user(admin, "reader-reset", role="reader")
        reader = self.h.login("reader-reset", password)
        response = self.h.update_user(admin, user["id"], {"password": "ReaderReset123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(reader.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.h.client().post("/api/auth/login", json={"username": "reader-reset", "password": password}).status_code, 401)
        self.assertEqual(self.h.login("reader-reset", "ReaderReset123").get("/api/auth/me").status_code, 200)

    def test_five_wrong_logins_lock_user_even_when_password_is_later_correct(self):
        admin = self.h.setup_admin()
        _user, password = self.h.create_user(admin, "reader-lockout", role="reader")
        for _ in range(5):
            response = self.h.client().post("/api/auth/login", json={"username": "reader-lockout", "password": "WrongPass123"})
            self.assertEqual(response.status_code, 401)
        locked = self.h.client().post("/api/auth/login", json={"username": "reader-lockout", "password": password})
        self.assertEqual(locked.status_code, 401)

    def test_scope_change_takes_effect_on_existing_cookie(self):
        admin = self.h.setup_admin()
        self.h.seed_bom(admin, "BOM-W20-OLD", "V1.0", "W20", "深圳金茉", "家电", "old-scope")
        self.h.seed_bom(admin, "BOM-UI20S-NEW", "V1.0", "UI20S", "广西由莱", "医疗", "new-scope")
        user, password = self.h.create_user(admin, "reader-rescope", scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}])
        reader = self.h.login("reader-rescope", password)
        before = json.dumps(reader.get("/api/state").json(), ensure_ascii=False)
        self.assertIn("BOM-W20-OLD", before)
        self.assertNotIn("BOM-UI20S-NEW", before)
        response = self.h.update_user(admin, user["id"], {"scopes": [{"registrant": "广西由莱", "project_category": "医疗", "product": "UI20S"}]})
        self.assertEqual(response.status_code, 200, response.text)
        after = json.dumps(reader.get("/api/state").json(), ensure_ascii=False)
        self.assertNotIn("BOM-W20-OLD", after)
        self.assertIn("BOM-UI20S-NEW", after)

    def test_foreign_manual_review_failure_rolls_back_item_changes(self):
        admin = self.h.setup_admin()
        self.h.seed_bom(admin, "BOM-W20-ROLLBACK", "V1.0", "W20", "深圳金茉", "家电")
        upload = admin.post(
            "/api/analyze",
            json={"files": [{"name": "BOM-W20-ROLLBACK_V1.1-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-W20-ROLLBACK", "V1.1", "W20", "深圳金茉", "rollback"))}]},
        ).json()
        _user, password = self.h.create_user(
            admin,
            "controller-rollback",
            role="document_control",
            scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}],
        )
        controller = self.h.login("controller-rollback", password)
        failed = controller.post(
            "/api/review",
            json={
                "batch_id": upload["batch_id"],
                "item_id": upload["items"][0]["id"],
                "registrant": "广西由莱",
                "project_category": "医疗",
                "product": "UI20S",
                "expected_edit_token": upload["items"][0]["edit_token"],
            },
        )
        self.assertIn(failed.status_code, (403, 404))
        item = admin.get(f"/api/batches/{upload['batch_id']}").json()["items"][0]
        self.assertEqual(item["registrant"], "深圳金茉")
        self.assertEqual(item["product"], "W20")
        self.assertEqual(item["project_category"], "家电")

    def test_scoped_controller_can_complete_own_unknown_pending_batch(self):
        admin = self.h.setup_admin()
        _user, password = self.h.create_user(
            admin,
            "controller-complete",
            role="document_control",
            scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}],
        )
        controller = self.h.login("controller-complete", password)
        upload = controller.post(
            "/api/analyze",
            json={"files": [{"name": "待确认资料.xlsx", "data": b64(xlsx_bom("BOM-PENDING", "V1.0", "待确认", "待确认", "pending"))}]},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        item = upload.json()["items"][0]
        review = controller.post(
            "/api/review",
            json={
                "batch_id": upload.json()["batch_id"],
                "item_id": item["id"],
                "code": "BOM-PENDING",
                "registrant": "深圳金茉",
                "project_category": "家电",
                "product": "W20",
                "module": "BOM",
                "type": "成品",
                "version": "V1.0",
                "expected_edit_token": item["edit_token"],
            },
        )
        self.assertEqual(review.status_code, 200, review.text)
        commit = controller.post("/api/commit", json={"batch_id": upload.json()["batch_id"]})
        self.assertEqual(commit.status_code, 200, commit.text)
        self.assertEqual(commit.json()["committed"], 1)

    def test_invalid_grants_and_bool_fields_are_rejected(self):
        admin = self.h.setup_admin()
        bad_payloads = [
            {"username": "bad-role", "display_name": "Bad Role", "password": "BadRole123", "role": "superuser", "active": True, "scopes": [], "can_download": True, "can_history": True},
            {"username": "bad-bool", "display_name": "Bad Bool", "password": "BadBool123", "role": "reader", "active": "yes", "scopes": [], "can_download": True, "can_history": True},
            {"username": "bad-scope", "display_name": "Bad Scope", "password": "BadScope123", "role": "reader", "active": True, "scopes": [{"registrant": "深圳金茉"}], "can_download": True, "can_history": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload["username"]):
                response = admin.post("/api/users", json=payload)
                self.assertIn(response.status_code, (400, 422), response.text)

    def test_notification_reads_are_independent_per_authorized_user(self):
        admin = self.h.setup_admin()
        self.h.seed_bom(admin, "BOM-NOTIFY", "V1.0", "W20", "深圳金茉", "家电", "notice")
        state_before = admin.get("/api/state").json()
        self.assertEqual(len(state_before["notifications"]), 1)
        notification = state_before["notifications"][0]
        _global_doc, password = self.h.create_user(
            admin,
            "global-doc-control",
            role="document_control",
            scopes=[{"registrant": "*", "project_category": "*", "product": "*"}],
            can_download=True,
            can_history=True,
        )
        controller = self.h.login("global-doc-control", password)
        controller_before = controller.get("/api/state").json()
        self.assertEqual(controller_before["notifications"][0]["id"], notification["id"])
        self.assertFalse(controller_before["notifications"][0]["read"])

        response = admin.post("/api/notifications/read", json={})
        self.assertEqual(response.status_code, 200, response.text)
        admin_after = admin.get("/api/state").json()
        controller_after = controller.get("/api/state").json()

        self.assertTrue(next(item for item in admin_after["notifications"] if item["id"] == notification["id"])["read"])
        controller_notification = next(item for item in controller_after["notifications"] if item["id"] == notification["id"])
        self.assertFalse(controller_notification["read"])
        self.assertEqual(controller_notification["title"], notification["title"])
        self.assertEqual([row["no"] for row in admin_after["records"]], [row["no"] for row in controller_after["records"]])

    def test_last_admin_cannot_be_disabled_or_demoted(self):
        admin = self.h.setup_admin()
        me = admin.get("/api/auth/me").json()["user"]
        for payload in ({"active": False}, {"role": "reader"}):
            response = self.h.update_user(admin, me["id"], payload)
            self.assertIn(response.status_code, (400, 403, 409), response.text)

    def test_scope_rows_do_not_cross_match_product_category_or_registrant(self):
        admin = self.h.setup_admin()
        self.h.seed_bom(admin, "BOM-ALLOWED-A", "V1.0", "W20", "深圳金茉", "家电", "allowed-a")
        self.h.seed_bom(admin, "BOM-ALLOWED-B", "V1.0", "UI20S", "广西由莱", "医疗", "allowed-b")
        self.h.seed_bom(admin, "BOM-CROSS", "V1.0", "UI20S", "深圳金茉", "医疗", "cross")
        scopes = [
            {"registrant": "深圳金茉", "project_category": "家电", "product": "W20"},
            {"registrant": "广西由莱", "project_category": "医疗", "product": "UI20S"},
        ]
        _user, password = self.h.create_user(admin, "reader-two-rows", scopes=scopes)
        state = self.h.login("reader-two-rows", password).get("/api/state").json()
        payload = json.dumps(state, ensure_ascii=False)
        self.assertIn("BOM-ALLOWED-A", payload)
        self.assertIn("BOM-ALLOWED-B", payload)
        self.assertNotIn("BOM-CROSS", payload)

    def test_document_controller_cannot_operate_on_foreign_scope_records(self):
        admin = self.h.setup_admin()
        self.h.seed_bom(admin, "BOM-W20-SCOPE", "V1.0", "W20", "深圳金茉", "家电")
        foreign_record, _foreign_file = self.h.seed_bom(admin, "BOM-FOREIGN", "V1.0", "UI20S", "广西由莱", "医疗")
        allowed_batch = admin.post(
            "/api/analyze",
            json={"files": [{"name": "BOM-W20-SCOPE_V1.1-W20【深圳金茉】-部件.xlsx", "data": b64(xlsx_bom("BOM-W20-SCOPE", "V1.1", "W20", "深圳金茉", "next"))}]},
        ).json()
        foreign_batch = admin.post(
            "/api/analyze",
            json={"files": [{"name": "BOM-FOREIGN_V1.1-UI20S【广西由莱】-部件.xlsx", "data": b64(xlsx_bom("BOM-FOREIGN", "V1.1", "UI20S", "广西由莱", "foreign-next"))}]},
        ).json()
        _user, password = self.h.create_user(
            admin,
            "controller-w20",
            role="document_control",
            scopes=[{"registrant": "深圳金茉", "project_category": "家电", "product": "W20"}],
        )
        controller = self.h.login("controller-w20", password)
        reassign = controller.post(
            "/api/review",
            json={
                "batch_id": allowed_batch["batch_id"],
                "item_id": allowed_batch["items"][0]["id"],
                "record_id": foreign_record["id"],
                "expected_edit_token": allowed_batch["items"][0]["edit_token"],
            },
        )
        classify = controller.post(f"/api/records/{foreign_record['id']}/classification", json={"project_category": "家电", "expected_edit_token": foreign_record["edit_token"]})
        commit = controller.post("/api/commit", json={"batch_id": foreign_batch["batch_id"]})
        self.assertIn(reassign.status_code, (403, 404))
        self.assertIn(classify.status_code, (403, 404))
        self.assertIn(commit.status_code, (403, 404))
        refreshed = admin.get("/api/state").json()
        self.assertEqual(next(row for row in refreshed["records"] if row["id"] == foreign_record["id"])["version"], "V1.0")

    def test_empty_scope_user_sees_no_business_data(self):
        admin = self.h.setup_admin()
        self.h.seed_bom(admin, "BOM-HIDDEN", "V1.0", "W20", "深圳金茉", "家电")
        _user, password = self.h.create_user(admin, "reader-empty", scopes=[])
        state = self.h.login("reader-empty", password).get("/api/state").json()
        self.assertEqual(state["records"], [])
        self.assertEqual(state["libraryFiles"], [])
        self.assertEqual(state["exceptions"], [])
        self.assertEqual(state["notifications"], [])


if __name__ == "__main__":
    unittest.main()
