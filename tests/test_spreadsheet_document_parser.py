from __future__ import annotations

import os
import unittest
from pathlib import Path

from parsers import _read_xlsx
from spreadsheet_document_parser import (
    extract_spreadsheet_document_version,
    find_spreadsheet_version_candidates,
    is_software_history_version_row,
)


REAL_UI20S_G2_RELEASE = Path(
    os.environ.get("DMR_SPREADSHEET_SAMPLE", "tests/fixtures/real-spreadsheet.xlsx")
)


class SpreadsheetDocumentParserTests(unittest.TestCase):
    def test_real_ui20s_g2_release_prefers_labeled_firmware_version(self):
        if not REAL_UI20S_G2_RELEASE.exists():
            self.skipTest("real UI20S G2 release workbook is not available")

        sheets = _read_xlsx(REAL_UI20S_G2_RELEASE.read_bytes())

        result = extract_spreadsheet_document_version(sheets=sheets)

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["scope"], "software")
        self.assertEqual(result["version"], "V1.0.2.00")
        self.assertEqual(result["version_evidence"]["label"], "固件版本")
        self.assertEqual(result["version_evidence"]["raw_value"], "UI20S CH JZ_V1.0.2.00 2025-9-17")
        self.assertTrue(
            any(candidate["scope"] == "document" and candidate["version"] == "V1.0" for candidate in result["version_candidates"])
        )

    def test_rows_input_extracts_document_report_version(self):
        result = extract_spreadsheet_document_version(
            rows=[
                ["文档编号\nDocument ID"],
                ["版本\nReport Version", "Ver 1.0"],
            ]
        )

        self.assertEqual(result["status"], "found")
        self.assertEqual(result["scope"], "document")
        self.assertEqual(result["version"], "V1.0")
        self.assertEqual(result["version_evidence"]["row"], 2)

    def test_multiple_labeled_software_versions_are_pending_not_ranked(self):
        result = extract_spreadsheet_document_version(
            rows=[
                ["固件版本", "APP_V1.0.2.00"],
                ["软件版本", "BOOT_V1.0.0.03"],
                ["版本\nReport Version", "Ver 9.9"],
            ]
        )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["scope"], "software")
        self.assertEqual(result["version"], "")
        self.assertIsNone(result["version_evidence"])
        self.assertEqual({candidate["version"] for candidate in result["version_candidates"] if candidate["scope"] == "software"}, {"V1.0.2.00", "V1.0.0.03"})

    def test_version_like_unlabeled_text_is_ignored(self):
        candidates = find_spreadsheet_version_candidates(
            rows=[
                ["测试名称", "软件版本确认"],
                ["更新履历", "串口指令获取版本信息：UI20S_CH_JZ_V1.0.2.00 2025-9-17"],
            ]
        )

        self.assertEqual(candidates, [])

    def test_software_history_predicate_accepts_real_software_row(self):
        self.assertTrue(
            is_software_history_version_row(
                {
                    "发布版本": "V1.0",
                    "完整版本": "V1.0.2.00",
                    "软件名字": "UI20S_CH_JZ_V1.0.2.00.hex",
                    "BOM信息": "1120SC6C05成品【广西由莱】",
                }
            )
        )

    def test_software_history_predicate_rejects_bom_appendix_rows(self):
        appendix_row = {
            "序号": "1120SC6C09",
            "发布版本": "成品（带NFC_不带附属头_减重版_国内_线上）_试产",
            "完整版本": "UI20S GR 强脉冲光治疗仪（UI20S_双灯管脱毛仪_灰锖色_带NFC_不带附属头_减重版_国内_哑面_线上）【广西由莱】",
            "软件名字": "",
        }

        self.assertFalse(is_software_history_version_row(appendix_row))


if __name__ == "__main__":
    unittest.main()
