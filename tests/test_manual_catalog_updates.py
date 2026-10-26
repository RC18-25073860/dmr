import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_feishu_catalog import FeishuCatalogHarness, sample_snapshot, SHEET_TOKEN
from backend.access import AccessPolicy
from backend.manual_catalog_updates import ManualCatalogUpdates, _product_matches


class ManualCatalogUpdateTests(unittest.TestCase):
    def setUp(self):
        self.h = FeishuCatalogHarness()
        self.admin = self.h.setup_admin()
        snapshot = sample_snapshot()
        snapshot['documents'][0]['name'] = '深圳金荣-W20 DMR清单'
        snapshot['documents'][0]['path'][-1] = snapshot['documents'][0]['name']
        block = snapshot['documents'][0]['sheets'][0]['ranges'][0]
        block['row_indices'] = [1, 2, 3]
        block['cells'].insert(1, ['BOM', '', ''])
        snapshot['documents'][0]['sheets'][0]['row_count'] = 3
        self.h.write_snapshot(snapshot)
        self.service = ManualCatalogUpdates(self.h.store, self.h.config)
        self.h.store.execute("INSERT INTO records(id,business_code,name,registrant,type,project_category,product,module,version,previous) VALUES('r1','11W2002C12B','测试','深圳金荣','成品','家电','W20','BOM','20260924','20260410')")
        self.add_file('f1', '20260924')

    def tearDown(self):
        self.h.close()

    def add_file(self, file_id, version):
        self.h.store.execute("INSERT INTO files(id,record_id,name,version,status,sha256,blob_path) VALUES(?,'r1','BOM.xlsx',?,'当前有效','test','files/test')", (file_id, version))

    def enqueue(self, file_id='f1', status='committed'):
        with self.h.store.conn:
            return self.service.enqueue_commit({'batch_id': 'B1', 'results': [{'status': status, 'file_id': file_id}]}, {'display_name': '文控'})

    def items(self):
        response = self.admin.get('/api/catalog-updates')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['items']

    def test_no_backfill_duplicate_or_historical(self):
        self.assertEqual(self.items(), [])
        self.assertEqual(self.enqueue(status='skipped'), [])
        self.h.store.execute("UPDATE files SET status='历史版本' WHERE id='f1'")
        self.assertEqual(self.enqueue(), [])
        self.h.store.execute("UPDATE files SET status='当前有效' WHERE id='f1'")
        self.h.store.execute("UPDATE records SET previous='20260410' WHERE id='r1'")
        self.assertEqual(len(self.enqueue()), 1)
        self.assertEqual(self.enqueue(), [])
        self.assertEqual(len(self.items()), 1)

    def test_match_and_manual_resolution_is_audited(self):
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['match_status'], 'matched')
        self.assertEqual(item['document_token'], SHEET_TOKEN)
        response = self.admin.post('/api/catalog-updates/' + item['id'] + '/resolve', json={'revision': item['revision'], 'note': '人工核对完成'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['manual_confirmation'])
        self.assertEqual(self.items()[0]['status'], 'processed')
        self.assertEqual(self.admin.post('/api/catalog-updates/' + item['id'] + '/resolve', json={'revision': item['revision']}).status_code, 409)
        log = self.h.store.execute("SELECT * FROM logs WHERE action='清单人工确认'").fetchone()
        self.assertIn('系统未验证飞书内容', log['change'])
        self.assertIn('管理员', log['user'])

    def test_coalesce_and_stale_confirmation(self):
        self.enqueue()
        old = self.items()[0]
        self.h.store.execute("UPDATE files SET status='历史版本' WHERE id='f1'")
        self.h.store.execute("UPDATE records SET version='20260925',previous='20260924' WHERE id='r1'")
        self.add_file('f2', '20260925')
        self.enqueue('f2')
        current = self.items()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['old_version'], '20260410')
        self.assertEqual(current[0]['new_version'], '20260925')
        self.assertEqual(current[0]['revision'], 2)
        self.assertEqual(self.admin.post('/api/catalog-updates/' + old['id'] + '/resolve', json={'revision': 1}).status_code, 409)

    def test_matching_document_is_linked_without_matching_entry(self):
        self.h.store.execute("UPDATE records SET business_code='NOT-IN-SHEET' WHERE id='r1'")
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['match_status'], 'matched')
        self.assertEqual(item['document_token'], SHEET_TOKEN)
        self.assertTrue(item['document_url'])

    def test_unknown_subject_shows_model_candidates_without_auto_association(self):
        import copy
        docs = self.service.documents()
        second = copy.deepcopy(docs[0])
        second['token'] = 'OtherSubjectSheet'
        second['name'] = '广西由莱-W20 DMR清单'
        second['path'] = [second['name']]
        for subject in ('', '待确认'):
            record = {'registrant': subject, 'product': 'W20'}
            with patch.object(self.service, 'documents', return_value=docs + [second]):
                result = self.service.match(record)
            self.assertEqual(len(result['candidate_tokens']), 2)
            self.assertEqual(result['match_status'], 'ambiguous')
            self.assertEqual(result['document_token'], '')
            with patch.object(self.service, 'documents', return_value=docs):
                self.assertEqual(self.service.match(record)['match_status'], 'ambiguous')
        with patch.object(self.service, 'documents', return_value=docs + [second]):
            result = self.service.match({'registrant': '广西由莱', 'product': 'W20'})
            self.assertEqual(result['document_token'], 'OtherSubjectSheet')
            self.assertEqual(self.service.match({'registrant': '', 'product': 'UI04'})['candidate_tokens'], [])

    def test_other_subjects_remain_candidates_but_not_auto_targets(self):
        import copy
        docs = self.service.documents()
        other = copy.deepcopy(docs[0])
        other.update(token='SharedOther', name='广西由莱-W20清单', path=['广西由莱-W20清单'])
        unrelated = copy.deepcopy(other)
        unrelated.update(token='OtherModel', name='广西由莱-UI04清单', path=['广西由莱-UI04清单'])
        result = self.service.match({'registrant': '深圳金荣', 'product': 'W20'}, documents=[other, unrelated] + docs)
        self.assertEqual(result['document_token'], SHEET_TOKEN)
        self.assertEqual(result['candidate_tokens'], [SHEET_TOKEN, 'SharedOther'])
        self.assertEqual(result['shared_candidate_tokens'], ['SharedOther'])
        self.assertEqual([t['document_token'] for t in result['targets']], [SHEET_TOKEN])
        result = self.service.match({'registrant': '深圳金荣', 'product': 'W20'}, documents=[other])
        self.assertEqual(result['match_status'], 'ambiguous')
        self.assertFalse(result.get('targets'))

    def test_other_region_same_company_family_is_shared_candidate_not_auto_match(self):
        docs = self.service.documents()
        other = copy.deepcopy(docs[0])
        other.update(token='GuangxiSheet', name='广西金荣-W20 DMR清单', path=['DMR', '家电', '广西金荣-W20 DMR清单'])
        result = self.service.match({'registrant': '深圳金荣', 'product': 'W20', 'project_category': '家电'}, documents=[other] + docs)
        self.assertEqual(result['document_token'], SHEET_TOKEN)
        self.assertEqual(result['match_status'], 'matched')
        self.assertEqual(result['shared_candidate_tokens'], ['GuangxiSheet'])

    def test_unmatched_requires_existing_doc_and_note(self):
        self.h.store.execute("UPDATE records SET product='UI04S',business_code='UNMATCHED-001' WHERE id='r1'")
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['match_status'], 'unmatched')
        url = '/api/catalog-updates/' + item['id'] + '/resolve'
        self.assertEqual(self.admin.post(url, json={'revision': 1}).status_code, 400)
        self.assertEqual(self.admin.post(url, json={'revision': 1, 'document_token': SHEET_TOKEN}).status_code, 400)
        self.assertEqual(self.admin.post(url, json={'revision': 1, 'document_token': SHEET_TOKEN, 'note': '已人工确认对应关系及完成清单修改'}).status_code, 200)
        self.assertFalse(_product_matches('UI04', '深圳由莱 UI04S'))

    def test_ambiguous_mapping_does_not_guess(self):
        import copy
        docs = self.service.documents()
        docs.append(copy.deepcopy(docs[0]))
        docs[1]['token'] = 'DifferentSheetToken123'
        with patch.object(self.service, 'documents', return_value=docs):
            self.enqueue()
        self.assertEqual(self.items()[0]['match_status'], 'ambiguous')
        self.assertEqual(self.items()[0]['document_url'], '')

    def test_permissions_and_changed_record_block(self):
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(self.h.client().get('/api/catalog-updates').status_code, 401)
        restricted = self.h.create_user(self.admin, 'restricted', [{'registrant': '深圳金荣', 'project_category': '家电', 'product': 'W20'}])
        self.assertEqual(restricted.get('/api/catalog-updates').status_code, 403)
        reader = self.h.create_user(self.admin, 'globalreader', [{'registrant': '*', 'project_category': '*', 'product': '*'}])
        self.assertFalse(reader.get('/api/catalog-updates').json()['can_manage'])
        url = '/api/catalog-updates/' + item['id'] + '/resolve'
        self.assertEqual(reader.post(url, json={'revision': 1}).status_code, 403)
        self.h.store.execute("UPDATE records SET product='UI04' WHERE id='r1'")
        self.assertEqual(self.admin.post(url, json={'revision': 1}).status_code, 409)

    def test_failed_transaction_does_not_leave_reminder(self):
        with self.assertRaises(RuntimeError):
            with self.h.store.conn.immediate():
                self.enqueue()
                raise RuntimeError('simulate archive failure')
        self.assertEqual(self.items(), [])

    def test_same_batch_keeps_pre_batch_old_version(self):
        self.h.store.execute("UPDATE files SET status='历史版本',version='20260410' WHERE id='f1'")
        self.add_file('f2', '20260924')
        self.h.store.execute("UPDATE files SET status='历史版本' WHERE id='f2'")
        self.add_file('f3', '20260925')
        self.h.store.execute("UPDATE records SET version='20260925',previous='20260924' WHERE id='r1'")
        with self.h.store.conn:
            result = self.service.enqueue_commit({'results': [{'status': 'committed', 'file_id': 'f2'}, {'status': 'committed', 'file_id': 'f3'}]}, {'display_name': '文控'})
        self.assertEqual(len(result), 1)
        self.assertEqual(self.items()[0]['old_version'], '20260410')
        self.assertEqual(self.items()[0]['new_version'], '20260925')

    def test_upgrade_0007_preserves_records_and_starts_empty(self):
        from alembic import command
        from alembic.script import ScriptDirectory
        from backend.db import _alembic_config, init_schema
        engine = self.h.store.conn.engine
        with engine.begin() as connection:
            cfg = _alembic_config(engine)
            cfg.attributes['connection'] = connection
            command.downgrade(cfg, '20260923_0007')
        init_schema(engine)
        self.assertEqual(self.h.store.execute("SELECT version FROM records WHERE id='r1'").fetchone()['version'], '20260924')
        self.assertEqual(self.items(), [])
        self.assertEqual(
            self.h.store.execute('SELECT version_num FROM alembic_version').fetchone()['version_num'],
            ScriptDirectory.from_config(cfg).get_current_head(),
        )

    def test_missing_snapshot_keeps_pending_visible(self):
        self.enqueue()
        self.service.path.unlink()
        self.assertEqual(len(self.items()), 1)
        item = self.items()[0]
        self.assertEqual(self.admin.post('/api/catalog-updates/' + item['id'] + '/resolve', json={'revision': 1}).status_code, 503)

    def test_first_archive_creates_check_even_without_match(self):
        self.h.store.execute("UPDATE records SET previous='',product='UNKNOWN',business_code='UNMATCHED-001' WHERE id='r1'")
        self.assertEqual(len(self.enqueue()), 1)
        item = self.items()[0]
        self.assertEqual(item['reason'], 'first_archive')
        self.assertEqual(item['old_version'], '')
        self.assertEqual(item['match_status'], 'unmatched')
        self.assertEqual(self.enqueue(), [])

    def test_indirect_register_notice_does_not_require_catalog_row_location(self):
        self.h.store.execute(
            "UPDATE records SET module='包装图纸',business_code='7322200005',version='V1',previous='V0',linked_bom='11W2002C12B',metadata=? WHERE id='r1'",
            (json.dumps({'kind': 'packaging_drawing', 'linked_bom': '11W2002C12B'}),),
        )
        self.h.store.execute(
            "UPDATE files SET name='7322200005-深圳Ulike-JSG10-电极头保护膜-转曲-20250908-V0.pdf',version='V1',metadata=? WHERE id='f1'",
            (json.dumps({'kind': 'packaging_drawing', 'linked_bom': '11W2002C12B'}),),
        )
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['management_target'], 'packaging_drawing_register')
        self.assertEqual(item['management_target_label'], '包装图纸台账')
        self.assertFalse(item['direct_catalog_row'])
        self.assertFalse(item['catalog_locator_enabled'])
        self.assertEqual(item['relation_candidates'][0]['code'], '11W2002C12B')
        response = self.admin.post('/api/catalog-updates/' + item['id'] + '/resolve',
                                   json={'revision': item['revision'], 'note': '已在包装图纸台账核对，无需 DMR 清单行定位'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['status'], 'processed')
        self.assertTrue(body['targets'][0]['document_token'].startswith('external:'))

    def test_legacy_notice_listing_gets_current_management_policy(self):
        self.h.store.execute(
            "INSERT INTO manual_catalog_updates(id,record_id,file_id,revision,status,payload,created_at,updated_at) VALUES('legacy','r1','f1',1,'pending',?,?,?)",
            (json.dumps({'id': 'legacy', 'record_id': 'r1', 'file_id': 'f1', 'file_name': '承认书.pdf',
                         'revision': 1, 'code': 'MAT-001', 'registrant': '深圳金荣',
                         'product': 'W20', 'module': '承认书&规格书', 'project_category': '家电',
                         'old_version': 'V0', 'new_version': 'V1', 'status': 'pending',
                         'created_at': 'now', 'updated_at': 'now'}, ensure_ascii=False), 'now', 'now'),
        )
        item = next(row for row in self.items() if row['id'] == 'legacy')
        self.assertEqual(item['management_target'], 'ack_spec_register')
        self.assertFalse(item['catalog_locator_enabled'])

    def test_software_history_notice_is_applicability_review_not_program_upgrade(self):
        self.h.store.execute("UPDATE records SET module='软件',business_code='UI20S_CH_JZ-HISTORY',version='V1.0.2.02',previous='V1.0.2.00' WHERE id='r1'")
        self.h.store.execute("UPDATE files SET name='UI20S_CH_JZ项目软件履历表.xlsx',version='V1.0.2.02',metadata=? WHERE id='f1'",
                             (json.dumps({'kind': 'software_history', 'versions': [{'linked_bom': '1120S09C02B', 'name': 'UI20S.hex'}]}),))
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['update_kind'], 'software_history_applicability_review')
        self.assertEqual(item['management_target'], 'software_release_register')
        self.assertFalse(item['catalog_locator_enabled'])
        self.assertIn('不等同于所有程序自动升级', item['management_instruction'])
        self.assertEqual(item['relation_candidates'][0]['code'], '1120S09C02B')
        self.assertEqual(item['relation_scope'], 'parsed_evidence_only')

    def test_structural_drawing_notice_is_not_direct_dmr_row(self):
        self.h.store.execute(
            "UPDATE records SET module='结构图纸',business_code='STR-001',version='V1',previous='V0',metadata=? WHERE id='r1'",
            (json.dumps({'kind': 'structural_drawing'}),),
        )
        self.h.store.execute("UPDATE files SET name='STR-001_UI20S_结构图纸_V1.pdf',version='V1',metadata=? WHERE id='f1'",
                             (json.dumps({'kind': 'structural_drawing'}),))
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['management_target'], 'structural_drawing_register')
        self.assertFalse(item['direct_dmr'])
        self.assertIn('结构图纸台账', item['catalog_location_hint'])

    def test_specific_management_kind_beats_generic_bom_module_in_notice(self):
        self.h.store.execute(
            "UPDATE records SET module='BOM',business_code='7322200005',version='V1',previous='V0',metadata=? WHERE id='r1'",
            (json.dumps({'kind': 'bom', 'management_kind': 'packaging_drawing', 'identifiers': {'material_no': '7322200005'}}),),
        )
        self.h.store.execute(
            "UPDATE files SET name='7322200005-包装图纸.pdf',version='V1',metadata=? WHERE id='f1'",
            (json.dumps({'kind': 'bom', 'management_kind': 'packaging_drawing', 'identifiers': {'material_no': '7322200005'}}),),
        )
        self.enqueue()
        item = self.items()[0]
        self.assertEqual(item['management_target'], 'packaging_drawing_register')
        self.assertFalse(item['direct_catalog_row'])
        self.assertFalse(item['catalog_locator_enabled'])

    def test_visible_bom_component_candidates_are_appended_for_indirect_materials(self):
        self.h.store.execute(
            "UPDATE records SET module='包装图纸',business_code='MAT-001',version='V1',previous='V0',metadata=? WHERE id='r1'",
            (json.dumps({'management_kind': 'packaging_drawing', 'identifiers': {'material_no': 'MAT-001'}}),),
        )
        self.h.store.execute(
            "UPDATE files SET name='MAT-001包装图纸.pdf',version='V1',metadata=? WHERE id='f1'",
            (json.dumps({'management_kind': 'packaging_drawing', 'identifiers': {'material_no': 'MAT-001'}}),),
        )
        components = {'kind': 'bom', 'bom_components': [{'material_no': 'MAT-001', 'row': 12}, {'material_no': 'MAT-OTHER', 'row': 13}]}
        hidden_components = {'kind': 'bom', 'bom_components': [{'material_no': 'MAT-001', 'row': 22}]}
        self.h.store.execute(
            "INSERT INTO records(id,business_code,name,registrant,type,project_category,product,module,version,previous,metadata) VALUES('bom-visible','BOM-VISIBLE','BOM','深圳金荣','成品','家电','W20','BOM','V1','',?)",
            (json.dumps(components),),
        )
        self.h.store.execute(
            "INSERT INTO files(id,record_id,name,version,status,sha256,blob_path,metadata) VALUES('fbom-visible','bom-visible','BOM-VISIBLE.xlsx','V1','当前有效','sha','files/bom',?)",
            (json.dumps(components),),
        )
        self.h.store.execute(
            "INSERT INTO records(id,business_code,name,registrant,type,project_category,product,module,version,previous,metadata) VALUES('bom-hidden','BOM-HIDDEN','BOM','广西金荣','成品','家电','W20','BOM','V1','',?)",
            (json.dumps(hidden_components),),
        )
        self.h.store.execute(
            "INSERT INTO files(id,record_id,name,version,status,sha256,blob_path,metadata) VALUES('fbom-hidden','bom-hidden','BOM-HIDDEN.xlsx','V1','当前有效','sha2','files/bom2',?)",
            (json.dumps(hidden_components),),
        )
        self.enqueue()
        item = self.items()[0]
        candidates = [c for c in item['relation_candidates'] if c.get('basis') == 'bom_component']
        self.assertEqual(
            [(c['code'], c['row'], c['matched_field']) for c in candidates],
            [('BOM-VISIBLE', '12', 'material_no'), ('BOM-HIDDEN', '22', 'material_no')],
        )
        self.assertEqual(item['related_boms'][-2]['code'], 'BOM-VISIBLE')

        limited = AccessPolicy(self.h.store, {'id': 'limited', 'role': 'reader', 'can_download': False, 'can_history': False,
                                              'scopes': [{'registrant': '广西金荣', 'project_category': '家电', 'product': 'W20'}]})
        self.service._apply_management_policy(item)
        self.service._append_visible_bom_references(limited, item)
        limited_candidates = [c for c in item['relation_candidates'] if c.get('basis') == 'bom_component']
        self.assertEqual([(c['code'], c['row']) for c in limited_candidates], [('BOM-HIDDEN', '22')])


    def test_shared_content_is_recommended_even_under_another_model(self):
        import copy
        docs = self.service.documents()
        shared = copy.deepcopy(docs[0])
        shared.update(token='SharedModel', name='广西由莱-UI04清单', path=['DMR', '医疗项目', 'UI04'])
        result = self.service.match({'registrant':'深圳金荣','product':'W20', 'project_category':'家电',
                                     'business_code':'11W2002C12B','module':'BOM'}, documents=docs+[shared])
        self.assertEqual(result['document_token'], SHEET_TOKEN)
        self.assertIn('SharedModel', result['candidate_tokens'])
        self.assertEqual(len(result['targets']), 1)

    def test_explicit_category_conflict_does_not_auto_associate(self):
        result = self.service.match({'registrant':'深圳金荣','product':'W20', 'project_category':'医疗'}, documents=self.service.documents())
        self.assertEqual(result['document_token'], '')


if __name__ == '__main__':
    unittest.main()
