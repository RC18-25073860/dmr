import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_permissions import PermissionsHarness
from test_backend import b64,xlsx_bom

class ClassificationIntegrationTests(unittest.TestCase):
    def setUp(self):self.h=PermissionsHarness();self.client=self.h.setup_admin()
    def tearDown(self):self.h.close()
    def upload(self,product,registrant,filename):
        r=self.client.post('/api/analyze',json={'files':[{'name':filename,'data':b64(xlsx_bom('AUTO001','V1.0',product,registrant))}]})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def test_medical_automatic_archive_and_manual_override_preserved(self):
        batch=self.upload('UI06S PRU','广西由莱','AUTO001_V1.0-UI06S PRU【广西由莱】-部件.xlsx')
        self.assertEqual(batch['items'][0]['project_category'],'医疗')
        r=self.client.post('/api/commit',json={'batch_id':batch['batch_id']});self.assertEqual(r.status_code,200,r.text)
        state=self.client.get('/api/state').json();self.assertEqual(state['records'][0]['project_category'],'医疗')
        record=state['records'][0]
        r=self.client.post('/api/records/'+record['id']+'/classification',json={'project_category':'家电','expected_edit_token':record['edit_token']});self.assertEqual(r.status_code,200,r.text)
        from scripts.reclassify_existing import run
        run(self.h.data_dir,True)
        self.assertEqual(self.client.get('/api/state').json()['records'][0]['project_category'],'家电')
    def test_conflicting_subject_and_model_blocks_commit_until_human_review(self):
        batch=self.upload('W20','广西由莱','AUTO001_V1.0-W20【广西由莱】-部件.xlsx');item=batch['items'][0]
        self.assertEqual(item['project_category'],'待确认');self.assertEqual(item['status'],'conflict')
        self.client.post('/api/commit',json={'batch_id':batch['batch_id']})
        self.assertEqual(self.client.get('/api/state').json()['records'],[])
        r=self.client.post('/api/review',json={'batch_id':batch['batch_id'],'item_id':item['id'],'project_category':'医疗','expected_edit_token':self.client.get('/api/batches/'+batch['batch_id']).json()['items'][0]['edit_token']})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['item']['status'],'matched')
        r=self.client.post('/api/commit',json={'batch_id':batch['batch_id']});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/api/state').json()['records'][0]['project_category'],'医疗')
    def test_unknown_not_defaulted_to_home(self):
        batch=self.upload('NEW999','未知主体','AUTO001_V1.0-NEW999【未知主体】-部件.xlsx')
        self.assertEqual(batch['items'][0]['project_category'],'待确认')

class ClassificationReapplyTests(unittest.TestCase):
    def test_rule_results_are_idempotent_and_manual_pending_is_preserved(self):
        from backend.classification import apply_classification
        item={'product':'W20','module':'BOM','registrant':'深圳金茉','project_category':'待确认'}
        first=apply_classification(item)
        self.assertEqual(first,apply_classification(first))
        changed=dict(first,product='UI20S')
        self.assertEqual(apply_classification(changed)['project_category'],'医疗')
        manual=dict(first,project_category='待确认',project_category_source='人工核对')
        self.assertEqual(apply_classification(manual)['project_category'],'待确认')
