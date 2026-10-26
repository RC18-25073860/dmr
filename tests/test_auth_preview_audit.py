import json
import hashlib
import unittest
from unittest.mock import patch

from test_permissions import PermissionsHarness, ADMIN_PASSWORD


class AuthPreviewAuditTests(unittest.TestCase):
    def setUp(self):
        self.h = PermissionsHarness()
        self.admin = self.h.setup_admin()
        self.identity = self.h.app.state.identity

    def tearDown(self):
        self.h.close()

    def failures(self):
        return [dict(row) for row in self.h.store.execute("SELECT * FROM identity_audit WHERE action='login'").fetchall()
                if json.loads(row['detail']).get('result') == 'failure']

    def test_failed_login_reasons_never_impersonate_the_target(self):
        user, password = self.h.create_user(self.admin, 'audit-login')
        anonymous = self.h.client()
        for username, supplied in [('missing-user', 'secret-wrong'), ('audit-login', 'secret-wrong')]:
            self.assertEqual(anonymous.post('/api/auth/login', json={'username': username, 'password': supplied}).status_code, 401)
        with self.h.store.conn:
            self.h.store.execute("UPDATE identity_users SET locked_until='2999-01-01T00:00:00+00:00' WHERE id=?", (user['id'],))
        self.assertEqual(anonymous.post('/api/auth/login', json={'username': 'audit-login', 'password': password}).status_code, 401)
        with self.h.store.conn:
            self.h.store.execute('UPDATE identity_users SET active=0 WHERE id=?', (user['id'],))
        self.assertEqual(anonymous.post('/api/auth/login', json={'username': 'audit-login', 'password': password}).status_code, 401)
        rows = self.failures()
        self.assertEqual({json.loads(row['detail'])['reason'] for row in rows}, {'unknown_user', 'invalid_credentials', 'locked', 'inactive'})
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIsNone(row['actor_user_id'])
            self.assertEqual(row['actor_display_name'], '未认证访问者')
        serialized = json.dumps(rows)
        self.assertNotIn(password, serialized)
        self.assertNotIn('secret-wrong', serialized)
        self.assertTrue(all(row['target_user_id'] == user['id'] for row in rows
                            if json.loads(row['detail'])['attempted_username'] == 'audit-login'))

    def test_logout_is_attributed_revoked_and_token_free(self):
        token, user = self.identity.login('jiajia', ADMIN_PASSWORD)
        self.identity.logout(token)
        self.identity.logout(token)
        rows = self.h.store.execute("SELECT * FROM identity_audit WHERE action='logout'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['actor_user_id'], user['id'])
        self.assertNotIn(token, json.dumps(dict(rows[0])))
        self.assertIsNotNone(self.h.store.execute('SELECT revoked_at FROM identity_sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()['revoked_at'])

    def test_identity_audit_failure_rolls_back_login_counter_and_logout(self):
        token, _ = self.identity.login('jiajia', ADMIN_PASSWORD)
        before = self.h.store.execute('SELECT COUNT(*) AS n FROM identity_sessions WHERE revoked_at IS NULL').fetchone()['n']
        with patch.object(self.identity, '_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.identity.login('jiajia', 'incorrect-password')
            with self.assertRaises(RuntimeError):
                self.identity.login('jiajia', ADMIN_PASSWORD)
            with self.assertRaises(RuntimeError):
                self.identity.logout(token)
        self.assertEqual(self.h.store.execute("SELECT failed_login_count FROM identity_users WHERE username_normalized='jiajia'").fetchone()['failed_login_count'], 0)
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM identity_sessions WHERE revoked_at IS NULL').fetchone()['n'], before)

    def test_preview_is_audited_only_after_authorization_and_integrity_checks(self):
        _, file = self.h.seed_bom(self.admin, 'BOM-AUDIT', 'V1.0', 'W20', '深圳金茉', salt='private-evidence')
        path = '/api/preview/' + file['id']
        self.assertEqual(self.admin.get(path).status_code, 200)
        rows = self.h.store.execute("SELECT * FROM logs WHERE action='预览文件'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn('佳佳 [', rows[0]['user'])
        self.assertEqual(rows[0]['object'], file['name'])
        _, password = self.h.create_user(self.admin, 'audit-foreign', scopes=[{'registrant': '广西由莱', 'project_category': '*', 'product': '*'}])
        foreign = self.h.login('audit-foreign', password)
        with patch.object(self.h.store, 'read_blob', side_effect=AssertionError('must authorize before reading')):
            denied = foreign.get(path)
        self.assertEqual(denied.status_code, 404)
        self.assertNotIn(file['name'], denied.text)
        self.assertNotIn('private-evidence', denied.text)
        with patch.object(self.h.store, 'read_blob', return_value=b'tampered'):
            self.assertEqual(self.admin.get(path).status_code, 409)
        self.assertEqual(len(self.h.store.execute("SELECT * FROM logs WHERE action='预览文件'").fetchall()), 1)
        with patch.object(self.h.store, 'add_log', side_effect=RuntimeError('audit unavailable')):
            self.assertEqual(self.admin.get(path).status_code, 500)


if __name__ == '__main__':
    unittest.main()
