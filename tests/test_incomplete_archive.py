import base64
import json
import tempfile
from pathlib import Path
from backend.service import DMRStore


def test_explicit_review_allows_missing_fields_and_commits_without_inventing_version():
    with tempfile.TemporaryDirectory() as tmp:
        store=DMRStore(Path(tmp).resolve())
        try:
            batch=store.analyze([{'name':'UI20S_Config.V16PY32Config','data':base64.b64encode(b'config').decode()}])
            item=batch['items'][0]
            assert item['status']=='unmatched'
            payload={'batch_id':batch['batch_id'],'item_id':item['id'],'expected_edit_token':item['edit_token']}
            reviewed=store.review(payload)['item']
            assert reviewed['status']=='unmatched'
            payload.update(expected_edit_token=reviewed['edit_token'],allow_incomplete=True)
            with store.acting_as('文控测试'):
                reviewed=store.review(payload)['item']
            assert reviewed['status']=='matched'
            approval=reviewed['metadata']['incomplete_archive']
            assert approval['confirmed_by']=='文控测试'
            assert 'version' in approval['missing_fields']
            result=store.commit_batch(batch['batch_id'])
            assert result['committed']==1
            file=store.execute('SELECT * FROM files').fetchone()
            assert file['version']==''
            assert json.loads(file['metadata'])['incomplete_archive']['missing_fields']
        finally: store.close()


def test_missing_version_does_not_replace_existing_or_collapse_other_files():
    with tempfile.TemporaryDirectory() as tmp:
        store=DMRStore(Path(tmp).resolve())
        try:
            for content in [b'first',b'second']:
                batch=store.analyze([{'name':'UI20S_Config.V16PY32Config','data':base64.b64encode(content).decode()}])
                item=batch['items'][0]
                store.review({'batch_id':batch['batch_id'],'item_id':item['id'],'expected_edit_token':item['edit_token'],'allow_incomplete':True})
                assert store.commit_batch(batch['batch_id'])['committed']==1
            assert store.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n']==2
            assert store.execute("SELECT COUNT(*) AS n FROM files WHERE status='当前有效'").fetchone()['n']==2
        finally: store.close()


def test_manual_selected_record_allows_incomplete_archive_without_overwriting_current():
    with tempfile.TemporaryDirectory() as tmp:
        store=DMRStore(Path(tmp).resolve())
        try:
            initial=store.analyze([{'name':'UI20S_CH_JZ_V1.0.2.02.hex','data':base64.b64encode(b'current').decode()}])
            item=initial['items'][0]
            reviewed=store.review({
                'batch_id':initial['batch_id'],'item_id':item['id'],'expected_edit_token':item['edit_token'],
                'code':'UI20S_CH_JZ','registrant':'广西由莱','product':'UI20S GR','module':'软件','type':'软件',
                'project_category':'医疗','version':'V1.0.2.02'
            })['item']
            assert reviewed['status']=='matched'
            assert store.commit_batch(initial['batch_id'])['committed']==1
            record=store.execute("SELECT * FROM records WHERE business_code='UI20S_CH_JZ'").fetchone()

            batch=store.analyze([{'name':'UI20S_Config.V16PY32Config','data':base64.b64encode(b'incomplete').decode()}])
            item=batch['items'][0]
            with store.acting_as('文控测试'):
                reviewed=store.review({
                    'batch_id':batch['batch_id'],'item_id':item['id'],'expected_edit_token':item['edit_token'],
                    'record_id':record['id'],'allow_incomplete':True,'review_note':'人工确认归档，版本暂缺'
                })['item']
            assert reviewed['status']=='matched'
            approval=reviewed['metadata']['incomplete_archive']
            assert approval['selected_record_id']==record['id']
            assert 'version' in approval['missing_fields']
            result=store.commit_batch(batch['batch_id'])
            assert result['committed']==1
            current=store.execute("SELECT * FROM records WHERE id=?", (record['id'],)).fetchone()
            assert current['version']=='V1.0.2.02'
            assert store.execute('SELECT COUNT(*) AS n FROM records').fetchone()['n']==2
        finally: store.close()


def test_save_pending_revokes_incomplete_approval():
    with tempfile.TemporaryDirectory() as tmp:
        store=DMRStore(Path(tmp).resolve())
        try:
            batch=store.analyze([{'name':'UI20S_Config.V16PY32Config','data':base64.b64encode(b'config').decode()}])
            item=batch['items'][0]
            payload={'batch_id':batch['batch_id'],'item_id':item['id'],'expected_edit_token':item['edit_token'],'allow_incomplete':True}
            item=store.review(payload)['item']
            payload.update(expected_edit_token=item['edit_token'],save_pending=True)
            item=store.review(payload)['item']
            assert item['status']=='unmatched'
            assert 'incomplete_archive' not in item['metadata']
            assert store.commit_batch(batch['batch_id'])['committed']==0
        finally: store.close()


def test_api_commit_subject_gate_honors_explicit_missing_subject_review():
    from tests.test_backend import with_server, b64
    def run(h, data):
        batch=h.request('POST','/api/analyze',{'files':[{'name':'UI20S_Config.V16PY32Config','data':b64(b'config')}]})
        item=batch['items'][0]
        h.request('POST','/api/review',{'batch_id':batch['batch_id'],'item_id':item['id'],'expected_edit_token':item['edit_token'],'allow_incomplete':True})
        result=h.request('POST','/api/commit',{'batch_id':batch['batch_id'],'restrict_new_subject':True})
        assert result['committed']==1,result
    with_server(run)
