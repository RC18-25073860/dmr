from __future__ import annotations

from pathlib import Path
import os
import unittest

from document_parser import extract_software_version, parse_document_file


FILES = Path(os.environ.get("DMR_DOCUMENT_FIXTURE_DIR", "tests/fixtures/documents"))
DOCX_RELEASE = FILES / "06392315-ded4-4e33-b694-d3deaba7e0fe.docx"
PDF_RELEASE = FILES / "c5701299-af38-4e82-9eb1-6fa192ccafc3.pdf"
PDF_HISTORY = FILES / "3da47838-6a4b-4a58-a5ba-664a09002d70.pdf"


class DocumentParserTests(unittest.TestCase):
    @unittest.skipUnless(DOCX_RELEASE.exists(), "local regression sample unavailable")
    def test_docx_release_record_keeps_release_version_and_records_firmware_version(self):
        item = parse_document_file(DOCX_RELEASE.read_bytes(), "UI20S_CH_JZ软件发布记录.docx")
        evidence = item["metadata"]["document_parse"]

        self.assertEqual(item["code"], "UI20S_CH_JZ软件发布记录")
        self.assertEqual(item["type"], "软件发布记录")
        self.assertEqual(item["module"], "软件")
        self.assertEqual(item["version"], "V1.0")
        self.assertEqual(evidence["version_source"], "发布版本")
        self.assertEqual(evidence["document_version"], "V1.0")
        self.assertEqual(evidence["firmware_version"], "V1.0.2.00")
        self.assertEqual(evidence["software_filename"], "UI20S_CH_JZ_V1.0.2.00.hex")
        self.assertIn(
            {"source": "软件文件名", "version": "V1.0.2.00", "scope": "software_file", "software_filename": "UI20S_CH_JZ_V1.0.2.00.hex", "selected": False},
            evidence["version_candidates"],
        )

    @unittest.skipUnless(PDF_RELEASE.exists(), "local regression sample unavailable")
    def test_pdf_release_record_uses_bundled_extractor_and_preserves_document_code(self):
        item = parse_document_file(PDF_RELEASE.read_bytes(), "UI20S_CH_JZ软件发布记录.pdf")
        evidence = item["metadata"]["document_parse"]

        self.assertEqual(item["code"], "UI20S_CH_JZ软件发布记录")
        self.assertEqual(item["type"], "软件发布记录")
        self.assertEqual(item["version"], "V1.0")
        self.assertEqual(item["metadata"]["pdf_text_extractor"], "pdfplumber_or_pypdf_subprocess")
        self.assertEqual(evidence["version_source"], "发布版本")
        self.assertEqual(evidence["firmware_version"], "V1.0.2.00")
        self.assertEqual(evidence["software_filename"], "UI20S_CH_JZ_V1.0.2.00.hex")
        self.assertTrue(any(row["selected"] and row["source"] == "发布版本" for row in evidence["version_candidates"]))

    @unittest.skipUnless(PDF_HISTORY.exists(), "local regression sample unavailable")
    def test_pdf_history_extracts_unique_software_file_version_without_bom_company_guessing(self):
        item = parse_document_file(PDF_HISTORY.read_bytes(), "UI20S_CH_JZ(国内减重)项目软件履历表---补充BOM信息.pdf")
        evidence = item["metadata"]["document_parse"]

        self.assertEqual(item["code"], "UI20S_CH_JZ(国内减重)项目软件履历表---补充BOM信息")
        self.assertEqual(item["type"], "软件履历")
        self.assertEqual(item["module"], "软件")
        self.assertEqual(item["version"], "V1.0.2.00")
        self.assertEqual(item["product"], "UI20S GR")
        self.assertEqual(item["linked_bom"], "")
        self.assertEqual(item["registrant"], "")
        self.assertEqual(evidence["document_role"], "software_history")
        self.assertEqual(evidence["version_source"], "软件文件名")
        self.assertEqual(evidence["software_filename"], "UI20S_CH_JZ_V1.0.2.00.hex")
        self.assertEqual(evidence["firmware_version"], "V1.0.2.00")

    def test_conflicting_software_file_versions_are_candidates_not_selected(self):
        evidence = extract_software_version(
            "软件履历 软件名字: UI20S_CH_JZ_V1.0.2.00.hex "
            "软件名字: UI20S_CH_JZ_V1.0.2.02.hex",
            "UI20S_CH_JZ项目软件履历表.pdf",
        )

        self.assertEqual(evidence["document_role"], "software_history")
        self.assertEqual(evidence["version"], "")
        self.assertEqual(evidence["firmware_version"], "")
        self.assertEqual(
            {row["version"] for row in evidence["version_candidates"] if row["source"] == "软件文件名"},
            {"V1.0.2.00", "V1.0.2.02"},
        )
        self.assertFalse(any(row["selected"] for row in evidence["version_candidates"]))

    def test_conflicting_release_versions_are_candidates_not_selected(self):
        evidence = extract_software_version(
            "软件发布记录 发布版本: V1.0 发布版本: V1.1 文件名称：UI20S_CH_JZ_V1.0.2.00.hex",
            "UI20S_CH_JZ软件发布记录.pdf",
        )

        self.assertEqual(evidence["document_role"], "software_release_record")
        self.assertEqual(evidence["version"], "")
        self.assertEqual(evidence["firmware_version"], "V1.0.2.00")
        self.assertEqual(
            {row["version"] for row in evidence["version_candidates"] if row["source"] == "发布版本"},
            {"V1.0", "V1.1"},
        )
        self.assertFalse(any(row["selected"] for row in evidence["version_candidates"]))


if __name__ == "__main__":
    unittest.main()
