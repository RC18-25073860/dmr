import json
import subprocess
import unittest

from backend.catalog_locator import LarkSheetReader, read_live_location


TOKEN = "ExampleCatalogToken0000000001"
SHEET_ID = "0GCbnr"


def envelope(data):
    return subprocess.CompletedProcess(
        args=["lark-cli"],
        returncode=0,
        stdout=json.dumps({"ok": True, "data": data}, ensure_ascii=False),
        stderr="",
    )


def cell(value=""):
    return {"value": value}


def sheet_rows(*, duplicate=False, moved=False, header_ambiguous=False):
    rows = [[cell("") for _ in range(6)] for _ in range(60)]
    header_row = 7 if moved else 8
    data_row = 40 if moved else 38
    rows[header_row - 1][3] = cell("编号")
    rows[header_row - 1][4] = cell("版本")
    if header_ambiguous:
        rows[header_row - 1][5] = cell("版本号")
    rows[data_row - 1][1] = cell("V1.2-UI04 医疗版脱毛仪 白色 B1【广西由莱】成品BOM")
    rows[data_row - 1][3] = cell("11UI04AA01")
    rows[data_row - 1][4] = cell("20260410")
    if duplicate:
        rows[45 - 1][3] = cell("11UI04AA01")
        rows[45 - 1][4] = cell("20260924")
    return rows


class FakeCli:
    def __init__(self, rows=None, *, revision=9, second_revision=None, fail_cells=False):
        self.rows = rows or sheet_rows()
        self.revision = revision
        self.second_revision = second_revision if second_revision is not None else revision
        self.fail_cells = fail_cells
        self.commands = []
        self.workbook_reads = 0

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        assert command[:3] == ["lark-cli", "sheets", command[2]]
        assert command[2] in {"+workbook-info", "+cells-get"}
        assert "--as" in command and "--format" in command
        assert "--spreadsheet-token" in command and TOKEN in command
        if command[2] == "+workbook-info":
            self.workbook_reads += 1
            revision = self.revision if self.workbook_reads == 1 else self.second_revision
            return envelope(
                {
                    "revision": revision,
                    "sheets": [
                        {
                            "sheet_id": SHEET_ID,
                            "sheet_name": "广西由莱-UI04A、B、C广西国内医疗版",
                            "row_count": len(self.rows),
                            "column_count": len(self.rows[0]),
                        }
                    ],
                }
            )
        if self.fail_cells:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr=json.dumps({"ok": False, "error": {"message": "permission denied"}}),
            )
        range_arg = command[command.index("--range") + 1]
        start, end = [int(part) for part in range_arg.replace("A", "").split(":F")]
        return envelope(
            {
                "revision": self.revision,
                "has_more": False,
                "ranges": [
                    {
                        "range": range_arg,
                        "actual_range": range_arg,
                        "row_indices": list(range(start, end + 1)),
                        "col_indices": ["A", "B", "C", "D", "E", "F"],
                        "cells": self.rows[start - 1 : end],
                        "truncated": False,
                    }
                ],
            }
        )


class CatalogLocatorTest(unittest.TestCase):
    def doc(self):
        return {
            "token": TOKEN,
            "name": "广西由莱-DMR清单-UI04（A1）",
            "url": f"https://example.feishu.cn/sheets/{TOKEN}",
        }

    def locate(self, fake):
        reader = LarkSheetReader("lark-cli", runner=fake)
        return read_live_location(self.doc(), "11UI04AA01", reader=reader, batch_rows=20)

    def test_locates_unique_current_version_cell(self):
        fake = FakeCli()
        result = self.locate(fake)
        self.assertEqual(result["status"], "located")
        self.assertEqual(result["location"]["row"], 38)
        self.assertEqual(result["location"]["code_cell"], "D38")
        self.assertEqual(result["location"]["version_cell"], "E38")
        self.assertEqual(result["location"]["current_version"], "20260410")
        self.assertEqual(result["cloud_writes"], 0)
        self.assertTrue(all(command[2] in {"+workbook-info", "+cells-get"} for command in fake.commands))

    def test_locates_row_after_inserted_rows(self):
        result = self.locate(FakeCli(sheet_rows(moved=True)))
        self.assertEqual(result["status"], "located")
        self.assertEqual(result["location"]["row"], 40)
        self.assertEqual(result["location"]["version_cell"], "E40")

    def test_duplicate_code_is_ambiguous(self):
        result = self.locate(FakeCli(sheet_rows(duplicate=True)))
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(result["entry_status"], "multiple")

    def test_ambiguous_header_blocks_guessed_location(self):
        result = self.locate(FakeCli(sheet_rows(header_ambiguous=True)))
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("表头不唯一", result["message"])
        self.assertEqual(result["matches"], [])

    def test_missing_code_returns_not_found(self):
        reader = LarkSheetReader("lark-cli", runner=FakeCli())
        result = read_live_location(self.doc(), "NOT-EXIST", reader=reader, batch_rows=20)
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["entry_status"], "missing")
        self.assertIn("编号或名称是否变更", result["message"])

    def test_read_failure_returns_safe_status(self):
        result = self.locate(FakeCli(fail_cells=True))
        self.assertEqual(result["status"], "read_failed")
        self.assertEqual(result["message"], "当前账号没有读取这份清单的权限")
        self.assertEqual(result["cloud_writes"], 0)

    def test_missing_revision_does_not_claim_live_location(self):
        result = self.locate(FakeCli(revision=None))
        self.assertEqual(result["status"], "read_failed")
        self.assertNotIn("location", result)

    def test_revision_change_fails_instead_of_returning_stale_cell(self):
        result = self.locate(FakeCli(second_revision=10))
        self.assertEqual(result["status"], "read_failed")
        self.assertIn("被他人修改", result["message"])


if __name__ == "__main__":
    unittest.main()

class GlobalCodeHitTests(unittest.TestCase):
    def test_ambiguous_header_still_reports_exact_code_candidate(self):
        cli = FakeCli(sheet_rows(header_ambiguous=True))
        result = read_live_location({'token':TOKEN}, '11UI04AA01', reader=LarkSheetReader('lark-cli', runner=cli))
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['code_hits'][0]['code_cell'], 'D38')
        self.assertNotIn('location', result)

    def test_no_header_code_hit_never_fabricates_version(self):
        rows=sheet_rows()
        rows[7]=[cell('') for _ in range(6)]
        result=read_live_location({'token':TOKEN}, '11UI04AA01', reader=LarkSheetReader('lark-cli', runner=FakeCli(rows)))
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['code_hits'][0]['code_cell'], 'D38')
        self.assertNotIn('location', result)
