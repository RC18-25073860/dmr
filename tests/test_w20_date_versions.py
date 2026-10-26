from pathlib import Path
import hashlib,os,sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_backend import Harness,b64,xlsx_bom
ROOT=Path(__file__).resolve().parents[1]
CODE='11W2002C12B'
class FinishedBomDateVersionTests(unittest.TestCase):
    def fixture(self):
        tmp=tempfile.TemporaryDirectory(prefix='w20-date-',dir=ROOT/'tests')
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{'DMR_AUTH_MODE':'local','DMR_LOCAL_ROLES':'admin'})
        env.start();self.addCleanup(env.stop)
        h=Harness(Path(tmp.name));self.addCleanup(h.close)
        return h
    def candidate(self,date,salt='',code=CODE,product='W20',registrant='深圳金茉'):
        return {'name':f'{code}_V1.0-{product}【{registrant}】-成品-{date}.xlsx','data':b64(xlsx_bom(code,'V1.0',product,registrant,salt))}
    def undated_candidate(self,salt='',code=CODE,product='W20',registrant='深圳金茉'):
        return {'name':f'{code}_V1.0-{product}【{registrant}】-成品.xlsx','data':b64(xlsx_bom(code,'V1.0',product,registrant,salt))}
    def analyze(self,h,*files):
        return h.request('POST','/api/analyze',{'files':list(files)})
    def commit(self,h,batch):
        return h.request('POST','/api/commit',{'batch_id':batch['batch_id']})
    def test_real_six_unordered_revisions_keep_latest_and_all_history(self):
        sample_root=Path(os.environ.get('DMR_REAL_WORKFLOW_DATA_DIR', 'tests/fixtures/real-workflow'))
        files=list((sample_root/'W20 BOM-不同版本update').glob('*.xlsx'))[::-1]
        if not files:
            self.skipTest('optional real W20 samples unavailable')
        h=self.fixture()
        self.assertEqual(len(files),6)
        batch=self.analyze(h,*[{'name':p.name,'data':b64(p.read_bytes())} for p in files])
        self.assertEqual(batch['summary']['matched'],6)
        result=self.commit(h,batch)
        self.assertEqual((result['committed'],result['blocked']),(6,0))
        state=h.request('GET','/api/state')
        self.assertEqual(len(state['records']),1)
        record=state['records'][0]
        self.assertEqual(record['version'],'20260907')
        self.assertEqual(record['previous'],'20260903')
        self.assertEqual(len(state['libraryFiles']),6)
        self.assertEqual(sum(f['status']=='当前有效' for f in state['libraryFiles']),1)
        source={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        for f in state['libraryFiles']:
            body,_=h.request('GET','/api/download/'+f['id'])
            self.assertEqual(hashlib.sha256(body).hexdigest(),source[f['name']])
        repeated=self.analyze(h,*[{'name':p.name,'data':b64(p.read_bytes())} for p in files])
        self.assertEqual(repeated['summary']['duplicate'],6)
        self.assertEqual(self.commit(h,repeated)['committed'],0)
        self.assertEqual(len(h.request('GET','/api/state')['libraryFiles']),6)
    def test_late_older_date_is_history_without_downgrading_current(self):
        h=self.fixture()
        self.commit(h,self.analyze(h,self.candidate('20260907','latest')))
        late=self.analyze(h,self.candidate('20260825','older'))
        self.assertEqual(late['items'][0]['status'],'matched')
        self.assertEqual(self.commit(h,late)['committed'],1)
        state=h.request('GET','/api/state')
        self.assertEqual(state['records'][0]['version'],'20260907')
        self.assertTrue(state['records'][0]['file'].endswith('20260907.xlsx'))
        self.assertEqual({f['version']:f['status'] for f in state['libraryFiles']},{'20260825':'历史版本','20260907':'当前有效'})
        changed=self.analyze(h,self.candidate('20260825','different old bytes'))
        self.assertEqual(changed['items'][0]['status'],'conflict')
        self.assertEqual(self.commit(h,changed)['committed'],0)
    def test_same_date_collision_stays_blocked_after_manual_review(self):
        h=self.fixture()
        batch=self.analyze(h,self.candidate('20260907','one'),self.candidate('20260907','two'))
        self.assertEqual(batch['summary']['conflict'],2)
        for item in batch['items']:
            h.request('POST','/api/review',{'batch_id':batch['batch_id'],'item_id':item['id'],'version':'20260907'})
        result=self.commit(h,batch)
        self.assertEqual((result['committed'],result['blocked']),(0,2))
        self.assertEqual(h.request('GET','/api/state')['records'],[])
    def test_manual_version_edit_cannot_override_filename_date(self):
        h=self.fixture()
        batch=self.analyze(h,self.candidate('20260907'))
        result=h.request('POST','/api/review',{'batch_id':batch['batch_id'],'item_id':batch['items'][0]['id'],'version':'20991231'})
        self.assertEqual(result['item']['status'],'conflict')
        self.assertEqual(self.commit(h,batch)['committed'],0)
    def test_all_finished_bom_codes_use_filename_date_version(self):
        h=self.fixture()
        other=self.analyze(h,self.candidate('20260529','other',code='11G2005E03B',product='G20',registrant='深圳金茉'))
        item=other['items'][0]
        self.assertEqual(item['version'],'20260529')
        self.assertEqual(item['metadata']['document_version'],'V1.0')
        self.assertEqual(item['metadata']['version_rule'],'filename_date')
        self.assertEqual(self.commit(h,other)['committed'],1)
        state=h.request('GET','/api/state')
        self.assertEqual(state['records'][0]['version'],'20260529')
        self.assertEqual(state['records'][0]['metadata']['document_version'],'V1.0')
    def test_invalid_or_missing_date_cannot_be_manually_recast_as_v1(self):
        h=self.fixture()
        bad=self.analyze(h,self.candidate('20260230'))
        bad_review=h.request('POST','/api/review',{'batch_id':bad['batch_id'],'item_id':bad['items'][0]['id'],'version':'V1.0','code':CODE,'module':'BOM','type':'成品'})
        self.assertNotIn(bad_review['item']['status'],{'matched','duplicate'})
        self.assertEqual(self.commit(h,bad)['committed'],0)
        missing=self.analyze(h,self.undated_candidate())
        missing_review=h.request('POST','/api/review',{'batch_id':missing['batch_id'],'item_id':missing['items'][0]['id'],'version':'V1.0','code':CODE,'module':'BOM','type':'成品'})
        self.assertNotIn(missing_review['item']['status'],{'matched','duplicate'})
        self.assertEqual(self.commit(h,missing)['committed'],0)
    def test_dated_bom_cannot_be_reassigned_to_another_identity(self):
        h=self.fixture()
        other=self.analyze(h,self.candidate('20260907','other',code='11W2002C13B'))
        self.assertEqual(other['items'][0]['version'],'20260907')
        self.commit(h,other)
        date_batch=self.analyze(h,self.candidate('20260907'))
        other_id=h.request('GET','/api/state')['records'][0]['id']
        reviewed=h.request('POST','/api/review',{'batch_id':date_batch['batch_id'],'item_id':date_batch['items'][0]['id'],'record_id':other_id,'version':'20260907'})
        self.assertEqual(reviewed['item']['status'],'conflict')
        self.assertEqual(self.commit(h,date_batch)['committed'],0)
        self.assertEqual(h.request('GET','/api/state')['records'][0]['version'],'20260907')
    def test_commit_rechecks_stale_same_date_batch(self):
        h=self.fixture()
        first=self.analyze(h,self.candidate('20260907','first'))
        stale=self.analyze(h,self.candidate('20260907','second'))
        self.assertEqual(self.commit(h,first)['committed'],1)
        self.assertEqual(self.commit(h,stale)['committed'],0)
        self.assertEqual(len(h.request('GET','/api/state')['libraryFiles']),1)

    def test_recently_updated_old_batch_is_restored_first(self):
        h=self.fixture()
        first=self.analyze(h,self.candidate('20260825'))
        second=self.analyze(h,self.candidate('20260907'))
        store=h.server_mod.STORE
        with store.lock,store.conn:
            store.execute('UPDATE batches SET created_at=?,updated_at=? WHERE id=?',('2026-08-01T10:00:00','2026-09-21T10:00:00',first['batch_id']))
            store.execute('UPDATE batches SET created_at=?,updated_at=? WHERE id=?',('2026-09-01T10:00:00','2026-09-20T10:00:00',second['batch_id']))
        self.assertEqual(h.request('GET','/api/state')['batches'][0]['id'],first['batch_id'])

    def test_archive_log_shows_old_date_to_new_date(self):
        h=self.fixture()
        self.assertEqual(self.commit(h,self.analyze(h,self.candidate('20260825','old')))['committed'],1)
        self.assertEqual(self.commit(h,self.analyze(h,self.candidate('20260907','new')))['committed'],1)
        logs=h.request('GET','/api/state')['operationLogs']
        self.assertTrue(any('20260825 → 20260907' in log['change'] for log in logs), logs)

if __name__=='__main__':unittest.main()
