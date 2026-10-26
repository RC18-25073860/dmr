"""Integration of the NAS extension with the real DMR app, not a stub router."""
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_permissions import PermissionsHarness
from test_backend import b64, xlsx_bom
from server import create_app

TOKEN = 'local-test-worker-token-that-is-not-a-real-secret-2026'

class NasAppTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'DMR_NAS_SYNC_TOKEN':TOKEN, 'DMR_NAS_READ_BASE_URL':'https://reader.example.test', 'DMR_NAS_CREDENTIALS_KEY':Fernet.generate_key().decode()})
        self.env.start()
        self.h=PermissionsHarness(); self.admin=self.h.setup_admin()
    def tearDown(self):
        self.h.close(); self.env.stop()
    def test_real_commit_keeps_original_uploader_and_enqueues_once(self):
        user,password=self.h.create_user(self.admin,'nas-doc',role='document_control')
        doc=self.h.login('nas-doc',password)
        r=self.admin.put(f"/api/users/{user['id']}/nas-account",json={'account':'test-doc-nas','password':'test-only-NAS-password'})
        self.assertEqual(r.status_code,200,r.text)
        body={'files':[{'name':'1122334455_V1.0-W20【测试主体】-部件.xlsx','data':b64(xlsx_bom('1122334455','V1.0','W20','测试主体','nas'))}]}
        upload=doc.post('/api/analyze',json=body);self.assertEqual(upload.status_code,200,upload.text)
        result=self.admin.post('/api/commit',json={'batch_id':upload.json()['batch_id']})
        self.assertEqual(result.status_code,200,result.text); self.assertEqual(result.json()['committed'],1,result.text)
        job=result.json()['nas_jobs'][0]; self.assertEqual(job['owner_user_id'],user['id']);self.assertEqual(job['status'],'blocked')
        file_id=job['file_id']
        again=self.admin.post('/api/commit',json={'batch_id':upload.json()['batch_id']});self.assertEqual(again.status_code,200,again.text)
        self.assertEqual(self.h.store.execute('SELECT count(*) n FROM nas_sync_jobs WHERE file_id=?',(file_id,)).fetchone()['n'],1)
        state=self.admin.get('/api/state').json();saved=next(f for f in state['libraryFiles'] if f['id']==file_id)
        self.assertEqual(saved['sync_status'],'blocked');self.assertEqual(self.admin.get('/api/download/'+file_id).status_code,200)
        mapping=self.admin.post('/api/nas/directory-mappings',json={'record_id':job['record_id'],'nas_path':'/DMR-TEST/medical/W20/BOM'})
        self.assertEqual(mapping.status_code,200,mapping.text)
        retry=self.admin.post('/api/admin/storage-sync/files/'+file_id+'/retry',json={});self.assertEqual(retry.status_code,200,retry.text)
        claimed=self.h.client().post('/api/storage-sync/claim',headers={'Authorization':'Bearer '+TOKEN},json={})
        self.assertEqual(claimed.status_code,200,claimed.text);self.assertEqual(claimed.json()[0]['file_id'],file_id)
        # Emulate the authenticated worker result; no real NAS is contacted.
        worker=self.h.client(); headers={'Authorization':'Bearer '+TOKEN}; task=claimed.json()[0]
        credentials=worker.post('/api/storage-sync/files/'+file_id+'/credentials',headers=headers,json={'lease_token':task['lease_token']})
        self.assertEqual(credentials.status_code,200,credentials.text)
        self.assertEqual(credentials.json()['account'],'test-doc-nas')
        done=worker.post('/api/storage-sync/files/'+file_id+'/result',headers=headers,json={'lease_token':task['lease_token'],'success':True,'target_path':task['target_path'],'sha256':saved['sha256']})
        self.assertEqual(done.status_code,200,done.text)
        self.assertEqual(self.admin.get('/api/download/'+file_id).status_code,409)
        self.assertEqual(self.admin.get('/api/preview/'+file_id).status_code,409)
        self.assertEqual(self.admin.post('/api/nas/files/'+file_id+'/read-ticket',json={'action':'download'}).status_code,409)
        ticket=doc.post('/api/nas/files/'+file_id+'/read-ticket',json={'action':'download'})
        self.assertEqual(ticket.status_code,200,ticket.text)
        grant=worker.post('/api/storage-sync/read/authorize',headers=headers,json={'ticket':ticket.json()['ticket']})
        self.assertEqual(grant.status_code,200,grant.text)
        self.assertEqual(grant.json()['account'],'test-doc-nas')
        self.assertIsInstance(grant.json()['size'],int)
        self.assertEqual(worker.post('/api/storage-sync/read/authorize',headers=headers,json={'ticket':ticket.json()['ticket']}).status_code,409)
        next_ticket=doc.post('/api/nas/files/'+file_id+'/read-ticket',json={'action':'download'}).json()['ticket']
        changed=self.h.update_user(self.admin,user['id'],{'can_download':False});self.assertEqual(changed.status_code,200,changed.text)
        self.assertEqual(worker.post('/api/storage-sync/read/authorize',headers=headers,json={'ticket':next_ticket}).status_code,409)

    def test_browser_roles_cannot_maintain_nas_or_impersonate_worker(self):
        user,password=self.h.create_user(self.admin,'nas-reader')
        reader=self.h.login('nas-reader',password)
        self.assertEqual(reader.get('/api/users/'+user['id']+'/nas-account').status_code,403)
        self.assertEqual(reader.put('/api/users/'+user['id']+'/nas-account',json={'account':'bad','password':'bad'}).status_code,403)
        self.assertEqual(reader.get('/api/admin/storage-sync').status_code,403)
        self.assertEqual(reader.post('/api/storage-sync/claim',json={}).status_code,401)
        self.assertEqual(self.admin.post('/api/storage-sync/claim',json={}).status_code,401)
    def test_missing_service_is_truthful_and_password_not_exposed(self):
        user=self.admin.get('/api/auth/me').json()['user']
        with patch.dict(os.environ,{'DMR_NAS_SYNC_TOKEN':'','DMR_NAS_READ_BASE_URL':''}):
            response=self.admin.put('/api/users/'+user['id']+'/nas-account',json={'account':'local-only','password':'DO-NOT-EXPOSE-THIS'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertNotIn('DO-NOT-EXPOSE-THIS',response.text)
            self.assertFalse(self.admin.get('/api/storage-status').json()['configured'])
            self.assertEqual(self.admin.post('/api/users/'+user['id']+'/nas-account/check',json={}).status_code,503)
            self.assertNotIn('DO-NOT-EXPOSE-THIS',self.admin.get('/api/users').text)
            self.assertNotIn('DO-NOT-EXPOSE-THIS',self.admin.get('/api/users/audit').text)
    def test_production_worker_auth_does_not_weaken_browser_origin_checks(self):
        cfg=replace(self.h.config,environment='production',allowed_hosts=('dmr.example.com',),trusted_origins=('https://dmr.example.com',),public_origin='https://dmr.example.com',cookie_secure=True,setup_allowed=False)
        client=TestClient(create_app(self.h.store,cfg),base_url='https://dmr.example.com',raise_server_exceptions=False);self.h.clients.append(client)
        self.assertEqual(client.post('/api/storage-sync/claim',json={},headers={'Authorization':'Bearer '+TOKEN}).status_code,200)
        self.assertEqual(client.post('/api/storage-sync/claim',json={},headers={'Authorization':'Bearer wrong'}).status_code,403)
        self.assertEqual(client.post('/api/storage-sync/claim',json={},headers={'Authorization':'Bearer '+TOKEN,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(client.post('/api/auth/login',json={'username':'jiajia','password':'AdminPass123'},headers={'Authorization':'Bearer '+TOKEN}).status_code,403)
