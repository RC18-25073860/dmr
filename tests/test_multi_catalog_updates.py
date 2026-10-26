import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.manual_catalog_updates import ManualCatalogUpdates
from test_feishu_catalog import FeishuCatalogHarness, SHEET_TOKEN, sample_snapshot


SECOND_TOKEN = 'SecondCatalogSheetToken01'


class MultiCatalogUpdateTests(unittest.TestCase):
    def setUp(self):
        self.h = FeishuCatalogHarness()
        self.admin = self.h.setup_admin()
        snapshot = sample_snapshot()
        snapshot['documents'][0]['name'] = '深圳金荣-W20 DMR清单'
        snapshot['documents'][0]['path'][-1] = snapshot['documents'][0]['name']
        second = copy.deepcopy(snapshot['documents'][0])
        second['token'] = SECOND_TOKEN
        second['name'] = '广西由莱-W20 共享DMR清单'
        second['url'] = f'https://example.feishu.cn/sheets/{SECOND_TOKEN}'
        second['path'] = ['DMR清单', '2.家电项目', 'W20', second['name']]
        snapshot['documents'].append(second)
        self.h.write_snapshot(snapshot)
        self.service = ManualCatalogUpdates(self.h.store, self.h.config)
        self.h.store.execute(
            "INSERT INTO records(id,business_code,name,registrant,type,project_category,product,module,version,previous) "
            "VALUES('r1','11W2002C12B','测试','深圳金荣','成品','家电','W20','BOM','20260924','20260410')")
        self.add_file('f1', '20260924')

    def tearDown(self):
        self.h.close()

    def add_file(self, file_id, version):
        self.h.store.execute(
            "INSERT INTO files(id,record_id,name,version,status,sha256,blob_path) "
            "VALUES(?,'r1','BOM.xlsx',?,'当前有效',?,'files/test')",
            (file_id, version, 'sha-' + file_id))

    def enqueue(self, file_id='f1'):
        with self.h.store.conn:
            return self.service.enqueue_commit({'batch_id': 'B1', 'results': [{'status': 'committed', 'file_id': file_id}]}, {'display_name': '文控'})

    def items(self):
        response = self.admin.get('/api/catalog-updates')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['items']

    def associate(self, item, tokens=(SHEET_TOKEN, SECOND_TOKEN), status=200, note='确认一份文件需要同步两份清单'):
        response = self.admin.post('/api/catalog-updates/' + item['id'] + '/associations',
                                   json={'revision': item['revision'], 'document_tokens': list(tokens), 'note': note})
        self.assertEqual(response.status_code, status, response.text)
        return response

    def resolve(self, item, token, status=200, note=''):
        response = self.admin.post('/api/catalog-updates/' + item['id'] + '/resolve',
                                   json={'revision': item['revision'], 'document_token': token, 'note': note})
        self.assertEqual(response.status_code, status, response.text)
        return response

    def test_partial_and_complete_multiple_targets(self):
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['target_count'], 1)

        item = self.associate(item).json()
        self.assertEqual(item['target_count'], 2)
        self.assertEqual(item['processed_count'], 0)
        self.assertEqual(item['status'], 'pending')

        item = self.resolve(item, SHEET_TOKEN).json()
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['processed_count'], 1)
        self.assertEqual([t['status'] for t in item['targets']], ['processed', 'pending'])

        item = self.resolve(item, SECOND_TOKEN).json()
        self.assertEqual(item['status'], 'processed')
        self.assertEqual(item['processed_count'], 2)
        self.assertEqual(item['target_count'], 2)
        self.assertEqual(self.items()[0]['status'], 'processed')

    def test_processed_notice_can_add_new_pending_target(self):
        self.enqueue()
        item = self.items()[0]
        item = self.resolve(item, SHEET_TOKEN).json()
        self.assertEqual(item['status'], 'processed')

        item = self.associate(item, status=200).json()
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['target_count'], 2)
        self.assertEqual(item['processed_count'], 1)
        self.assertEqual([t['status'] for t in item['targets']], ['processed', 'pending'])

        item = self.resolve(item, SECOND_TOKEN).json()
        self.assertEqual(item['status'], 'processed')
        self.assertEqual(item['processed_count'], 2)

    def test_resaving_processed_associations_keeps_processed_when_no_pending_work(self):
        self.enqueue()
        item = self.associate(self.items()[0]).json()
        item = self.resolve(item, SHEET_TOKEN).json()
        item = self.resolve(item, SECOND_TOKEN).json()
        self.assertEqual(item['status'], 'processed')

        item = self.associate(item).json()
        self.assertEqual(item['status'], 'processed')
        self.assertEqual(item['processed_count'], 2)
        self.assertEqual(item['target_count'], 2)
        self.assertEqual([target['status'] for target in item['targets']], ['processed', 'processed'])

    def test_future_upload_inherits_confirmed_targets_only_for_same_identity(self):
        self.enqueue()
        item = self.associate(self.items()[0]).json()
        item = self.resolve(item, SHEET_TOKEN).json()
        item = self.resolve(item, SECOND_TOKEN).json()

        self.h.store.execute("UPDATE files SET status='历史版本' WHERE id='f1'")
        self.h.store.execute("UPDATE records SET version='20260925',previous='20260924' WHERE id='r1'")
        self.add_file('f2', '20260925')
        self.enqueue('f2')
        inherited = next(item for item in self.items() if item['new_version'] == '20260925')
        self.assertEqual(inherited['association_source'], 'confirmed_history')
        self.assertEqual([t['document_token'] for t in inherited['targets']], [SHEET_TOKEN, SECOND_TOKEN])
        self.assertEqual([t['association_confirmed'] for t in inherited['targets']], [True, True])
        self.assertEqual([t['status'] for t in inherited['targets']], ['pending', 'pending'])

        self.h.store.execute("UPDATE files SET status='历史版本' WHERE id='f2'")
        self.h.store.execute("UPDATE records SET product='W20 Pro',version='20260926',previous='20260925' WHERE id='r1'")
        self.add_file('f3', '20260926')
        self.enqueue('f3')
        changed_identity = next(item for item in self.items() if item['new_version'] == '20260926')
        self.assertNotEqual(changed_identity.get('association_source'), 'confirmed_history')
        self.assertFalse(any(t.get('association_confirmed') for t in changed_identity['targets']))

    def test_latest_confirmed_association_set_supersedes_older_history(self):
        self.enqueue()
        item = self.associate(self.items()[0]).json()
        item = self.associate(item, tokens=(SHEET_TOKEN,), note='确认只有主清单需要维护').json()
        self.assertEqual([target['document_token'] for target in item['targets']], [SHEET_TOKEN])

        self.h.store.execute("UPDATE files SET status='历史版本' WHERE id='f1'")
        self.h.store.execute("UPDATE records SET version='20260925',previous='20260924' WHERE id='r1'")
        self.add_file('f2', '20260925')
        self.enqueue('f2')
        inherited = next(item for item in self.items() if item['new_version'] == '20260925')
        self.assertEqual(inherited['association_source'], 'confirmed_history')
        self.assertEqual([target['document_token'] for target in inherited['targets']], [SHEET_TOKEN])

    def test_association_validation_stale_duplicate_unknown_and_processed_removal(self):
        self.enqueue()
        item = self.items()[0]
        url = '/api/catalog-updates/' + item['id'] + '/associations'
        self.assertEqual(self.admin.post(url, json={'revision': 999, 'document_tokens': [SHEET_TOKEN], 'note': 'x'}).status_code, 409)
        self.assertEqual(self.admin.post(url, json={'revision': item['revision'], 'document_tokens': [SHEET_TOKEN, SHEET_TOKEN], 'note': 'x'}).status_code, 400)
        self.assertEqual(self.admin.post(url, json={'revision': item['revision'], 'document_tokens': ['missing'], 'note': 'x'}).status_code, 400)
        self.assertEqual(self.admin.post(url, json={'revision': item['revision'], 'document_tokens': [SHEET_TOKEN], 'note': ''}).status_code, 400)

        item = self.associate(item).json()
        item = self.resolve(item, SHEET_TOKEN).json()
        self.assertEqual(self.admin.post(url, json={'revision': item['revision'], 'document_tokens': [SECOND_TOKEN], 'note': 'remove done'}).status_code, 400)

    def test_multiple_targets_require_explicit_document_token_to_resolve(self):
        self.enqueue()
        item = self.associate(self.items()[0]).json()
        response = self.admin.post('/api/catalog-updates/' + item['id'] + '/resolve',
                                   json={'revision': item['revision'], 'note': '少了清单选择'})
        self.assertEqual(response.status_code, 400, response.text)

    def test_permissions_and_changed_identity_block_multiple_associations(self):
        self.enqueue()
        item = self.items()[0]
        reader = self.h.create_user(self.admin, 'globalreader2', [{'registrant': '*', 'project_category': '*', 'product': '*'}])
        self.assertEqual(reader.post('/api/catalog-updates/' + item['id'] + '/associations',
                                     json={'revision': item['revision'], 'document_tokens': [SHEET_TOKEN], 'note': 'x'}).status_code, 403)

        self.h.store.execute("UPDATE records SET module='软件' WHERE id='r1'")
        self.associate(item, status=409)
        self.resolve(item, SHEET_TOKEN, status=409)

    def test_legacy_single_resolve_still_returns_target_progress(self):
        self.enqueue()
        item = self.items()[0]
        response = self.admin.post('/api/catalog-updates/' + item['id'] + '/resolve',
                                   json={'revision': item['revision'], 'note': '人工核对完成'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['status'], 'processed')
        self.assertEqual(body['target_count'], 1)
        self.assertEqual(body['processed_count'], 1)
        self.assertEqual(body['targets'][0]['document_token'], SHEET_TOKEN)


if __name__ == '__main__':
    unittest.main()
