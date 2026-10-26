"""Independent simultaneous-editor checks against real SQLite transactions."""
import json
import sys
import threading
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_permissions import PermissionsHarness
from test_backend import b64, xlsx_bom
from backend.service import DMRStore
from server import create_app


class MultiuserEditingTests(unittest.TestCase):
    def setUp(self):
        self.h = PermissionsHarness()
        self.a = self.h.setup_admin()
        self.other_store = DMRStore(self.h.data_dir, None)
        self.b = TestClient(create_app(self.other_store, self.h.config), base_url='http://127.0.0.1', raise_server_exceptions=False)
        made = self.a.post('/api/users', json={'username':'second-editor','display_name':'第二位编辑者','password':'Temporary-Edit-2026','role':'admin','active':True,'scopes':[{'registrant':'*','product':'*','project_category':'*'}],'can_download':True,'can_history':True})
        self.assertEqual(made.status_code, 200, made.text)
        self.assertEqual(self.b.post('/api/auth/login',json={'username':'second-editor','password':'Temporary-Edit-2026'}).status_code,200)
        self.assertEqual(self.b.post('/api/auth/change-password',json={'current_password':'Temporary-Edit-2026','new_password':'Personal-Edit-2026'}).status_code,200)
        self.assertEqual(self.b.post('/api/auth/login',json={'username':'second-editor','password':'Personal-Edit-2026'}).status_code,200)

    def tearDown(self):
        self.b.close();self.other_store.close();self.h.close()

    def batch(self, code='EDIT-001'):
        r=self.a.post('/api/analyze',json={'files':[{'name':f'{code}_V1.0-TEST【测试主体】-部件.xlsx','data':b64(xlsx_bom(code,'V1.0','TEST','测试主体'))}]})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def payload(self,batch,notes):
        item=batch['items'][0]
        return {'batch_id':batch['batch_id'],'item_id':item['id'],'notes':notes,'expected_edit_token':item['edit_token']}

    def race(self, path, left, right):
        barrier=threading.Barrier(2);responses=[];errors=[]
        def run(client,payload):
            try:
                barrier.wait(timeout=5);responses.append(client.post(path,json=payload))
            except Exception as exc:errors.append(repr(exc))
        threads=[threading.Thread(target=run,args=(self.a,left)),threading.Thread(target=run,args=(self.b,right))]
        for t in threads:t.start()
        for t in threads:t.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
        self.assertEqual(sorted(r.status_code for r in responses),[200,409],[(r.status_code,r.text) for r in responses])
        return responses

    def test_review_requires_token_and_stale_save_preserves_first_update(self):
        batch=self.batch();p=self.payload(batch,'first')
        self.assertEqual(self.a.post('/api/review',json={k:v for k,v in p.items() if k!='expected_edit_token'}).status_code,428)
        self.assertEqual(self.a.post('/api/review',json=p).status_code,200)
        self.assertEqual(self.b.post('/api/review',json=p|{'notes':'stale'}).status_code,409)
        item=self.a.get('/api/batches/'+batch['batch_id']).json()['items'][0]
        self.assertEqual(item['notes'],'first');self.assertNotEqual(item['edit_token'],p['expected_edit_token'])
        self.assertEqual(self.a.post('/api/review',json=p|{'expected_edit_token':'非法凭据'}).status_code,409)

    def test_two_independent_connections_review_one_winner(self):
        batch=self.batch();responses=self.race('/api/review',self.payload(batch,'alice'),self.payload(batch,'bob'))
        winning=next(r.json()['item']['notes'] for r in responses if r.status_code==200)
        self.assertEqual(self.a.get('/api/batches/'+batch['batch_id']).json()['items'][0]['notes'],winning)

    def test_repeated_save_invalidates_token_even_when_values_same(self):
        batch=self.batch();p=self.payload(batch,'same')
        r=self.a.post('/api/review',json=p);self.assertEqual(r.status_code,200,r.text)
        fresh=r.json()['item']['edit_token']
        r=self.a.post('/api/review',json=p|{'expected_edit_token':fresh});self.assertEqual(r.status_code,200,r.text)
        self.assertNotEqual(fresh,r.json()['item']['edit_token'])
        self.assertEqual(self.b.post('/api/review',json=p|{'expected_edit_token':fresh}).status_code,409)

    def records(self):
        for code in ('EDIT-ONE','EDIT-TWO'):
            batch=self.batch(code)
            r=self.a.post('/api/commit',json={'batch_id':batch['batch_id']});self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['committed'],1)
        return self.a.get('/api/state').json()['records']

    def test_classification_token_covers_other_records_in_same_product(self):
        records=self.records();first,second=records[:2]
        r=self.a.post('/api/records/'+first['id']+'/classification',json={'project_category':'家电','expected_edit_token':first['edit_token']})
        self.assertEqual(r.status_code,200,r.text)
        r=self.b.post('/api/records/'+second['id']+'/classification',json={'project_category':'医疗','expected_edit_token':second['edit_token']})
        self.assertEqual(r.status_code,409,r.text)
        self.assertEqual({r['project_category'] for r in self.a.get('/api/state').json()['records']},{'家电'})

    def test_classification_concurrent_across_process_connections(self):
        row=self.records()[0]
        self.race('/api/records/'+row['id']+'/classification',{'project_category':'家电','expected_edit_token':row['edit_token']},{'project_category':'医疗','expected_edit_token':row['edit_token']})
        self.assertEqual(len({r['project_category'] for r in self.a.get('/api/state').json()['records']}),1)

    def test_classification_requires_token(self):
        row=self.records()[0]
        self.assertEqual(self.a.post('/api/records/'+row['id']+'/classification',json={'project_category':'家电'}).status_code,428)
        self.assertEqual({r['project_category'] for r in self.a.get('/api/state').json()['records']},{'待确认'})

    def test_archiving_invalidates_pending_review_snapshot(self):
        batch=self.batch();p=self.payload(batch,'stale')
        self.assertEqual(self.a.post('/api/commit',json={'batch_id':batch['batch_id']}).json()['committed'],1)
        self.assertEqual(self.b.post('/api/review',json=p).status_code,409)
        self.assertEqual(self.a.get('/api/batches/'+batch['batch_id']).json()['items'][0]['status'],'committed')


if __name__=='__main__':unittest.main()
