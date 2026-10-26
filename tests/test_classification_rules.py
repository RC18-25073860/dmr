import json
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.classification import apply_classification


class ClassificationRuleTests(unittest.TestCase):
    def classify(self, item, filename=""):
        return apply_classification(item, filename=filename)

    def project_meta(self, result):
        return result["metadata"]["project_classification"]

    def test_bom_registrant_and_filename_medical_classify_ui06s(self):
        result = self.classify(
            {
                "type": "BOM",
                "registrant": "广西由莱",
                "product": "UI06S PRU",
                "project_category": "待确认",
                "project_category_source": "未提供",
            },
            "1106S05901B_V1.0-UI06S PRU 强脉冲光治疗仪（水晶紫_哑面_MODE_国内医疗版_线下通用版）【广西由莱】-成品（国内_医疗）-20260324.xlsx",
        )
        self.assertEqual(result["project_category"], "医疗")
        self.assertFalse(self.project_meta(result)["conflict"])
        self.assertTrue(any("UI06S" in row["detail"] for row in self.project_meta(result)["evidence"]))

    def test_w20ai_uses_w20_alias_and_does_not_need_prefix_guessing(self):
        result = self.classify(
            {"type": "BOM", "registrant": "深圳金茉", "product": "W20AI 大师白", "project_category": "待确认"},
            "11W2002C12B_V1.0-W20AI 大师白【深圳金茉】-成品-20260907.xlsx",
        )
        self.assertEqual(result["project_category"], "家电")
        self.assertTrue(any(row["data"].get("canonical_product") == "W20" for row in self.project_meta(result)["evidence"]))

    def test_longest_boundary_match_keeps_m12u_from_being_swallowed_by_m12(self):
        result = self.classify(
            {"type": "BOM", "registrant": "测试主体", "product": "M12U", "project_category": "待确认"},
            "BOM-M12U 医疗资料.xlsx",
        )
        self.assertEqual(result["project_category"], "医疗")
        product_evidence = [row for row in self.project_meta(result)["evidence"] if row["source"] == "用户提供截图目录"]
        self.assertEqual(product_evidence[0]["data"]["canonical_product"], "M12U")

    def test_unknown_suffix_does_not_match_product_catalog(self):
        result = self.classify(
            {"type": "BOM", "registrant": "测试主体", "product": "UI20Sxxx", "project_category": "待确认"},
            "UI20Sxxx BOM.xlsx",
        )
        self.assertEqual(result["project_category"], "待确认")
        self.assertFalse(any(row["source"] == "用户提供截图目录" for row in self.project_meta(result)["evidence"]))

    def test_nonmedical_negation_wins_over_medical_substring(self):
        result = self.classify(
            {"type": "BOM", "registrant": "测试主体", "product": "未知产品", "project_category": "待确认"},
            "某产品-非医疗版-说明.xlsx",
        )
        self.assertEqual(result["project_category"], "家电")
        self.assertFalse(self.project_meta(result)["conflict"])

    def test_conflicting_clear_evidence_requires_manual_confirmation(self):
        result = self.classify(
            {"type": "BOM", "registrant": "深圳金茉", "product": "W20", "project_category": "医疗", "project_category_source": "项目类别"},
            "11W2002C12B_V1.0-W20【深圳金茉】-成品-20260907.xlsx",
        )
        self.assertEqual(result["project_category"], "待确认")
        self.assertEqual(result["project_category_source"], "规则依据冲突，待核对")
        self.assertTrue(self.project_meta(result)["conflict"])

    def test_manual_classification_is_kept_even_when_rules_disagree(self):
        result = self.classify(
            {"type": "BOM", "registrant": "深圳金茉", "product": "W20", "project_category": "医疗", "project_category_source": "人工核对"},
            "11W2002C12B_V1.0-W20【深圳金茉】-成品-20260907.xlsx",
        )
        self.assertEqual(result["project_category"], "医疗")
        self.assertEqual(result["project_category_source"], "人工核对")
        self.assertFalse(self.project_meta(result)["conflict"])

    def test_singapore_industry_is_not_generalized_to_singapore_youlai(self):
        result = self.classify(
            {"type": "BOM", "registrant": "新加坡实业", "product": "未知", "project_category": "待确认"},
            "BOM-新加坡实业-未知产品.xlsx",
        )
        self.assertEqual(result["project_category"], "待确认")
        self.assertFalse(any(row["source"] == "瑞瑞BOM规则" for row in self.project_meta(result)["evidence"]))

    def test_non_bom_does_not_use_bom_registrant_rule_but_uses_catalog(self):
        from_registrant_only = self.classify(
            {"type": "软件资料", "registrant": "广西由莱", "product": "未知", "project_category": "待确认"},
            "广西由莱-固件.hex",
        )
        self.assertEqual(from_registrant_only["project_category"], "待确认")

        from_catalog = self.classify(
            {"type": "软件资料", "registrant": "测试主体", "product": "UI06S", "project_category": "待确认"},
            "UI06S 固件.hex",
        )
        self.assertEqual(from_catalog["project_category"], "医疗")

    def test_metadata_json_string_is_preserved_and_augmented(self):
        result = self.classify(
            {
                "type": "BOM",
                "registrant": "深圳金茉",
                "product": "W20",
                "project_category": "待确认",
                "metadata": json.dumps({"version_rule": "filename_date"}, ensure_ascii=False),
            },
            "W20 BOM.xlsx",
        )
        self.assertEqual(result["metadata"]["version_rule"], "filename_date")
        self.assertEqual(result["metadata"]["project_classification"]["rule_version"], "2026-09-22-r1")

    def test_metadata_and_notes_are_not_used_as_product_catalog_evidence(self):
        result = self.classify(
            {
                "type": "BOM",
                "registrant": "测试主体",
                "product": "未知",
                "project_category": "待确认",
                "project_category_source": "未提供",
                "notes": "适用 W20 包装附件",
                "metadata": {"related_models": ["UI06S", "W20"]},
            },
            "未知产品.xlsx",
        )
        self.assertEqual(result["project_category"], "待确认")
        self.assertEqual(result["project_category_source"], "未提供")

    def test_multiple_catalog_categories_in_product_or_filename_are_conflict(self):
        result = self.classify(
            {"type": "BOM", "registrant": "测试主体", "product": "UI06S / W20", "project_category": "待确认"},
            "UI06S-W20-资料.xlsx",
        )
        self.assertEqual(result["project_category"], "待确认")
        self.assertTrue(result["metadata"]["project_classification"]["conflict"])

    def test_manual_source_containing_manual_word_is_kept(self):
        result = self.classify(
            {"type": "BOM", "registrant": "深圳金茉", "product": "W20", "project_category": "医疗", "project_category_source": "人工确认"},
            "W20.xlsx",
        )
        self.assertEqual(result["project_category"], "医疗")
        self.assertEqual(result["project_category_source"], "人工确认")

    def test_declared_source_and_unknown_source_stay_compatible(self):
        declared = self.classify(
            {"type": "BOM", "registrant": "测试主体", "product": "TESTMODEL", "project_category": "医疗", "project_category_source": "项目类别"},
            "TESTMODEL.xlsx",
        )
        self.assertEqual(declared["project_category"], "医疗")
        self.assertEqual(declared["project_category_source"], "项目类别")

        unknown = self.classify(
            {"type": "BOM", "registrant": "测试主体", "product": "TESTMODEL", "project_category": "待确认", "project_category_source": "未提供"},
            "TESTMODEL.xlsx",
        )
        self.assertEqual(unknown["project_category"], "待确认")
        self.assertEqual(unknown["project_category_source"], "未提供")

    def test_fullwidth_parentheses_product_folder_names_match(self):
        result = self.classify(
            {"type": "BOM", "registrant": "测试主体", "product": "UI04 （A1）", "project_category": "待确认"},
            "UI04 （A1） BOM.xlsx",
        )
        self.assertEqual(result["project_category"], "医疗")


if __name__ == "__main__":
    unittest.main()
