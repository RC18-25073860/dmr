import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_feishu_catalog import sample_snapshot
from test_permissions import PermissionsHarness
from test_backend import b64, xlsx_bom


class ArchiveCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.h = PermissionsHarness()
        self.a = self.h.setup_admin()
        self.store = self.h.store

    def tearDown(self):
        self.h.close()

    def archive(self, code, version='V1.0', product='TEST'):
        response = self.a.post('/api/analyze', json={'files': [{'name': f'{code}_{version}-{product}【测试主体】-部件.xlsx', 'data': b64(xlsx_bom(code, version, product, '测试主体'))}]})
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()
        response = self.a.post('/api/commit', json={'batch_id': batch['batch_id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['committed'], 1, response.text)
        return dict(self.store.execute('SELECT * FROM files WHERE source=?', (batch['batch_id'],)).fetchone())

    def payload(self, file, **fields):
        response = self.a.get(f"/api/files/{file['id']}/correction")
        self.assertEqual(response.status_code, 200, response.text)
        return {'expected_edit_token': response.json()['edit_token'], 'reason': '原归档位置有误，按清单更正', **fields}

    def move(self, file, **fields):
        return self.a.post(f"/api/files/{file['id']}/correction", json=self.payload(file, **fields))

    def write_catalog_snapshot(self):
        (self.h.data_dir / "feishu_catalog.json").write_text(json.dumps(sample_snapshot(), ensure_ascii=False), encoding="utf-8")

    def record(self, id):
        return dict(self.store.execute('SELECT * FROM records WHERE id=?', (id,)).fetchone())

    def test_move_current_rebuilds_both_histories_and_audits_unchanged_bytes(self):
        old = self.archive('SRC')
        moved = self.archive('SRC', 'V3.0')
        target = self.archive('DST', 'V2.0')
        content = self.store.read_blob(moved['blob_path'])
        response = self.move(moved, target_record_id=target['record_id'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.record(old['record_id'])['version'], 'V1.0')
        self.assertEqual(self.record(old['record_id'])['previous'], '')
        self.assertEqual(self.record(target['record_id'])['version'], 'V3.0')
        self.assertEqual(self.record(target['record_id'])['previous'], 'V2.0')
        self.assertEqual(self.store.read_blob(moved['blob_path']), content)
        log = self.store.execute("SELECT * FROM logs WHERE action='归档更正'").fetchone()
        audit = json.loads(log['change'])
        self.assertEqual(audit['before']['file']['sha256'], audit['after']['file']['sha256'])
        self.assertIn('佳佳', log['user'])
        self.assertEqual(audit['after']['file']['record_id'], target['record_id'])
        self.assertEqual(self.store.execute('SELECT record_id FROM nas_sync_jobs WHERE file_id=?', (moved['id'],)).fetchone()['record_id'], target['record_id'])

    def test_correction_supersedes_old_pending_catalog_notice_and_creates_new_target_notice(self):
        self.write_catalog_snapshot()
        moved = self.archive('SRC-CAT', 'V3.0')
        old_notice = self.store.execute("SELECT id,revision,status FROM manual_catalog_updates WHERE file_id=?", (moved['id'],)).fetchone()
        self.assertEqual(old_notice['status'], 'pending')
        target = self.archive('DST-CAT', 'V2.0', 'OTHER')

        response = self.move(moved, target_record_id=target['record_id'])
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body['manual_updates']), 1)
        self.assertEqual(body['manual_updates'][0], old_notice['id'])
        rows = [dict(row) for row in self.store.execute(
            "SELECT status,revision,payload FROM manual_catalog_updates WHERE file_id=?",
            (moved['id'],),
        ).fetchall()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'pending')
        self.assertGreater(rows[0]['revision'], old_notice['revision'])
        item = json.loads(rows[0]['payload'])
        self.assertEqual(item['id'], body['manual_updates'][0])
        self.assertEqual(item['record_id'], target['record_id'])
        self.assertEqual(item['file_id'], moved['id'])
        self.assertEqual(item['code'], 'DST-CAT')
        self.assertEqual(item['registrant'], '测试主体')
        self.assertEqual(item['product'], 'OTHER')
        self.assertEqual(item['new_version'], 'V3.0')
        stale = self.a.post('/api/catalog-updates/' + old_notice['id'] + '/resolve', json={'revision': old_notice['revision'], 'note': '旧提醒不能处理'})
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_move_historical_preserves_newest_and_new_identity_keeps_empty_shell(self):
        history = self.archive('SRC')
        self.archive('SRC', 'V3.0')
        target = self.archive('DST', 'V2.0')
        response = self.move(history, target_record_id=target['record_id'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.record(target['record_id'])['version'], 'V2.0')
        self.assertEqual(self.record(target['record_id'])['previous'], 'V1.0')
        self.assertEqual(self.record(history['record_id'])['previous'], '')
        single = self.archive('ALONE')
        response = self.move(single, code='FIXED', registrant='测试主体', product='OTHER', module='BOM')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.record(response.json()['record_id'])['business_code'], 'FIXED')
        self.assertEqual(self.record(single['record_id'])['version'], '')

    def test_same_version_and_stale_snapshot_roll_back(self):
        source = self.archive('SRC')
        target = self.archive('DST')
        response = self.move(source, target_record_id=target['record_id'])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.execute('SELECT record_id FROM files WHERE id=?', (source['id'],)).fetchone()['record_id'], source['record_id'])
        payload = self.payload(source, code='FIXED', registrant='测试主体', product='TEST', module='BOM')
        self.archive('DST', 'V2.0')
        response = self.a.post(f"/api/files/{source['id']}/correction", json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.execute("SELECT COUNT(*) AS n FROM logs WHERE action='归档更正'").fetchone()['n'], 0)

    def test_permissions_scope_and_history(self):
        source = self.archive('SRC')
        target = self.archive('DST', 'V2.0', 'OTHER')
        for username, role, history, expected in [('reader', 'reader', True, 403), ('doc', 'document_control', True, 404)]:
            _, password = self.h.create_user(self.a, username, role, [{'registrant':'测试主体','project_category':'*','product':'TEST'}], can_history=history)
            client = self.h.login(username, password)
            payload = self.payload(source, target_record_id=target['record_id'])
            response = client.post(f"/api/files/{source['id']}/correction", json=payload)
            # Each principal gets a scope-specific token.
            if role == 'document_control':
                payload['expected_edit_token'] = client.get(f"/api/files/{source['id']}/correction").json()['edit_token']
                response = client.post(f"/api/files/{source['id']}/correction", json=payload)
            self.assertEqual(response.status_code, expected, response.text)
        _, password = self.h.create_user(self.a, 'nohistory', 'document_control', can_history=False)
        client = self.h.login('nohistory', password)
        payload = self.payload(source, target_record_id=target['record_id'])
        response = client.post(f"/api/files/{source['id']}/correction", json=payload)
        self.assertEqual(response.status_code, 403, response.text)

    def test_empty_bom_shell_no_longer_counts_as_linked_and_missing_blob_blocks(self):
        bom = self.archive('BOM-OLD')
        response = self.move(bom, code='BOM-FIXED', registrant='测试主体', product='TEST', module='BOM')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.link_status('BOM-OLD', '测试主体', 'TEST'), 'BOM缺失')
        current = self.archive('MISSING')
        (self.store.data_dir / current['blob_path']).unlink()
        response = self.move(current, code='FIXED', registrant='测试主体', product='TEST', module='BOM')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('原件', response.text)
        self.assertEqual(self.store.execute('SELECT record_id FROM files WHERE id=?', (current['id'],)).fetchone()['record_id'], current['record_id'])

    def test_dated_bom_cannot_change_code_or_module(self):
        source = self.archive('DATE')
        self.store.execute('UPDATE files SET version=?,metadata=? WHERE id=?', ('20260922', json.dumps({'version_rule':'filename_date', 'version_date':'20260922', 'source_bom_code':'DATE'}), source['id']))
        response = self.move(source, code='OTHER', registrant='测试主体', product='TEST', module='BOM')
        self.assertEqual(response.status_code, 409, response.text)
        response = self.move(source, code='DATE', registrant='测试主体', product='OTHER', module='软件')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.execute('SELECT record_id FROM files WHERE id=?', (source['id'],)).fetchone()['record_id'], source['record_id'])

    def test_remote_job_and_incompatible_versions_block(self):
        source = self.archive('SRC')
        self.store.execute("UPDATE nas_sync_jobs SET status='pending',attempts=1 WHERE file_id=?", (source['id'],))
        response = self.move(source, code='FIXED', registrant='测试主体', product='TEST', module='BOM')
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.execute("SELECT COUNT(*) AS n FROM records WHERE business_code='FIXED'").fetchone()['n'], 0)
        self.store.execute("UPDATE nas_sync_jobs SET status='blocked',attempts=0 WHERE file_id=?", (source['id'],))
        target = self.archive('DST', 'V2.0')
        self.store.execute("UPDATE files SET version='garbage' WHERE id=?", (target['id'],))
        response = self.move(source, target_record_id=target['record_id'])
        self.assertEqual(response.status_code, 409, response.text)

    def test_unclaimed_jobs_can_be_corrected_and_require_recheck(self):
        for index, status in enumerate(('pending', 'blocked')):
            with self.subTest(status=status):
                source = self.archive(f'SRC{index}')
                self.store.execute("UPDATE nas_sync_jobs SET status=?,target_path='/old/stale.xlsx',target_name='stale.xlsx' WHERE file_id=?", (status, source['id']))
                response = self.move(source, code=f'FIXED{index}', registrant='测试主体', product='TEST', module='BOM')
                self.assertEqual(response.status_code, 200, response.text)
                job = dict(self.store.execute('SELECT * FROM nas_sync_jobs WHERE file_id=?', (source['id'],)).fetchone())
                self.assertEqual(job['record_id'], response.json()['record_id'])
                self.assertEqual(job['status'], 'blocked')
                self.assertEqual(job['target_path'], '')
                self.assertEqual(job['target_name'], '')
                self.assertEqual(job['next_attempt_at'], '')
                self.assertIn('核对', job['last_error'])
                self.assertEqual(job['attempts'], 0)

    def test_any_remote_delivery_evidence_blocks_correction_atomically(self):
        cases = [('attempts', 1), ('synced_at', '2026-10-26'),
                 ('lease_token', 'lease'), ('lease_until', '2026-10-27'),
                 ('last_heartbeat_at', '2026-10-26'), ('status', 'transferring'), ('status', 'synced')]
        for index, (field, value) in enumerate(cases):
            with self.subTest(field=field, value=value):
                source = self.archive(f'GUARD{index}')
                self.store.execute(f'UPDATE nas_sync_jobs SET {field}=? WHERE file_id=?', (value, source['id']))
                before = dict(self.store.execute('SELECT * FROM nas_sync_jobs WHERE file_id=?', (source['id'],)).fetchone())
                response = self.move(source, code=f'NEW{index}', registrant='测试主体', product='TEST', module='BOM')
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.store.execute('SELECT record_id FROM files WHERE id=?', (source['id'],)).fetchone()['record_id'], source['record_id'])
                self.assertEqual(dict(self.store.execute('SELECT * FROM nas_sync_jobs WHERE file_id=?', (source['id'],)).fetchone()), before)
                self.assertIsNone(self.store.execute('SELECT id FROM records WHERE business_code=?', (f'NEW{index}',)).fetchone())


if __name__ == '__main__':
    unittest.main()
