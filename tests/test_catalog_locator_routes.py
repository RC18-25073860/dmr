import sys
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).parent))
from test_manual_catalog_updates import ManualCatalogUpdateTests
from test_feishu_catalog import SHEET_TOKEN

class LocatorRouteTests(ManualCatalogUpdateTests):
    def setUp(self):
        super().setUp()
        self.h.app.state.catalog_refresh = Mock()
        self.h.app.state.catalog_refresh.ensure_fresh.return_value = {'status':'success','running':False}

    def test_stale_directory_never_returns_cached_coordinates(self):
        self.h.app.state.catalog_refresh.ensure_fresh.return_value = {'status':'failed','running':False}
        with patch('backend.catalog_locator.read_live_location') as read:
            result=self.admin.post('/api/catalog-locate',json={'document_token':SHEET_TOKEN,'code':'11W2002C12B'})
        self.assertEqual(result.status_code,503)
        read.assert_not_called()

    def test_scan_list_refresh_requires_current_directory(self):
        self.assertEqual(self.admin.post('/api/catalog-updates/refresh-list',json={}).status_code,200)
        self.h.app.state.catalog_refresh.ensure_fresh.return_value = {'status':'running','running':True}
        self.assertEqual(self.admin.post('/api/catalog-updates/refresh-list',json={}).status_code,503)

    def test_live_locate_known_doc_only_and_read_only(self):
        before=self.h.store.execute('SELECT COUNT(*) AS n FROM logs').fetchone()['n']
        with patch('backend.catalog_locator.read_live_location', return_value={'status':'located','location':{'version_cell':'E38'}}) as read:
            response=self.admin.post('/api/catalog-locate',json={'document_token':SHEET_TOKEN,'code':'11W2002C12B'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['location']['version_cell'],'E38')
            self.assertEqual(self.admin.post('/api/catalog-locate',json={'document_token':'not-allowed','code':'11W2002C12B'}).status_code,400)
            self.assertEqual(read.call_count,1)
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM logs').fetchone()['n'],before)

    def test_notice_locate_stale_revision(self):
        self.enqueue();item=self.items()[0]
        with patch('backend.catalog_locator.read_live_location') as read:
            response=self.admin.post('/api/catalog-locate',json={'document_token':SHEET_TOKEN,'code':item['code'],'notice_id':item['id'],'revision':999})
            self.assertEqual(response.status_code,409)
            read.assert_not_called()

    def test_pending_notice_locates_without_writing_cloud(self):
        self.enqueue(); item = self.items()[0]
        with patch('backend.catalog_locator.read_live_location', return_value={'status':'located','location':{'version_cell':'E38'}}) as read:
            response = self.admin.post('/api/catalog-locate', json={
                'document_token': SHEET_TOKEN, 'code': item['code'],
                'notice_id': item['id'], 'revision': item['revision'],
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['location']['version_cell'], 'E38')
        self.assertEqual(response.json()['proposed_version'], item['new_version'])
        read.assert_called_once()
        self.assertEqual(self.items()[0]['status'], 'pending')

    def test_cloud_newer_version_is_reported(self):
        with patch('backend.catalog_locator.read_live_location', return_value={'status':'located','location':{'current_version':'20260920'}}):
            response=self.admin.post('/api/catalog-locate',json={'document_token':SHEET_TOKEN,'code':'11SG104C03B','proposed_version':'20260907'})
            self.assertEqual(response.status_code,200)
            self.assertLess(response.json()['version_comparison'],0)

    def test_name_only_request_passes_filename_and_module(self):
        with patch('backend.catalog_locator.read_live_location', return_value={'status':'not_found'}) as read:
            response=self.admin.post('/api/catalog-locate',json={'document_token':SHEET_TOKEN,'filename':'UI20S.hex','module':'BOM'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(read.call_args.kwargs['filename'],'UI20S.hex')
            self.assertEqual(read.call_args.kwargs['module'],'BOM')
            self.assertEqual(self.admin.post('/api/catalog-locate',json={'document_token':SHEET_TOKEN}).status_code,400)
