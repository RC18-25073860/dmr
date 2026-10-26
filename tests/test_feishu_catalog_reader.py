"""Offline tests for the cloud read-only snapshot publisher."""
import importlib.util
from pathlib import Path
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
spec=importlib.util.spec_from_file_location('reader',Path(__file__).resolve().parents[1]/'scripts/read_feishu_catalog.py')
reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)

class ReaderTests(unittest.TestCase):
    def test_write_commands_are_rejected_before_subprocess(self):
        with patch.object(reader.subprocess,'run') as run:
            for args in [('sheets','+cells-set'),('drive','+upload'),('drive','files','patch')]:
                with self.assertRaises(ValueError):reader.call(*args)
            run.assert_not_called()

    def test_all_physical_rows_and_columns_preserved(self):
        doc={'token':'source','name':'test'}
        meta={'revision':4,'title':'test','sheets':[{'sheet_id':'tab','sheet_name':'原清单','row_count':102,'column_count':2}]}
        ranges=[]
        def fake(*args):
            if args[1]=='+workbook-info':return meta
            area=args[args.index('--range')+1];ranges.append(area)
            start=1 if area=='A1:B100' else 101;end=100 if start==1 else 102
            return {'revision':4,'has_more':False,'ranges':[{'range':area,'actual_range':area,'row_indices':list(range(start,end+1)),'col_indices':['A','B'],'cells':[[{}, {'value':'末尾内容' if i==102 else ''}] for i in range(start,end+1)],'truncated':False}]}
        with patch.object(reader,'call',side_effect=fake):result=reader.read_document(doc)
        self.assertEqual(ranges,['A1:B100','A101:B102'])
        self.assertEqual(result['sheets'][0]['ranges'][1]['row_indices'],[101,102])
        self.assertEqual(result['sheets'][0]['ranges'][1]['cells'][-1][1]['value'],'末尾内容')

    def test_incomplete_or_changed_read_is_rejected(self):
        meta={'revision':1,'sheets':[{'sheet_id':'tab','sheet_name':'S','row_count':1,'column_count':1}]}
        for data in [{'has_more':True}, {'has_more':False,'revision':2,'ranges':[{}]}, {'has_more':False,'revision':1,'ranges':[{'truncated':True}]}]:
            with self.subTest(data=data),patch.object(reader,'call',side_effect=[meta,data]):
                with self.assertRaises(RuntimeError):reader.read_document({'token':'doc','name':'Test'})


    def test_directory_only_preserves_cached_sheets_without_cell_reads(self):
        from backend import catalog_refresh
        from test_feishu_catalog import PROJECT_TOKEN, SHEET_TOKEN, sample_snapshot

        product_token = 'ProductFolderToken001'
        new_sheet_token = 'NewSheetToken000000001'
        old = sample_snapshot()
        old['documents'][0]['parent_token'] = product_token
        old['folders'].append({'token': product_token, 'parent_token': PROJECT_TOKEN, 'name': '旧产品', 'path': ['DMR清单', '2.家电项目', '旧产品']})
        tree = {
            catalog_refresh.ROOT_TOKEN: [{'token': PROJECT_TOKEN, 'name': '1.医疗项目', 'type': 'folder'}],
            PROJECT_TOKEN: [{'token': product_token, 'name': 'UI04（A1）', 'type': 'folder'}],
            product_token: [
                {'token': SHEET_TOKEN, 'name': '广西由莱-DMR清单-UI04（A1）', 'type': 'sheet'},
                {'token': new_sheet_token, 'name': '深圳由莱-DMR清单-UI04（A1）', 'type': 'sheet'},
            ],
        }

        def fake_children(self, folder, *, deadline_at=None):
            return tree.get(folder['token'], [])

        with tempfile.TemporaryDirectory() as tmp, patch.object(catalog_refresh.LarkCatalogDirectoryReader, 'folder_children', fake_children), patch.object(reader.subprocess, 'run') as run:
            output = Path(tmp) / 'feishu_catalog.json'
            output.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
            reader.publish_directory(SimpleNamespace(output=output))
            saved = json.loads(output.read_text(encoding='utf-8'))

        run.assert_not_called()
        docs = {doc['token']: doc for doc in saved['documents']}
        self.assertEqual(docs[SHEET_TOKEN]['sheets'][0]['sheet_name'], '清单')
        self.assertEqual(docs[new_sheet_token]['sheets'], [])

    def test_pagination_cycle_is_rejected(self):
        with patch.object(reader,'call',return_value={'files':[],'has_more':True,'next_page_token':'same'}):
            with self.assertRaises(RuntimeError):reader.folder_children({'token':'folder'})

if __name__=='__main__':unittest.main()
