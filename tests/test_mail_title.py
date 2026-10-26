import unittest

from backend.mail_title import apply_mail_title_evidence, classify_mail_subject, extract_mail_title


class MailTitleTests(unittest.TestCase):
    def test_ecn_mail_is_distinct_from_bom_mail(self):
        self.assertEqual(classify_mail_subject('深圳由莱-ECN（2026100005）BOM变更'), 'ecn')
        self.assertEqual(classify_mail_subject('深圳由莱-ENC（2026100005）BOM变更'), 'ecn')
        self.assertEqual(classify_mail_subject('BOM发放（W20冷凝舱）'), 'bom')
        self.assertEqual(classify_mail_subject('资料更新'), 'other')
        self.assertNotIn('module', extract_mail_title('深圳由莱-ECN（2026100005）BOM变更').get('fields', {}))

    def test_extracts_confirmed_subject_category_product_and_module_only(self):
        title = extract_mail_title("广西由莱-医疗项目-UI06S PR-DMR BOM-新版资料 V9.9")
        self.assertEqual(title["fields"]["registrant"]["value"], "广西由莱")
        self.assertEqual(title["fields"]["project_category"]["value"], "医疗")
        self.assertEqual(title["fields"]["product"]["value"], "UI06S PR")
        self.assertEqual(title["fields"]["module"]["value"], "BOM")
        self.assertNotIn("version", title["fields"])

    def test_missing_subject_produces_no_evidence(self):
        self.assertEqual(extract_mail_title("建筑附件更新"), {})

    def test_applies_only_missing_fields_and_preserves_conflicts(self):
        title = extract_mail_title("广西由莱-医疗-UI06S PR-软件")
        item = {
            "registrant": "深圳金茉",
            "project_category": "待确认",
            "project_category_source": "未提供",
            "product": "",
            "module": "软件",
            "metadata": {},
        }

        updated, evidence = apply_mail_title_evidence(item, title)

        self.assertEqual(updated["registrant"], "深圳金茉")
        self.assertEqual(updated["project_category"], "医疗")
        self.assertEqual(updated["project_category_source"], "邮件标题")
        self.assertEqual(updated["product"], "UI06S PR")
        self.assertEqual(updated["module"], "软件")
        self.assertEqual(evidence["applied"], ["project_category", "product"])
        self.assertEqual(evidence["conflicts"], [{"field": "registrant", "existing": "深圳金茉", "mail_title": "广西由莱"}])
        self.assertEqual(updated["metadata"]["mail_title"]["status"], "conflict")


    def test_pending_filename_fields_are_not_silently_resolved(self):
        title=extract_mail_title('广西由莱-医疗-UI20S-软件')
        for field in ('registrant','product','module'):
            updated,evidence=apply_mail_title_evidence({field:'','metadata':{field+'_reading_status':'pending'}},title)
            self.assertEqual(updated[field],'')
            self.assertTrue(updated['metadata']['review_pending'])
            self.assertIn(field,[c['field'] for c in evidence['conflicts']])

    def test_model_family_preserves_specific_attachment_variant(self):
        updated,evidence=apply_mail_title_evidence({'product':'UI06S PR','metadata':{}},extract_mail_title('UI06S-软件'))
        self.assertEqual(updated['product'],'UI06S PR')
        self.assertFalse(evidence['conflicts'])

    def test_version_or_file_number_is_not_used_as_product(self):
        title=extract_mail_title('广西由莱-医疗-11UI04AA01-软件-V1.0')
        self.assertNotIn('product',title['fields'])


if __name__ == "__main__":
    unittest.main()
