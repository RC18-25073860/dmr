"""Commit route creates local catalog reminders and never writes cloud docs."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_permissions import PermissionsHarness
from test_backend import b64, xlsx_bom


class WritebackRouteTests(unittest.TestCase):
    def setUp(self):
        self.h = PermissionsHarness()
        self.admin = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def upload(self, client, code='DMR-BOM-NO-CLOUD-WRITE'):
        response = client.post('/api/analyze', json={'files': [{
            'name': f'{code}_V1.0-W20【深圳金茉】-部件.xlsx',
            'data': b64(xlsx_bom(code, 'V1.0', 'W20', '深圳金茉', 'route test'))}]})
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()
        self.assertEqual(batch['items'][0]['status'], 'matched', batch)
        self.assertEqual(batch['items'][0]['code'], code)
        return batch

    def commit(self, client, batch):
        response = client.post('/api/commit', json={'batch_id': batch['batch_id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['committed'], 1, response.text)
        return response.json()

    def notice(self, notice_id):
        row = self.h.store.execute('SELECT * FROM manual_catalog_updates WHERE id=?', (notice_id,)).fetchone()
        self.assertIsNotNone(row)
        return dict(row) | {'payload': json.loads(row['payload'])}

    def assert_local_archive_and_notice(self, result, code):
        self.assertNotIn('test_writeback', result)
        self.assertEqual(len(result['manual_updates']), 1)
        item = result['results'][0]
        notice = self.notice(result['manual_updates'][0])
        self.assertEqual(notice['file_id'], item['file_id'])
        self.assertEqual(notice['record_id'], item['record_id'])
        self.assertEqual(notice['status'], 'pending')
        self.assertEqual(notice['payload']['code'], code)
        self.assertEqual(notice['payload']['new_version'], 'V1.0')
        self.assertEqual(notice['payload']['reason'], 'first_archive')
        with sqlite3.connect(self.h.store.db_path) as independent:
            archived = independent.execute(
                'SELECT f.status,f.version,r.business_code FROM files f JOIN records r ON r.id=f.record_id WHERE f.id=?',
                (item['file_id'],)).fetchone()
            self.assertEqual(archived, ('当前有效', 'V1.0', code))
            self.assertEqual(independent.execute(
                'SELECT COUNT(*) FROM manual_catalog_updates WHERE file_id=? AND status=?',
                (item['file_id'], 'pending')).fetchone()[0], 1)
        return notice

    def test_admin_commit_is_durable_and_creates_manual_update_only(self):
        code = 'DMR-BOM-NO-CLOUD-ADMIN'
        batch = self.upload(self.admin, code)
        result = self.commit(self.admin, batch)
        notice = self.assert_local_archive_and_notice(result, code)
        self.assertEqual(notice['payload']['created_by'], '佳佳')

    def test_legacy_writeback_switch_enabled_still_never_starts_cli(self):
        code = 'DMR-BOM-OLD-SWITCH-OFFLINE'
        batch = self.upload(self.admin, code)
        with patch.dict('os.environ', {'DMR_FEISHU_TEST_WRITEBACK': '1'}), \
                patch('subprocess.run') as cli:
            result = self.commit(self.admin, batch)
        cli.assert_not_called()
        self.assert_local_archive_and_notice(result, code)

    def test_document_controller_commit_creates_manual_update_without_cloud_hook(self):
        _, password = self.h.create_user(self.admin, 'controller', role='document_control')
        controller = self.h.login('controller', password)
        code = 'DMR-BOM-NO-CLOUD-DOC'
        batch = self.upload(controller, code)
        with patch('subprocess.run') as cli:
            result = self.commit(controller, batch)
        cli.assert_not_called()
        notice = self.assert_local_archive_and_notice(result, code)
        self.assertEqual(notice['payload']['created_by'], 'controller')

    def test_cloud_like_failure_path_no_longer_affects_archive_result(self):
        code = 'DMR-BOM-NO-CLOUD-FAILURE'
        batch = self.upload(self.admin, code)
        with patch.dict('os.environ', {'DMR_FEISHU_TEST_WRITEBACK': '1'}), \
                patch('subprocess.run', side_effect=AssertionError('cloud write must not run')):
            result = self.commit(self.admin, batch)
        self.assert_local_archive_and_notice(result, code)
        state = self.admin.get('/api/state').json()
        saved = next(row for row in state['libraryFiles'] if row['id'] == result['results'][0]['file_id'])
        self.assertEqual(saved['status'], '当前有效')


if __name__ == '__main__':
    unittest.main()
