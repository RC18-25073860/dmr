from __future__ import annotations

import os
import zipfile
from io import BytesIO
import unittest
from collections import defaultdict
from pathlib import Path
import re

from parsers import compare_versions, enrich_batch, parse_file
from tests.test_backend import xlsx_bom


ROOT = Path(os.environ.get("DMR_REAL_WORKFLOW_DATA_DIR", "tests/fixtures/real-workflow"))
BOM_DIR = ROOT / "新增BOM-测试用"
W20_DIR = ROOT / "W20 BOM-不同版本update"
SOFTWARE_NEW = ROOT / "UI20S_CH_JZ_V1.0.2.02.hex 2026.09.18生效-测试用"
SOFTWARE_OLD = ROOT / "UI20S国内老版本-测试用" / "旧版" / "UI20S_CH_JZ_V1.0.2.00.hex 2025.09.26生效"


def parsed(path: Path):
    return parse_file(path.read_bytes(), path.name)


@unittest.skipUnless((ROOT / "新增BOM-测试用").is_dir(), "optional real workflow fixtures unavailable")
class ParserTests(unittest.TestCase):
    def test_all_13_new_finished_bom_files_use_filename_date_as_version(self):
        files = sorted(BOM_DIR.glob("*.xlsx"))
        self.assertEqual(len(files), 13)

        rows = [parsed(path) for path in files]
        expected_dates = {re.search(r"-(20\d{6})\.xlsx$", path.name).group(1) for path in files}

        self.assertEqual({row["metadata"]["kind"] for row in rows}, {"bom"})
        self.assertEqual(len({row["code"] for row in rows}), 13)
        self.assertEqual({row["version"] for row in rows}, expected_dates)
        self.assertTrue(all(row["metadata"]["version_rule"] == "filename_date" for row in rows))
        self.assertTrue(all(row["metadata"]["document_version"] == "V1.0" for row in rows))
        self.assertEqual({row["metadata"]["version_date"] for row in rows}, expected_dates)
        self.assertTrue(all(row["linked_bom"] == row["code"] for row in rows))
        self.assertTrue(all(row["registrant"] for row in rows))
        self.assertTrue(all(row["product"] and len(row["product"]) < 30 for row in rows))
        self.assertTrue(any(row["registrant"] == "新加坡实业" and row["product"] == "MI01 CB" for row in rows))

    def test_ui20s_old_and_new_software_history_link_to_their_own_bom_rows(self):
        new_history = parsed(SOFTWARE_NEW / "UI20S_CH_JZ(国内减重)项目软件履历表.xlsx")
        old_history = parsed(SOFTWARE_OLD / "UI20S_CH_JZ(国内减重)项目软件履历表 2026.07.15更新.xlsx")

        self.assertEqual(new_history["metadata"]["kind"], "software_history")
        self.assertEqual(new_history["metadata"]["management_kind"], "history")
        self.assertEqual(new_history["metadata"]["version_semantic"], "software_program_version")
        self.assertEqual(new_history["metadata"]["document_version"], "")
        self.assertEqual(new_history["code"], "UI20S_CH_JZ(国内减重)项目软件履历表-HISTORY")
        self.assertEqual(new_history["version"], "")
        self.assertEqual(new_history["linked_bom"], "")
        self.assertEqual(new_history["metadata"]["main_selection_status"], "pending")
        self.assertIn("history_main_candidates", new_history["metadata"])

        self.assertEqual(old_history["metadata"]["kind"], "software_history")
        self.assertEqual(old_history["version"], "")
        self.assertEqual(old_history["linked_bom"], "1120SC6C05")
        self.assertEqual(old_history["metadata"]["main_selection_status"], "found")
        self.assertEqual(old_history["metadata"]["main"]["version"], "V1.0.2.00")
        self.assertIn("表内程序版本不等于履历表文档版本", " ".join(old_history["notes"]))
        self.assertTrue(any("1120S09C01B" in v["evidence"].get("BOM信息", "") for v in old_history["metadata"]["versions"]))

    def test_enrich_batch_links_hex_to_exact_history_rows_without_latest_bleed(self):
        items = [
            parsed(SOFTWARE_OLD / "UI20S_CH_JZ(国内减重)项目软件履历表 2026.07.15更新.xlsx"),
            parsed(SOFTWARE_NEW / "UI20S_CH_JZ(国内减重)项目软件履历表.xlsx"),
            parsed(SOFTWARE_NEW / "UI20S_CH_JZ_V1.0.2.02.hex"),
            parsed(SOFTWARE_OLD / "UI20S_CH_JZ_V1.0.2.00.hex"),
        ]

        enrich_batch(items)
        by_version = {item["version"]: item for item in items if item["metadata"]["kind"] == "firmware_hex"}

        self.assertEqual(by_version["V1.0.2.02"]["linked_bom"], "1120S09C02B")
        self.assertEqual(by_version["V1.0.2.00"]["linked_bom"], "1120SC6C05")
        self.assertNotEqual(by_version["V1.0.2.00"]["linked_bom"], by_version["V1.0.2.02"]["linked_bom"])

    def test_w20_same_bom_and_same_version_are_distinct_file_revisions_by_hash_and_date(self):
        files = sorted(W20_DIR.glob("*.xlsx"))
        self.assertEqual(len(files), 6)

        rows = [parsed(path) for path in files]
        by_identity = defaultdict(list)
        for row in rows:
            by_identity[(row["code"], row["metadata"]["document_version"])].append(row)

        self.assertEqual(list(by_identity), [("11W2002C12B", "V1.0")])
        self.assertEqual({row["version"] for row in rows}, {"20260814", "20260819", "20260825", "20260826", "20260903", "20260907"})
        self.assertTrue(all(row["metadata"]["version_rule"] == "filename_date" for row in rows))
        self.assertTrue(all(row["metadata"]["root_bom_version"].endswith("V1.0") for row in rows))
        self.assertEqual(len({row["metadata"]["sha256"] for row in rows}), 6)
        self.assertEqual(len({row["metadata"]["version_date"] for row in rows}), 6)
        self.assertGreaterEqual(len({row["metadata"]["full_spec"] for row in rows}), 2)

    def test_finished_bom_date_version_rule_is_strict(self):
        source = next(W20_DIR.glob("*20260907.xlsx"))

        missing_date = parse_file(source.read_bytes(), "11W2002C12B_V1.0-W20AI【深圳金茉】-成品.xlsx")
        self.assertEqual(missing_date["metadata"]["kind"], "bom")
        self.assertEqual(missing_date["version"], "")
        self.assertEqual(missing_date["metadata"]["version_rule"], "filename_date")
        self.assertIn("文件名缺少有效YYYYMMDD日期", missing_date["metadata"]["parse_error"])

        bad_date = parse_file(source.read_bytes(), "11W2002C12B_V1.0-W20AI【深圳金茉】-成品-20261340.xlsx")
        self.assertEqual(bad_date["metadata"]["kind"], "bom")
        self.assertEqual(bad_date["version"], "")
        self.assertEqual(bad_date["metadata"]["version_rule"], "filename_date")
        self.assertIn("文件名缺少有效YYYYMMDD日期", bad_date["metadata"]["parse_error"])

        mismatched_name = parse_file(source.read_bytes(), "11W2002C13B_V1.0-W20AI【深圳金茉】-成品-20260907.xlsx")
        self.assertEqual(mismatched_name["metadata"]["kind"], "bom")
        self.assertIn("与BOM根编号 11W2002C12B 不一致", mismatched_name["metadata"]["parse_error"])

        other_w20 = parsed(ROOT / "新增BOM-测试用" / "11W2002C13B_V1.0-W20AI 大师白[金茉智能水光仪]_新8弹礼盒（0号+2号）_新国标【深圳金茉】-成品-20260918.xlsx")
        self.assertEqual(other_w20["code"], "11W2002C13B")
        self.assertEqual(other_w20["version"], "20260918")
        self.assertEqual(other_w20["metadata"]["document_version"], "V1.0")
        self.assertEqual(other_w20["metadata"]["version_rule"], "filename_date")

    def test_non_finished_bom_keeps_document_version_semantics(self):
        source = xlsx_bom("BOM-PART", "V1.0", "W20", "深圳金茉")

        part_bom = parse_file(source, "BOM-PART_V1.0-W20【深圳金茉】-部件.xlsx")

        self.assertEqual(part_bom["metadata"]["kind"], "bom")
        self.assertEqual(part_bom["version"], "V1.0")
        self.assertNotIn("version_rule", part_bom["metadata"])

    def test_bom_components_are_explicit_material_rows_not_parent_guess(self):
        source = xlsx_bom_with_rows(
            ["BOM层级", "父项编码", "BOM版本", "规格型号", "物料类别", "子项编码", "图号"],
            [
                ["0", "BOM-COMP", "V1.0", "UI20S[测试产品]【深圳由莱】", "BOM", "PARENT-SHOULD-NOT-APPEAR", "ROOT-DRAW"],
                ["1", "BOM-COMP", "V1.0", "UI20S", "物料", "MAT-001", "DRW-001"],
                ["2", "BOM-COMP", "V1.0", "UI20S", "物料", "MAT-002", ""],
            ],
        )

        item = parse_file(source, "BOM-COMP_V1.0-UI20S【深圳由莱】-部件.xlsx")

        self.assertEqual(
            item["metadata"]["bom_components"],
            [
                {"material_no": "MAT-001", "row": 3, "source_column": "子项编码", "drawing_no": "DRW-001", "drawing_source_column": "图号"},
                {"material_no": "MAT-002", "row": 4, "source_column": "子项编码"},
            ],
        )
        self.assertFalse(item["metadata"]["bom_components_truncated"])

    def test_bom_components_are_bounded_for_reverse_search_indexing(self):
        rows = [["1", "BOM-LARGE", "V1.0", "UI20S", "物料", f"MAT-{i:03d}"] for i in range(505)]
        source = xlsx_bom_with_rows(
            ["BOM层级", "父项编码", "BOM版本", "规格型号", "物料类别", "物料编码"],
            [["0", "BOM-LARGE", "V1.0", "UI20S[测试产品]【深圳由莱】", "BOM", "ROOT"]] + rows,
        )

        item = parse_file(source, "BOM-LARGE_V1.0-UI20S【深圳由莱】-部件.xlsx")

        self.assertEqual(len(item["metadata"]["bom_components"]), 500)
        self.assertTrue(item["metadata"]["bom_components_truncated"])
        self.assertEqual(item["metadata"]["bom_components"][0]["material_no"], "MAT-000")
        self.assertEqual(item["metadata"]["bom_components"][-1]["material_no"], "MAT-499")

    def test_software_attachment_filename_parsing_is_stable_and_conservative(self):
        hex_file = parsed(SOFTWARE_NEW / "UI20S_CH_JZ_V1.0.2.02.hex")
        socprj = parsed(SOFTWARE_NEW / "UI20S_G2(G3_HardWare)_TOUCH_2KEY_V1.0.0.00_0x5426C273.socprj")
        config = parsed(SOFTWARE_NEW / "UI20S_G2(G3_HardWare)_Config_IC=PY32F031xx8_20260106.V16PY32Config")
        tool_zip = parsed(SOFTWARE_NEW / "UI20S校准工具V1.19.04.zip")
        pdf = parsed(SOFTWARE_NEW / "UI20S_CH_JZ(国内减重)项目软件履历表.pdf")

        self.assertEqual(hex_file["code"], "UI20S_CH_JZ")
        self.assertEqual(hex_file["version"], "V1.0.2.02")
        self.assertEqual(socprj["code"], "UI20S_G2(G3_HardWare)_TOUCH_2KEY")
        self.assertEqual(socprj["version"], "V1.0.0.00")
        self.assertEqual(config["metadata"]["kind"], "config")
        self.assertEqual(config["version"], "")
        self.assertEqual(tool_zip["version"], "V1.19.04")
        self.assertEqual(tool_zip["metadata"]["management_kind"], "software")
        self.assertEqual(pdf["metadata"]["kind"], "pdf")
        self.assertEqual(pdf["linked_bom"], "")
        self.assertEqual(pdf["module"], "软件")

    def test_compare_versions_is_segmented_and_strict(self):
        self.assertEqual(compare_versions("V2.10", "V2.3"), 1)
        self.assertEqual(compare_versions("V1.0.2.00", "V1.0.2.02"), -1)
        self.assertEqual(compare_versions("20260907", "20260825"), 1)
        self.assertIsNone(compare_versions("20261340", "20260825"))
        self.assertIsNone(compare_versions("20260907", "V1.0"))
        self.assertIsNone(compare_versions("B04", "V1.0"))

    def test_malformed_xlsx_returns_parse_error_record(self):
        result = parse_file(b"not a zip", "bad.xlsx")

        self.assertEqual(result["metadata"]["kind"], "unknown")
        self.assertTrue(result["metadata"]["parse_error"])
        self.assertTrue(result["notes"])

    def test_enrich_batch_does_not_cross_link_conflicting_registrants_or_boms(self):
        history_a = {
            "metadata": {
                "kind": "software_history",
                "versions": [
                    {
                        "version": "V1.0.0.01",
                        "name": "SAME.hex",
                        "linked_bom": "11111111B",
                        "registrant": "深圳由莱",
                        "product": "UI20S GR",
                    }
                ],
            }
        }
        history_b = {
            "metadata": {
                "kind": "software_history",
                "versions": [
                    {
                        "version": "V1.0.0.01",
                        "name": "SAME.hex",
                        "linked_bom": "22222222B",
                        "registrant": "广西由莱",
                        "product": "UI20S GR",
                    }
                ],
            }
        }
        attachment = parse_file(b":020000040000FA", "SAME.hex")

        enrich_batch([history_a, history_b, attachment])

        self.assertEqual(attachment["linked_bom"], "")
        self.assertEqual(attachment["registrant"], "")
        self.assertIn("history_link_candidates", attachment["metadata"])
        self.assertTrue(any("未自动关联" in note for note in attachment["notes"]))

    def test_pdf_same_stem_companion_is_marked_as_suggestion_only(self):
        history = parsed(SOFTWARE_NEW / "UI20S_CH_JZ(国内减重)项目软件履历表.xlsx")
        pdf = parsed(SOFTWARE_NEW / "UI20S_CH_JZ(国内减重)项目软件履历表.pdf")

        enrich_batch([history, pdf])

        self.assertEqual(pdf["type"], "履历伴随文档")
        self.assertTrue(pdf["metadata"]["companion_suggestion_only"])
        self.assertEqual(pdf["linked_bom"], "")
        self.assertNotIn("linked_history_row", pdf["metadata"])

    def test_xlsx_rejects_doctype_entity_xml(self):
        malicious = _zip_bytes(
            {
                "[Content_Types].xml": b"<?xml version='1.0'?><Types/>",
                "xl/sharedStrings.xml": b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><sst></sst>",
                "xl/worksheets/sheet1.xml": b"<?xml version='1.0'?><worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'/>",
            }
        )

        result = parse_file(malicious, "evil.xlsx")

        self.assertEqual(result["metadata"]["kind"], "unknown")
        self.assertIn("doctype/entity", result["metadata"]["parse_error"])

    def test_docx_corruption_is_explicit_rejection(self):
        result = parse_file(b"not a zip", "broken.docx")

        self.assertEqual(result["metadata"]["kind"], "unknown")
        self.assertTrue(result["metadata"]["parse_error"])
        self.assertTrue(any("解析失败" in note for note in result["notes"]))

    def test_xlsx_rejects_unsafe_zip_member_path(self):
        bad = _zip_bytes(
            {
                "../xl/worksheets/sheet1.xml": b"<worksheet/>",
                "xl/worksheets/sheet1.xml": b"<worksheet/>",
            }
        )

        result = parse_file(bad, "bad-path.xlsx")

        self.assertEqual(result["metadata"]["kind"], "unknown")
        self.assertIn("unsafe zip member", result["metadata"]["parse_error"])

    def test_zip_attachment_is_not_opened_or_executed_even_when_nested(self):
        nested = _zip_bytes(
            {
                "payload.xlsx": _zip_bytes(
                    {
                        "xl/sharedStrings.xml": b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><sst></sst>",
                    }
                )
            }
        )

        result = parse_file(nested, "UI20S工具V1.2.3.zip")

        self.assertEqual(result["metadata"]["kind"], "zip_attachment")
        self.assertEqual(result["version"], "V1.2.3")
        self.assertNotIn("parse_error", result["metadata"])

    def test_plain_zip_is_attachment_not_software_by_default(self):
        result = parse_file(b"not really a zip", "外部资料包V1.2.3.zip")

        self.assertEqual(result["metadata"]["kind"], "zip_attachment")
        self.assertEqual(result["metadata"]["management_kind"], "attachment")
        self.assertEqual(result["module"], "")
        self.assertEqual(result["type"], "压缩包附件")

    def test_software_history_with_one_program_identity_can_select_main(self):
        versions = [
            {"name": "UI20S_CH_JZ_V1.0.2.00.hex", "version": "V1.0.2.00", "linked_bom": "BOM1"},
            {"name": "UI20S_CH_JZ_V1.0.2.00.hex", "version": "V1.0.2.00", "linked_bom": "BOM1"},
        ]

        selected = __import__("parsers")._select_history_main_row(versions)

        self.assertEqual(selected["version"], "V1.0.2.00")

    def test_software_history_does_not_pick_max_across_branches(self):
        versions = [
            {"name": "UI20S_APP_V9.9.9.9.hex", "version": "V9.9.9.9", "linked_bom": "BOM_APP"},
            {"name": "UI20S_BOOT_V1.0.0.1.hex", "version": "V1.0.0.1", "linked_bom": "BOM_BOOT"},
        ]

        selected = __import__("parsers")._select_history_main_row(versions)

        self.assertIsNone(selected)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def xlsx_bom_with_rows(headers: list[str], rows: list[list[str]]) -> bytes:
    def cell(col: str, row: int, value: str) -> str:
        return f'<c r="{col}{row}" t="inlineStr"><is><t>{value}</t></is></c>'

    xml_rows = []
    for row_index, values in enumerate([headers] + rows, start=1):
        cols = "".join(cell(chr(ord("A") + i), row_index, value) for i, value in enumerate(values))
        xml_rows.append(f'<row r="{row_index}">{cols}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"
    )
    return _zip_bytes({"[Content_Types].xml": b"<Types/>", "xl/worksheets/sheet1.xml": sheet.encode("utf-8")})


if __name__ == "__main__":
    unittest.main()
