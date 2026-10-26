import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_backend import Harness, b64, xlsx_bom

class InitialLatestOnlyTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'DMR_AUTH_MODE':'local','DMR_LOCAL_ROLES':'admin'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent); self.addCleanup(self.tmp.cleanup)
        self.h = Harness(Path(self.tmp.name)); self.addCleanup(self.h.close)
    def candidate(self, date, salt=''):
        code='11W2002C12B'
        return {'name':f'{code}_V1.0-W20【深圳优莱】-成品-{date}.xlsx', 'data':b64(xlsx_bom(code,'V1.0','W20','深圳优莱',salt))}
    def run_batch(self, *files, latest=True):
        batch=self.h.request('POST','/api/analyze',{'files':list(files)})
        result=self.h.server_mod.STORE.commit_batch(batch['batch_id'], initial_latest_only=latest)
        return result, self.h.request('GET','/api/state')
    def test_first_import_keeps_latest_date(self):
        result,state=self.run_batch(self.candidate('20260907'),self.candidate('20260825'))
        self.assertEqual(result['committed'],1)
        self.assertEqual([f['version'] for f in state['libraryFiles']],['20260907'])
        self.assertEqual(sum(r['status']=='skipped' for r in result['results']),1)
    def test_existing_records_keep_history_and_upgrade(self):
        self.run_batch(self.candidate('20260907'))
        result,state=self.run_batch(self.candidate('20260825'),self.candidate('20260910'))
        self.assertEqual(result['committed'],2)
        self.assertEqual(state['records'][0]['version'],'20260910')
        self.assertEqual(len(state['libraryFiles']),3)
        self.assertEqual(sum(f['status']=='当前有效' for f in state['libraryFiles']),1)
    def test_explicit_history_import_preserves_all_dates(self):
        result,state=self.run_batch(self.candidate('20260825'),self.candidate('20260907'),latest=False)
        self.assertEqual(result['committed'],2)
        self.assertEqual(len(state['libraryFiles']),2)
    def test_latest_conflict_does_not_promote_older_version(self):
        result,state=self.run_batch(self.candidate('20260825'),self.candidate('20260907','one'),self.candidate('20260907','two'))
        self.assertEqual(result['committed'],0)
        self.assertEqual(state['records'],[])
if __name__=='__main__': unittest.main()
