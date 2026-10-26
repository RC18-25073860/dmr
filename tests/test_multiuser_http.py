import sys
import unittest
from dataclasses import replace
from pathlib import Path
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from test_permissions import PermissionsHarness
from server import create_app


class MultiuserHttpTests(unittest.TestCase):
    def setUp(self):
        self.h=PermissionsHarness();self.admin=self.h.setup_admin()
    def tearDown(self):self.h.close()

    def new_user(self):
        r=self.admin.post('/api/users',json={'username':'new-person','display_name':'新人员','password':'InitialPassword2026','role':'reader','active':True,'scopes':[{'registrant':'*','project_category':'*','product':'*'}],'can_download':True,'can_history':True})
        self.assertEqual(r.status_code,200,r.text);return r.json()['user']

    def test_first_login_is_gated_by_backend_until_changed_and_reauthenticated(self):
        self.new_user();c=self.h.client()
        r=c.post('/api/auth/login',json={'username':'new-person','password':'InitialPassword2026'})
        self.assertTrue(r.json()['user']['must_change_password'])
        self.assertEqual(c.get('/api/auth/me').status_code,200)
        for path in ('/api/state','/api/download/missing','/api/preview/missing'):
            r=c.get(path);self.assertEqual(r.status_code,403,r.text);self.assertEqual(r.json()['code'],'PASSWORD_CHANGE_REQUIRED')
        r=c.post('/api/auth/change-password',json={'current_password':'InitialPassword2026','new_password':'ChangedPassword2026'})
        self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['must_login'])
        self.assertIn('Max-Age=0',r.headers['set-cookie'])
        self.assertEqual(c.get('/api/state').status_code,401)
        self.assertEqual(c.post('/api/auth/login',json={'username':'new-person','password':'ChangedPassword2026'}).status_code,200)
        self.assertEqual(c.get('/api/state').status_code,200)

    def test_reset_revokes_existing_browser_and_reapplies_gate(self):
        self.new_user();c=self.h.client()
        c.post('/api/auth/login',json={'username':'new-person','password':'InitialPassword2026'})
        c.post('/api/auth/change-password',json={'current_password':'InitialPassword2026','new_password':'ChangedPassword2026'})
        c.post('/api/auth/login',json={'username':'new-person','password':'ChangedPassword2026'})
        u=next(u for u in self.admin.get('/api/users').json()['users'] if u['username']=='new-person')
        r=self.admin.post('/api/users/'+u['id'],json={'password':'ResetPassword2026','expected_edit_token':u['edit_token']})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(c.get('/api/state').status_code,401)
        c.post('/api/auth/login',json={'username':'new-person','password':'ResetPassword2026'})
        self.assertEqual(c.get('/api/state').status_code,403)

    def test_wrong_or_same_password_does_not_revoke_valid_session(self):
        r=self.admin.post('/api/auth/change-password',json={'current_password':'WrongPassword2026','new_password':'SomeNewPassword2026'})
        self.assertEqual(r.status_code,403,r.text)
        r=self.admin.post('/api/auth/change-password',json={'current_password':'AdminPass123','new_password':'AdminPass123'})
        self.assertEqual(r.status_code,400,r.text)
        self.assertEqual(self.admin.get('/api/state').status_code,200)

    def production(self):
        cfg=replace(self.h.config,environment='production',allowed_hosts=('dmr.example.com',),trusted_origins=('https://dmr.example.com',),public_origin='https://dmr.example.com',cookie_secure=True,setup_allowed=False)
        app=create_app(self.h.store,cfg)
        c=TestClient(app,base_url='https://dmr.example.com',raise_server_exceptions=False);self.h.clients.append(c)
        return c

    def test_production_cookie_and_origin_protection_integrated(self):
        c=self.production();credentials={'username':'jiajia','password':'AdminPass123'}
        self.assertEqual(c.post('/api/auth/login',json=credentials).status_code,403)
        self.assertEqual(c.post('/api/auth/login',json=credentials,headers={'Origin':'http://dmr.example.com'}).status_code,403)
        r=c.post('/api/auth/login',json=credentials,headers={'Origin':'https://dmr.example.com'})
        self.assertEqual(r.status_code,200,r.text)
        for flag in ('Secure','HttpOnly','SameSite=strict'):self.assertIn(flag,r.headers['set-cookie'])
        self.assertEqual(c.get('/api/state').status_code,200)
        self.assertEqual(c.get('/api/state',headers={'Host':'evil.invalid','X-Forwarded-Host':'dmr.example.com'}).status_code,403)
        self.assertEqual(c.post('/api/auth/setup',json={},headers={'Origin':'https://dmr.example.com'}).status_code,403)
        r=c.post('/api/auth/logout',json={},headers={'Origin':'https://dmr.example.com'})
        self.assertEqual(r.status_code,200);self.assertIn('Secure',r.headers['set-cookie'])
        self.assertEqual(c.get('/api/state').status_code,401)

    def test_production_cannot_start_with_empty_identity_store(self):
        empty=PermissionsHarness()
        try:
            cfg=replace(empty.config,environment='production',allowed_hosts=('dmr.example.com',),trusted_origins=('https://dmr.example.com',),public_origin='https://dmr.example.com',cookie_secure=True,setup_allowed=False)
            with self.assertRaises(RuntimeError):create_app(empty.store,cfg)
        finally:empty.close()


if __name__=='__main__':unittest.main()
