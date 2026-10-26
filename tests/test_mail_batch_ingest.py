import json
import unittest
from unittest.mock import patch
from test_mail_ingest import MailIngestTests

class MailBatchTests(unittest.TestCase):
    setUp=MailIngestTests.setUp
    tearDown=MailIngestTests.tearDown
    content_payload=MailIngestTests.content_payload

    def group(self):
        first=self.content_payload(b'first',filename='first.txt',attachment_id='archive/path/first.txt')
        second=self.content_payload(b'second',filename='second.txt',attachment_id='archive/path/second.txt')
        return {**{k:v for k,v in first.items() if k!='attachment'},'attachments':[first['attachment'],second['attachment']]}

    def test_same_batch_retries_and_changed_payload(self):
        self.h.create_mail_client(self.admin)
        client=self.h.bearer_client();payload=self.group()
        with patch.object(self.h.store,'analyze',wraps=self.h.store.analyze) as analyze:
            response=client.post('/api/integrations/mail/batches',json=payload)
            self.assertEqual(response.status_code,201,response.text)
            self.assertEqual(len(analyze.call_args.args[0]),2)
            receipts=response.json()['receipts']
            self.assertEqual(len({r['batch_id'] for r in receipts}),1)
            self.assertEqual(len(receipts),2)
            again=client.post('/api/integrations/mail/batches',json=payload)
            self.assertEqual(again.status_code,200,again.text)
            self.assertEqual(analyze.call_count,1)
        payload['subject']='changed'
        self.assertEqual(client.post('/api/integrations/mail/batches',json=payload).status_code,409)

    def test_ecn_subject_keeps_mail_case_separate_from_attachment_type(self):
        self.h.create_mail_client(self.admin)
        payload = self.group()
        payload['subject'] = '深圳由莱-ECN（2026100005）BOM变更'
        response = self.h.bearer_client().post('/api/integrations/mail/batches', json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        rows = self.h.store.execute('SELECT metadata FROM batch_items WHERE batch_id=?',
                                    (response.json()['batch_id'],)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(json.loads(row['metadata'])['email_source']['mail_case'] == 'ecn' for row in rows))
        self.assertTrue(all(json.loads(row['metadata'])['review_pending'] for row in rows))
        self.assertEqual(self.h.store.execute("SELECT COUNT(*) AS n FROM batch_items WHERE batch_id=? AND status='conflict'",
                                              (response.json()['batch_id'],)).fetchone()['n'], 2)

    def test_validation_atomicity_and_auth(self):
        self.h.create_mail_client(self.admin)
        client=self.h.bearer_client();payload=self.group()
        payload['attachments'][1]['size']=12345
        self.assertEqual(client.post('/api/integrations/mail/batches',json=payload).status_code,422)
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM mail_receipts').fetchone()['n'],0)
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM batches').fetchone()['n'],0)
        self.assertEqual(self.h.bearer_client('wrong').post('/api/integrations/mail/batches',json=self.group()).status_code,401)

    def test_rollback_cleans_all_files(self):
        self.h.create_mail_client(self.admin)
        with patch.object(self.h.store,'add_log',side_effect=RuntimeError('test rollback')):
            response=self.h.bearer_client().post('/api/integrations/mail/batches',json=self.group())
            self.assertEqual(response.status_code,500)
        self.assertEqual(self.h.store.execute('SELECT COUNT(*) AS n FROM batches').fetchone()['n'],0)
        self.assertEqual(list(self.h.store.files_dir.iterdir()),[])

    def test_mail_batch_preserves_history_to_firmware_link(self):
        from test_parsers import SOFTWARE_NEW
        history=SOFTWARE_NEW / 'UI20S_CH_JZ(国内减重)项目软件履历表.xlsx'
        firmware=SOFTWARE_NEW / 'UI20S_CH_JZ_V1.0.2.02.hex'
        if not history.is_file() or not firmware.is_file():
            self.skipTest('optional real software samples unavailable')
        self.h.create_mail_client(self.admin)
        rows=[self.content_payload(p.read_bytes(),filename=p.name,attachment_id=p.name) for p in (history,firmware)]
        payload={**{k:v for k,v in rows[0].items() if k!='attachment'},'attachments':[r['attachment'] for r in rows]}
        response=self.h.bearer_client().post('/api/integrations/mail/batches',json=payload)
        self.assertEqual(response.status_code,201,response.text)
        batch=self.h.store.get_batch(response.json()['batch_id'])
        item=next(i for i in batch['items'] if i['fileName']==firmware.name)
        self.assertEqual(item.get('linkedBom',item.get('linked_bom')),'1120S09C02B')
