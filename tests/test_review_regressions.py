"""Regression tests for the September 23 code review; isolated databases only."""
import json
import unittest
import test_mail_ingest as mail
from test_backend import xlsx_bom


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = mail.MailIngestTests()
        self.fixture.setUp()
        self.h, self.admin = self.fixture.h, self.fixture.admin
        self.h.create_mail_client(self.admin)
        self.contents = {}

    def tearDown(self):
        self.fixture.tearDown()

    def upload(self, key, salt=''):
        if salt not in self.contents:
            self.contents[salt] = xlsx_bom('QUALITY001', 'V1.0', 'W20', '深圳金茉', salt)
        data = self.contents[salt]
        payload = self.fixture.content_payload(data, filename='QUALITY001_V1.0-W20【深圳金茉】-部件.xlsx',
                                              message_id=key, attachment_id=key)
        return self.fixture.ingest(payload).json(), data

    def review(self, receipt):
        item = self.admin.get('/api/batches/' + receipt['batch_id']).json()['items'][0]
        result = self.admin.post('/api/review', json={
            'batch_id': receipt['batch_id'], 'item_id': item['id'], 'expected_edit_token': item['edit_token'],
            'code': 'QUALITY001', 'registrant': '深圳金茉', 'product': 'W20', 'module': 'BOM',
            'type': '部件', 'version': 'V1.0', 'project_category': '家电'})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()['item']

    def receipt(self, receipt):
        response = self.h.bearer_client().get('/api/integrations/mail/receipts/' + receipt['receipt_id'])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def commit(self, receipt):
        result = self.admin.post('/api/commit', json={'batch_id': receipt['batch_id']})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_missing_and_corrupt_blob_persist_failure_and_can_be_repaired(self):
        for kind in ('missing', 'corrupt'):
            with self.subTest(kind=kind):
                receipt, data = self.upload(kind, kind)
                item = self.review(receipt)
                blob = self.h.store.data_dir / item['metadata']['blob_path']
                if kind == 'missing': blob.unlink()
                else: blob.write_bytes(b'corrupt')
                result = self.commit(receipt)
                self.assertEqual(result['blocked'], 1)
                after = self.receipt(receipt)
                self.assertEqual(after['status'], 'needs_review')
                self.assertEqual(after['reason'], result['results'][0]['message'])
                exceptions = self.admin.get('/api/state').json()['exceptions']
                self.assertTrue(any(e['itemId'] == receipt['item_id'] and e['status'] == 'open' for e in exceptions))
                blob.write_bytes(data)
                fixed = self.review(receipt)
                after = self.receipt(receipt)
                self.assertEqual(after['reason'], fixed['recommendation'])
                self.assertNotIn('无法归档', after['reason'])
                # No commit here: both fixtures must remain independent new-record cases.
                self.assertEqual(after['status'], 'awaiting_confirmation')

    def test_late_duplicate_and_conflict_are_visible_in_receipt(self):
        first, _ = self.upload('first')
        duplicate, _ = self.upload('duplicate')
        conflict, _ = self.upload('conflict', 'different content')
        for receipt in (first, duplicate, conflict): self.review(receipt)
        self.assertEqual(self.commit(first)['committed'], 1)
        result = self.commit(duplicate)
        self.assertEqual(result['results'][0]['status'], 'skipped')
        self.assertEqual(self.receipt(duplicate)['status'], 'skipped')
        self.assertEqual(self.commit(duplicate)['blocked'], 0)
        result = self.commit(conflict)
        self.assertEqual(result['blocked'], 1)
        self.assertEqual(self.receipt(conflict)['status'], 'needs_review')
        self.assertIn('同版本', self.receipt(conflict)['reason'])
        # Replaying a completed batch must not turn the archived receipt into a failure.
        self.commit(first)
        self.assertEqual(self.receipt(first)['status'], 'archived')
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM files').fetchone()['n'], 1)

    def test_pending_exception_survives_more_than_100_new_batches(self):
        receipt, _ = self.upload('old')
        item = self.admin.get('/api/batches/' + receipt['batch_id']).json()['items'][0]
        self.admin.post('/api/review', json={'batch_id':receipt['batch_id'], 'item_id':item['id'],
            'expected_edit_token':item['edit_token'], 'save_pending':True, 'review_note':'等待资料'})
        with self.h.store.lock, self.h.store.conn:
            for index in range(100):
                self.h.store.execute('INSERT INTO batches(id,status,created_at,updated_at,summary) VALUES(?,?,?,?,?)',
                    (f'NEW-{index:03}', 'committed', '2099-01-01T00:00:00', '2099-01-01T00:00:00', '{}'))
        state = self.admin.get('/api/state').json()
        self.assertTrue(any(e['batchId'] == receipt['batch_id'] for e in state['exceptions']))
        self.assertTrue(any(b['id'] == receipt['batch_id'] for b in state['batches']))
        outsider, password = self.h.create_user(self.admin, 'outside', scopes=[
            {'registrant':'别的公司','project_category':'*','product':'*'}])
        outside = self.h.login('outside', password)
        self.assertEqual(outside.get('/api/batches/' + receipt['batch_id']).status_code, 404)
        self.assertFalse(any(e['batchId'] == receipt['batch_id'] for e in outside.get('/api/state').json()['exceptions']))
