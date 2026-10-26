"""A transfer form archives by the date in its filename, without creating DMR rows."""
import base64
import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backend.transfer_manifest import destination_for
from test_mail_ingest import MailIngestHarness


def transfer_workbook():
    buf = io.BytesIO()
    cells = [
        '<row r="2"><c r="A2" t="inlineStr"><is><t>DMR文件转移清单</t></is></c></row>',
        '<row r="9">' + ''.join(
            f'<c r="{col}9" t="inlineStr"><is><t>{name}</t></is></c>'
            for col, name in [('A', '序号'), ('B', '文件名称'), ('C', '编号'), ('D', '版本')]
        ) + '</row>',
    ]
    with zipfile.ZipFile(buf, 'w') as book:
        book.writestr('xl/workbook.xml', '<workbook/>')
        book.writestr('xl/worksheets/sheet1.xml',
                      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                      + ''.join(cells) + '</sheetData></worksheet>')
    return buf.getvalue()


class TransferManifestTests(unittest.TestCase):
    def setUp(self):
        self.h = MailIngestHarness()
        self.admin, _ = self.h.setup_admin()
        self.token = self.h.create_mail_client(self.admin)['bearer_token']
        self.client = self.h.bearer_client(self.token)
        self.raw = transfer_workbook()
        self.filename = 'DMR文件转移清单（BW-2026100801-深圳由莱）.xlsx'

    def tearDown(self):
        self.h.close()

    def payload(self, filename=None, message_id='message-1'):
        return {
            'mailbox': 'dcc@example.test', 'message_id': message_id,
            'subject': '归档转移清单', 'sender': 'dcc@example.test',
            'received_at': '2026-10-10T09:00:00+08:00',
            'attachment': {
                'attachment_id': 'attachment-1', 'filename': filename or self.filename,
                'content_base64': base64.b64encode(self.raw).decode(), 'size': len(self.raw),
                'content_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'sha256': hashlib.sha256(self.raw).hexdigest(),
            },
        }

    def test_browser_upload_archives_without_controlled_file_fields(self):
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            body = {'name': self.filename, 'data': base64.b64encode(self.raw).decode()}
            with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': mount}):
                first = self.admin.post('/api/transfer-manifests/upload', json=body)
                retry = self.admin.post('/api/transfer-manifests/upload', json=body)
            self.assertEqual(first.status_code, 201, first.text)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(first.json()['receipt_id'], retry.json()['receipt_id'])
            self.assertEqual(first.json()['archive_status'], 'archived')
            self.assertFalse(first.json().get('already_exists', False))
            self.assertTrue(retry.json()['already_exists'])
            self.assertEqual((Path(mount) / 'Y-2026' / 'DMR-2026-10' / self.filename).read_bytes(), self.raw)
            self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM transfer_manifests').fetchone()['n'], 1)
            self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n'], 0)
            self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM batch_items').fetchone()['n'], 0)

    def test_browser_upload_same_name_same_content_prompts_review_without_writing(self):
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            target = Path(mount) / 'Y-2026' / 'DMR-2026-10' / self.filename
            target.parent.mkdir(parents=True)
            target.write_bytes(self.raw)
            body = {'name': self.filename, 'data': base64.b64encode(self.raw).decode()}
            with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': mount}):
                response = self.admin.post('/api/transfer-manifests/upload', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['already_exists'])
            self.assertIn(self.filename, response.json()['network_path'])
            self.assertEqual(target.read_bytes(), self.raw)

    def test_browser_upload_same_name_different_content_needs_review(self):
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            target = Path(mount) / 'Y-2026' / 'DMR-2026-10' / self.filename
            target.parent.mkdir(parents=True)
            target.write_bytes(b'existing original')
            body = {'name': self.filename, 'data': base64.b64encode(self.raw).decode()}
            with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': mount}):
                response = self.admin.post('/api/transfer-manifests/upload', json=body)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['archive_status'], 'needs_review')
            self.assertIn(self.filename, response.json()['network_path'])
            self.assertEqual(target.read_bytes(), b'existing original')
            row = self.h.store.execute('SELECT status,blob_path FROM transfer_manifests').fetchone()
            self.assertEqual(row['status'], 'needs_review')
            self.assertTrue((self.h.store.data_dir / row['blob_path']).is_file())

    def test_standard_analyze_rejects_transfer_manifest(self):
        body = {'files': [{'name': self.filename, 'data': base64.b64encode(self.raw).decode()}]}
        response = self.admin.post('/api/analyze', json=body)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM batch_items').fetchone()['n'], 0)

    def test_filename_date_and_invalid_date(self):
        date, target = destination_for(self.filename)
        self.assertEqual(date, '2026-10-08')
        self.assertEqual(target, 'Y-2026/DMR-2026-10/' + self.filename)
        legacy = 'DMR文件转移清单（LQyn2026081901-深圳由莱） .xlsx'
        self.assertEqual(destination_for(legacy), ('2026-08-19', 'Y-2026/DMR-2026-08/' + legacy))
        self.assertEqual(destination_for('DMR文件转移清单（CMJ-2023073101）.xlsx')[0], '2023-07-31')
        self.assertEqual(destination_for('DMR文件转移清单(ZY-2024012201-深圳金茉).xlsx')[0], '2024-01-22')
        self.assertEqual(destination_for('ECN-2024060007：DMR文件转移清单（CMJ-2024061102-深圳由莱）.xlsx')[0], '2024-06-11')
        response = self.client.post('/api/integrations/mail/transfer-manifests',
                                    json=self.payload(filename='DMR文件转移清单（BW-2026023001-深圳由莱）.xlsx'))
        self.assertEqual(response.status_code, 422, response.text)

    def test_platform_receipt_retries_until_192_archive_succeeds(self):
        with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': ''}):
            first = self.client.post('/api/integrations/mail/transfer-manifests', json=self.payload())
        self.assertEqual(first.status_code, 503, first.text)
        row = self.h.store.execute('SELECT * FROM transfer_manifests').fetchone()
        self.assertEqual(row['status'], 'pending')
        self.assertTrue((self.h.store.data_dir / row['blob_path']).is_file())
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': mount}):
                second = self.client.post('/api/integrations/mail/transfer-manifests', json=self.payload())
                again = self.client.post('/api/integrations/mail/transfer-manifests', json=self.payload())
            self.assertEqual(second.status_code, 201, second.text)
            self.assertEqual(again.status_code, 200, again.text)
            network_root = r'\\nas.example.local\文档中心\DMR转移清单'
            self.assertEqual(second.json()['network_path'], network_root + '\\Y-2026\\DMR-2026-10\\' + self.filename)
            target = Path(mount) / 'Y-2026' / 'DMR-2026-10' / self.filename
            self.assertEqual(target.read_bytes(), self.raw)
            self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM transfer_manifests').fetchone()['n'], 1)
            self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n'], 0)

    def test_mail_can_stage_transfer_manifest_without_touching_192(self):
        payload = self.payload()
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': mount}):
                first = self.client.post('/api/integrations/mail/transfer-manifests?stage_only=true', json=payload)
                repeat = self.client.post('/api/integrations/mail/transfer-manifests?stage_only=true', json=payload)
            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(repeat.status_code, 202, repeat.text)
            self.assertEqual(first.json()['receipt_id'], repeat.json()['receipt_id'])
            self.assertEqual(first.json()['archive_status'], 'staged')
            self.assertTrue(first.json()['staged_only'])
            self.assertEqual(list(Path(mount).iterdir()), [])
            self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n'], 0)

    def test_same_name_different_bytes_never_overwrites(self):
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            target = Path(mount) / 'Y-2026' / 'DMR-2026-10' / self.filename
            target.parent.mkdir(parents=True)
            target.write_bytes(b'other original')
            with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': mount}):
                response = self.client.post('/api/integrations/mail/transfer-manifests', json=self.payload())
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['archive_status'], 'needs_review')
            self.assertEqual(target.read_bytes(), b'other original')

    def test_host_local_path_file_works_without_environment_variable(self):
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            config_file = self.h.data_dir / 'transfer-archive-root.txt'
            config_file.write_text(mount, encoding='utf-8')
            with patch('backend.transfer_manifest.ARCHIVE_ROOT_FILE', config_file), patch.dict(os.environ, {}, clear=True):
                response = self.client.post('/api/integrations/mail/transfer-manifests', json=self.payload())
            self.assertEqual(response.status_code, 201, response.text)
            self.assertTrue((Path(mount) / 'Y-2026' / 'DMR-2026-10' / self.filename).is_file())

    def test_xlsx_without_controlled_file_fields_archives_unchanged(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as book:
            book.writestr('xl/workbook.xml', '<workbook/>')
            book.writestr('xl/worksheets/sheet2.xml', '<worksheet/>')
        self.raw = buf.getvalue()
        with tempfile.TemporaryDirectory(dir=self.h.data_dir) as mount:
            with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': mount}):
                response = self.client.post('/api/integrations/mail/transfer-manifests', json=self.payload())
            self.assertEqual(response.status_code, 201, response.text)
            target = Path(mount) / 'Y-2026' / 'DMR-2026-10' / self.filename
            self.assertEqual(target.read_bytes(), self.raw)
            self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n'], 0)

    def test_auditor_can_see_pending_archive_without_creating_controlled_record(self):
        with patch.dict(os.environ, {'DMR_TRANSFER_ARCHIVE_MOUNT_ROOT': ''}):
            self.assertEqual(self.client.post('/api/integrations/mail/transfer-manifests', json=self.payload()).status_code, 503)
        listing = self.admin.get('/api/integrations/mail/transfer-manifests')
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()['items'][0]['status'], 'pending')
        self.assertIn(r'\\nas.example.local\文档中心', listing.json()['items'][0]['network_path'])
        self.assertIn('未配置', listing.json()['items'][0]['last_error'])
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n'], 0)


if __name__ == '__main__':
    unittest.main()
