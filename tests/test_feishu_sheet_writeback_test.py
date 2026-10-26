import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import importlib.util
if importlib.util.find_spec("backend.feishu_sheet_writeback_test") is None:
    raise unittest.SkipTest("旧云端写入试验模块已移除；现行只读及人工提醒由其他路由测试覆盖")

from backend.feishu_sheet_writeback_test import FeishuSheetWritebackTest, locate, normalized


def fixture():
    cells = [[{} for _ in range(20)] for _ in range(200)]
    cells[1][0]['value'] = '广西由莱-UI04A/B/C-广西国内医疗版'
    cells[4][2]['value'] = 'UI04（A1）'
    cells[7][3]['value'] = '编号'
    cells[7][4]['value'] = '版本'
    cells[36][0]['value'] = 'BOM'
    cells[37][3]['value'] = '11UI04AA01'
    cells[37][4] = {'value': '20260410', 'cell_styles': {'font_size': 13}, 'unknown_future_prop': 'retain'}
    return dict(cells=cells, workbook={'revision': 3}, structure={'revision': 3, 'merged_cells': ['B38:C38']}, images={'revision': 3, 'sheets': []})


class Store:
    lock = threading.RLock()
    def __init__(self):
        self.conn = self.lock
    def add_log(self, *args, **kwargs):
        pass


class FakeWriter(FeishuSheetWritebackTest):
    def __init__(self, directory):
        super().__init__(Store(), directory)
        self.initial = fixture()
        self.remote = copy.deepcopy(self.initial)
        self.current = {'file_id': 'f1', 'version': '20260924'}
        self.writes = []
        self.corrupt_after = False
        self.race = False
    def _baseline(self):
        return copy.deepcopy(self.initial)
    def _current(self, result):
        return self.current
    def _read(self, label):
        if label == 'preflight' and self.race:
            self.remote['cells'][12][0]['value'] = 'human edit'
        return copy.deepcopy(self.remote)
    def _command(self, kind, **kwargs):
        assert kind == '+cells-set'
        self.writes.append(kwargs)
        self.remote['cells'][kwargs['row']-1][4]['value'] = kwargs['version']
        if self.corrupt_after:
            self.remote['cells'][1][0]['value'] = 'unexpected'


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'DMR_FEISHU_SHEET_TEST': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.writer = FakeWriter(self.temp.name)
    def sync(self):
        return self.writer.sync_commit({'results': []}, {'display_name': 'test'})
    def test_only_single_value_changed_and_idempotent(self):
        before = normalized(self.writer.remote)
        self.assertEqual(self.sync()['status'], 'synced')
        desired = copy.deepcopy(before)
        desired['cells'][37][4]['value'] = '20260924'
        self.assertEqual(normalized(self.writer.remote), desired)
        self.assertEqual(self.writer.writes, [{'row': 38, 'version': '20260924'}])
        self.assertEqual(self.sync()['status'], 'synced')
        self.assertEqual(len(self.writer.writes), 1)
    def test_equal_initial_is_noop(self):
        self.writer.current['version'] = '20260410'
        self.assertEqual(self.sync()['status'], 'synced')
        self.assertEqual(self.writer.writes, [])
    def test_no_match_or_duplicate_stops(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                self.writer.remote = copy.deepcopy(self.writer.initial)
                if duplicate:
                    self.writer.remote['cells'][40][3]['value'] = '11UI04AA01'
                else:
                    self.writer.remote['cells'][37][3]['value'] = 'other'
                self.assertEqual(self.sync()['status'], 'error')
        self.assertEqual(self.writer.writes, [])
    def test_stale_any_cell_stops(self):
        self.writer.remote['cells'][100][2]['value'] = 'other edit'
        self.assertEqual(self.sync()['status'], 'error')
        self.assertFalse(self.writer.writes)
    def test_downgrade_stops(self):
        self.writer.current['version'] = '20250101'
        self.assertEqual(self.sync()['status'], 'error')
        self.assertFalse(self.writer.writes)
    def test_non_target_skips(self):
        self.writer.current = None
        self.assertEqual(self.sync()['status'], 'skipped')
        self.assertFalse(self.writer.writes)
    def test_preflight_concurrent_edit_stops(self):
        self.writer.race = True
        self.assertEqual(self.sync()['status'], 'error')
        self.assertFalse(self.writer.writes)
    def test_unexpected_after_change_not_reverted(self):
        self.writer.corrupt_after = True
        self.assertEqual(self.sync()['status'], 'error')
        self.assertEqual(self.writer.remote['cells'][1][0]['value'], 'unexpected')
        self.assertEqual(self.sync()['status'], 'error')
        self.assertEqual(len(self.writer.writes), 1)
    def test_recover_uncertain_success_without_second_write(self):
        original = self.writer._command
        def uncertain(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('timeout after success')
        self.writer._command = uncertain
        self.assertEqual(self.sync()['status'], 'pending')
        self.assertEqual(self.sync()['status'], 'synced')
        self.assertEqual(len(self.writer.writes), 1)
    def test_location_is_fresh_not_fixed_row(self):
        remote = fixture()
        remote['cells'][38] = remote['cells'][37]
        remote['cells'][37] = [{} for _ in range(20)]
        self.assertEqual(locate(remote), 38)
        self.writer.remote = remote
        self.assertEqual(self.sync()['status'], 'error')  # structural relocation requires rebaseline
        self.assertFalse(self.writer.writes)
    def test_authoritative_store_filters_subject_product_category_and_current_file(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript("CREATE TABLE records(id, business_code, registrant, product, module, project_category); CREATE TABLE files(id, record_id, version, status);")
        conn.execute("INSERT INTO records VALUES ('r','11UI04AA01','广西由莱','UI04 A1','BOM','医疗')")
        conn.execute("INSERT INTO files VALUES ('f','r','20260924','当前有效')")
        store = Store()
        store.execute = conn.execute
        writer = FeishuSheetWritebackTest(store, self.temp.name)
        result = {'results': [{'file_id': 'f', 'status': 'committed'}]}
        self.assertEqual(writer._current(result), {'file_id': 'f', 'version': '20260924'})
        conn.execute("UPDATE records SET registrant='other'")
        self.assertIsNone(writer._current(result))
        conn.execute("UPDATE records SET registrant='广西由莱',project_category='家电'")
        self.assertIsNone(writer._current(result))
        conn.execute("UPDATE records SET project_category='医疗'")
        conn.execute("UPDATE files SET status='历史版本'")
        conn.execute("INSERT INTO files VALUES ('f2','r','20260925','当前有效')")
        with self.assertRaises(ValueError):
            writer._current(result)

    def test_command_rejects_other_operation(self):
        with self.assertRaises(ValueError):
            FeishuSheetWritebackTest._command(self.writer, '+cells-clear')
    def test_disabled(self):
        with patch.dict(os.environ, {'DMR_FEISHU_SHEET_TEST': '0'}):
            self.assertEqual(self.sync()['status'], 'disabled')
        self.assertFalse(self.writer.writes)

if __name__ == '__main__':
    unittest.main()
