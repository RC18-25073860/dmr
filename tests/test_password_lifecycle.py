import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = ROOT / "tests" / ".runtime"
sys.path.insert(0, str(ROOT))

from backend.identity import IdentityService
from backend.service import DMRStore


ADMIN_PASSWORD = "AdminPass123"


class IdentityHarness:
    def __init__(self):
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="identity-lifecycle-", dir=TEST_RUNTIME)
        self.data_dir = Path(self.tmp.name)
        self.store = DMRStore(self.data_dir, None)
        self.identity = IdentityService(self.store)
        _token, self.admin = self.identity.setup("佳佳", "jiajia", ADMIN_PASSWORD)

    def close(self):
        self.store.close()
        self.tmp.cleanup()

    def create_reader(self, username="reader-one", password="InitialReader123"):
        return self.identity.create_user(
            {
                "username": username,
                "display_name": username,
                "password": password,
                "role": "reader",
                "active": True,
                "scopes": [{"registrant": "*", "project_category": "*", "product": "*"}],
                "can_download": True,
                "can_history": True,
            },
            self.admin,
        )


class PasswordLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h = IdentityHarness()

    def tearDown(self):
        self.h.close()

    def assert_http(self, status_code, func, *args, **kwargs):
        with self.assertRaises(HTTPException) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.status_code, status_code, str(ctx.exception.detail))

    def user_by_id(self, identity, user_id):
        return next(row for row in identity.list_users() if row["id"] == user_id)

    def test_admin_created_user_must_change_password_and_self_change_revokes_all_sessions(self):
        user = self.h.create_reader()
        self.assertTrue(user["must_change_password"])
        first_token, first_user = self.h.identity.login("reader-one", "InitialReader123")
        second_token, _second_user = self.h.identity.login("reader-one", "InitialReader123")
        self.assertTrue(first_user["must_change_password"])
        self.assertTrue(self.h.identity.current(first_token)["must_change_password"])

        self.assert_http(400, self.h.identity.change_password, first_token, "InitialReader123", "InitialReader123")
        self.assert_http(403, self.h.identity.change_password, first_token, "WrongReader123", "ReaderReady123")

        changed = self.h.identity.change_password(first_token, "InitialReader123", "ReaderReady123")
        self.assertFalse(changed["must_change_password"])
        self.assert_http(401, self.h.identity.current, first_token)
        self.assert_http(401, self.h.identity.current, second_token)
        self.assert_http(401, self.h.identity.login, "reader-one", "InitialReader123")
        new_token, logged_in = self.h.identity.login("reader-one", "ReaderReady123")
        self.assertFalse(logged_in["must_change_password"])
        self.assertEqual(self.h.identity.current(new_token)["id"], user["id"])

    def test_admin_password_reset_requires_next_login_change_and_revokes_sessions(self):
        user = self.h.create_reader()
        token, _initial_user = self.h.identity.login("reader-one", "InitialReader123")
        self.h.identity.change_password(token, "InitialReader123", "ReaderReady123")
        active_token, _active_user = self.h.identity.login("reader-one", "ReaderReady123")

        current_user = self.user_by_id(self.h.identity, user["id"])
        reset = self.h.identity.update_user(
            user["id"],
            {"password": "ResetReader123", "expected_edit_token": current_user["edit_token"]},
            self.h.admin,
        )
        self.assertTrue(reset["must_change_password"])
        self.assert_http(401, self.h.identity.current, active_token)
        self.assert_http(401, self.h.identity.login, "reader-one", "ReaderReady123")
        reset_token, reset_user = self.h.identity.login("reader-one", "ResetReader123")
        self.assertTrue(reset_user["must_change_password"])
        self.assertTrue(self.h.identity.current(reset_token)["must_change_password"])

    def test_user_edit_token_blocks_stale_independent_connection_updates(self):
        user = self.h.create_reader()
        second_store = DMRStore(self.h.data_dir, None)
        try:
            second_identity = IdentityService(second_store)
            first_snapshot = self.user_by_id(self.h.identity, user["id"])
            second_snapshot = self.user_by_id(second_identity, user["id"])
            self.assertEqual(first_snapshot["edit_token"], second_snapshot["edit_token"])
            self.h.identity.login("reader-one", "InitialReader123")

            updated = self.h.identity.update_user(
                user["id"],
                {"display_name": "reader renamed", "expected_edit_token": first_snapshot["edit_token"]},
                self.h.admin,
            )
            self.assertEqual(updated["display_name"], "reader renamed")
            self.assertNotEqual(updated["edit_token"], first_snapshot["edit_token"])

            self.assert_http(
                409,
                second_identity.update_user,
                user["id"],
                {"role": "document_control", "expected_edit_token": second_snapshot["edit_token"]},
                self.h.admin,
            )
            self.assert_http(428, self.h.identity.update_user, user["id"], {"role": "document_control"}, self.h.admin)
            self.assert_http(
                428,
                self.h.identity.update_user,
                user["id"],
                {"role": "document_control", "expected_edit_token": "这不是合法token"},
                self.h.admin,
            )
            final_user = self.user_by_id(self.h.identity, user["id"])
            self.assertEqual(final_user["display_name"], "reader renamed")
            self.assertEqual(final_user["role"], "reader")
        finally:
            second_store.close()


if __name__ == "__main__":
    unittest.main()
