import json
import unittest
from test_permissions import PermissionsHarness
from test_backend import b64, xlsx_bom

class ReviewFilesTests(unittest.TestCase):
    def setUp(self):
        self.h=PermissionsHarness(); self.admin=self.h.setup_admin()
        self.data=xlsx_bom('1122334455','V1.0','W20','测试主体','review evidence')
        response=self.admin.post('/api/analyze',json={'files':[{'name':'1122334455_V1.0-W20【测试主体】-部件.xlsx','data':b64(self.data)}]})
        assert response.status_code==200,response.text
        self.batch=response.json()['batch_id']
        self.item=self.h.store.execute('SELECT * FROM batch_items WHERE batch_id=?',(self.batch,)).fetchone()
        self.url=f"/api/batch-files/{self.batch}/{self.item['id']}"
    def tearDown(self): self.h.close()
    def test_original_bytes_and_preview_before_archive(self):
        response=self.admin.get(self.url+'/download')
        self.assertEqual(response.status_code,200);self.assertEqual(response.content,self.data)
        self.assertIn('attachment',response.headers['content-disposition'])
        preview=self.admin.get(self.url+'/preview')
        self.assertEqual(preview.status_code,200);self.assertIn('review evidence',preview.text)
        self.assertEqual(preview.headers['content-security-policy'],'sandbox')
    def test_permissions_and_wrong_item(self):
        self.assertEqual(self.h.client().get(self.url+'/download').status_code,401)
        _,password=self.h.create_user(self.admin,'review-reader')
        self.assertEqual(self.h.login('review-reader',password).get(self.url+'/preview').status_code,403)
        _,password=self.h.create_user(self.admin,'review-doc',role='document_control',can_download=False)
        doc=self.h.login('review-doc',password)
        self.assertEqual(doc.get(self.url+'/download').status_code,403)
        self.assertEqual(doc.get(self.url+'/preview').status_code,200)
        _,password=self.h.create_user(self.admin,'foreign-doc',role='document_control',scopes=[{'registrant':'别的公司','project_category':'*','product':'*'}])
        self.assertEqual(self.h.login('foreign-doc',password).get(self.url+'/preview').status_code,404)
        self.assertEqual(self.admin.get(f'/api/batch-files/{self.batch}/wrong/preview').status_code,404)
    def test_corruption_and_committed_cannot_bypass_archive(self):
        path=self.h.store.data_dir/json.loads(self.item['metadata'])['blob_path']
        path.write_bytes(b'changed')
        self.assertEqual(self.admin.get(self.url+'/download').status_code,409)
        path.write_bytes(self.data)
        with self.h.store.conn:
            self.h.store.execute("UPDATE batch_items SET status='committed' WHERE id=?",(self.item['id'],))
        self.assertEqual(self.admin.get(self.url+'/preview').status_code,409)
    def test_pending_is_durable_and_explicit_review_releases(self):
        token=self.h.store.row_item(self.item)['edit_token']
        response=self.admin.post('/api/review',json={'batch_id':self.batch,'item_id':self.item['id'],'expected_edit_token':token,'save_pending':True,'review_note':'等待确认对应产品'})
        self.assertEqual(response.status_code,200,response.text)
        item=response.json()['item'];self.assertEqual(item['status'],'unmatched')
        self.assertEqual(item['metadata']['review_note'],'等待确认对应产品')
        result=self.admin.post('/api/commit',json={'batch_id':self.batch})
        self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['committed'],0)
        item=self.h.store.row_item(self.h.store.execute('SELECT * FROM batch_items WHERE id=?',(self.item['id'],)).fetchone())
        response=self.admin.post('/api/review',json={'batch_id':self.batch,'item_id':item['id'],'expected_edit_token':item['edit_token'],'save_pending':False})
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['item']['status'],'matched')
    def test_missing_fields_can_save_then_complete(self):
        token=self.h.store.row_item(self.item)['edit_token']
        payload={'batch_id':self.batch,'item_id':self.item['id'],'expected_edit_token':token,'registrant':'','product':''}
        response=self.admin.post('/api/review',json=payload);self.assertEqual(response.status_code,200,response.text)
        item=response.json()['item'];self.assertEqual(item['status'],'unmatched');self.assertIn('公司主体',item['recommendation'])
        payload.update(expected_edit_token=item['edit_token'],registrant='测试主体',product='W20')
        response=self.admin.post('/api/review',json=payload);self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['item']['status'],'matched')
