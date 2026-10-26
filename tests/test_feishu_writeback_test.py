import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import importlib.util
if importlib.util.find_spec("backend.feishu_writeback_test") is None:
    raise unittest.SkipTest("旧云端写入试验模块已移除；现行只读及人工提醒由其他路由测试覆盖")

from backend.feishu_writeback_test import FeishuWritebackTest, TARGET_TOKEN, TEST_CODE, BEGIN, END, digest


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''CREATE TABLE records(id,business_code,registrant,product,module);
            CREATE TABLE files(id,record_id,version,name,sha256,status);
            INSERT INTO records VALUES('r','DMR-BOM-WRITEBACK-TEST','深圳金茉','测试产品','BOM');
            INSERT INTO files VALUES('f1','r','V1.0','test-v1.xlsx','sha1','当前有效');''')

    def add_log(self, *args, **kwargs):
        pass

    def execute(self, *args):
        return self.conn.execute(*args)


class FakeWriter(FeishuWritebackTest):
    def __init__(self, store, root):
        super().__init__(store, root)
        self.remote = b'Existing original text\n'
        self.calls = []
        self.fail_after_upload = False
        self.mutate_preflight = False

    def _run(self, args):
        self.calls.append(args)
        assert args[args.index('--file-token') + 1] == TARGET_TOKEN
        if args[0] == '+download':
            filename = args[args.index('--output') + 1]
            if filename == 'preflight.txt' and self.mutate_preflight:
                self.remote += b'Human edit'
            (self.directory / filename).write_bytes(self.remote)
        elif args[0] == '+upload':
            self.remote = (self.directory / 'desired.txt').read_bytes()
            if self.fail_after_upload:
                self.fail_after_upload = False
                raise TimeoutError('upload succeeded but acknowledgement lost')
        else:
            raise AssertionError(args)
        return {}


class WritebackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store()
        self.writer = FakeWriter(self.store, self.root)
        (self.root / 'feishu_writeback_test_baseline.txt').write_bytes(self.writer.remote)
        self.env = patch.dict(os.environ, {'DMR_FEISHU_TEST_WRITEBACK': '1'})
        self.env.start()
        self.result = {'results': [{'status': 'committed', 'file_id': 'f1', 'record_id': 'r'}]}

    def tearDown(self):
        self.env.stop()
        self.store.conn.close()
        self.tmp.cleanup()

    def sync(self):
        return self.writer.sync_commit(self.result, {'id': 'admin'})

    def uploads(self):
        return len([call for call in self.writer.calls if call[0] == '+upload'])

    def test_disabled_and_non_target_never_call_cli(self):
        with patch.dict(os.environ, {'DMR_FEISHU_TEST_WRITEBACK': '0'}):
            self.assertEqual(self.sync()['status'], 'disabled')
        self.store.execute('UPDATE records SET business_code=?', ('REAL-BOM',))
        self.assertEqual(self.sync()['status'], 'skipped')
        self.assertFalse(self.writer.calls)

    def test_v1_v2_preserve_original_and_single_block_idempotent(self):
        original = self.writer.remote
        self.assertEqual(self.sync()['status'], 'synced')
        self.assertTrue(self.writer.remote.startswith(original))
        self.assertEqual(self.sync()['status'], 'synced')
        self.assertEqual(self.uploads(), 1)
        self.store.execute("UPDATE files SET status='历史版本'")
        self.store.execute("INSERT INTO files VALUES('f2','r','V2.0','test-v2.xlsx','sha2','当前有效')")
        self.result['results'][0]['file_id'] = 'f2'
        self.assertEqual(self.sync()['status'], 'synced')
        text = self.writer.remote.decode()
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)
        self.assertIn('V2.0', text)
        self.assertNotIn('V1.0', text)
        self.assertTrue(self.writer.remote.startswith(original))
        self.assertEqual(self.uploads(), 2)
        self.assertTrue(list(self.writer.directory.glob('backup-*.txt')))
        state = json.loads((self.writer.directory / 'state.json').read_text())
        self.assertEqual(state['expected_sha256'], digest(self.writer.remote))

    def test_external_edit_blocks_and_preserves_remote(self):
        self.writer.remote += b'External edit'
        self.assertEqual(self.sync()['status'], 'error')
        self.assertEqual(self.uploads(), 0)
        self.assertTrue(self.writer.remote.endswith(b'External edit'))

    def test_preflight_edit_blocks(self):
        self.writer.mutate_preflight = True
        self.assertEqual(self.sync()['status'], 'error')
        self.assertEqual(self.uploads(), 0)

    def test_uncertain_success_recovers_without_duplicate_upload(self):
        self.writer.fail_after_upload = True
        self.assertEqual(self.sync()['status'], 'pending')
        self.assertEqual(self.sync()['status'], 'synced')
        self.assertEqual(self.uploads(), 1)

    def test_failed_upload_can_retry_when_remote_unchanged(self):
        with patch.object(self.writer, '_upload', side_effect=TimeoutError):
            self.assertEqual(self.sync()['status'], 'pending')
        self.assertEqual(self.sync()['status'], 'synced')
        self.assertEqual(self.uploads(), 1)

    def test_missing_baseline_and_ambiguous_record_fail_closed(self):
        (self.root / 'feishu_writeback_test_baseline.txt').unlink()
        self.assertEqual(self.sync()['status'], 'error')
        self.assertFalse(self.writer.calls)
        self.store.execute("INSERT INTO files VALUES('f2','r','V2','a.xlsx','sha2','当前有效')")
        self.assertEqual(self.sync()['status'], 'error')
        self.assertFalse(self.writer.calls)

    def test_skipped_commit_does_not_trigger(self):
        self.result['results'][0]['status'] = 'skipped'
        self.assertEqual(self.sync()['status'], 'skipped')
        self.assertFalse(self.writer.calls)


if __name__ == '__main__':
    unittest.main()
