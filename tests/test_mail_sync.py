import argparse
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
TEST_RUNTIME = ROOT / "tests" / ".runtime"
TEST_RUNTIME.mkdir(parents=True, exist_ok=True)

from scripts import mail_sync
from test_mail_ingest import MailIngestHarness


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def run(self, argv):
        self.commands.append(argv)
        if not self.responses:
            raise AssertionError("unexpected command: " + " ".join(argv))
        response = self.responses.pop(0)
        if isinstance(response, mail_sync.CliResult):
            return response
        return mail_sync.CliResult(0, json.dumps(response), "")


class FakeDownloader:
    def __init__(self, writers):
        self.writers = list(writers)
        self.calls = []

    def download(self, url, dest, timeout):
        self.calls.append((url, dest, timeout))
        if not self.writers:
            raise AssertionError("unexpected download")
        self.writers.pop(0)(dest)


class FakeUploader:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def post_json(self, url, payload):
        self.calls.append((url, payload))
        if self.error:
            raise self.error
        if self.responses:
            return self.responses.pop(0)
        receipts = [
            {"receipt_id": f"MAILRCPT-{index}", "batch_id": "UPLOAD-1", "item_id": f"ITEM-{index}", "status": "needs_review"}
            for index, _ in enumerate(payload.get("attachments", []), start=1)
        ]
        return 201, {"batch_id": "UPLOAD-1", "receipts": receipts}


class BackendUploader:
    def __init__(self, client, token):
        self.client = client
        self.token = token
        self.calls = []

    def post_json(self, url, payload):
        self.calls.append((url, payload))
        path = urlsplit(url).path
        response = self.client.post(path, json=payload, headers={"Authorization": "Bearer " + self.token})
        return response.status_code, response.json()


def ok_messages():
    return {
        "ok": True,
        "data": {"messages": [
            {"message_id": "msg-1", "subject": "建筑 BOM 更新"},
        ]},
    }


def ok_details():
    return {
        "ok": True,
        "data": {"messages": [
            {
                "message_id": "msg-1",
                "subject": "建筑 BOM 更新",
                "internal_date": 1790213146000,
                "head_from": {"mail_address": "operator@example.com"},
                "attachments": [{"id": "att-1", "filename": "bom.zip", "is_inline": False}],
            }
        ]},
    }


def ok_details_two_attachments():
    return {
        "ok": True,
        "data": {"messages": [
            {
                "message_id": "msg-1",
                "subject": "建筑 BOM 更新",
                "internal_date": 1790213146000,
                "head_from": {"mail_address": "operator@example.com"},
                "attachments": [
                    {"id": "att-1", "filename": "bom-a.xlsx", "is_inline": False},
                    {"id": "att-2", "filename": "bom-b.xlsx", "is_inline": False},
                ],
            }
        ]},
    }


def ok_download_url():
    return {"ok": True, "data": {"download_urls": [{"download_url": "https://download.example/att"}]}}


def write_zip(dest):
    with zipfile.ZipFile(dest, "w") as archive:
        archive.writestr("bom-a.xlsx", b"bom-a")
        archive.writestr("bom-b.xlsx", b"bom-b")


def write_file(dest):
    dest.write_bytes(b"plain bom")


def write_second_file(dest):
    dest.write_bytes(b"second bom")


def fail_download(_dest):
    raise mail_sync.DownloadError("download timeout")


class MailSyncTests(unittest.TestCase):
    def test_transfer_filename_bypasses_normal_attachment_pipeline(self):
        filename = 'DMR文件转移清单（BW-2026100801-深圳由莱）.xlsx'
        details = {"ok": True, "data": {"messages": [{
            "message_id": "transfer-existing-query", "subject": "建筑资料",
            "internal_date": 1790213146000,
            "head_from": {"mail_address": "operator@example.com"},
            "attachments": [{"id": "att-1", "filename": filename, "is_inline": False}],
        }]}}
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            runner = FakeRunner([{"ok": True, "data": {"messages": [{"message_id": "transfer-existing-query", "subject": "建筑资料"}]}}, details, ok_download_url()])
            uploader = FakeUploader(responses=[(201, {"archive_status": "archived", "receipt_id": "TRANSFER-1"})])
            result = mail_sync.MailSync(self.config(tmp), runner=runner, downloader=FakeDownloader([write_file]),
                                        uploader=uploader, log=lambda _: None).run()
            self.assertEqual(result.uploaded_files, 1)
            self.assertTrue(uploader.calls[0][0].endswith('/api/integrations/mail/transfer-manifests'))

    def test_transfer_manifest_keyword_routes_only_matching_filename_and_retries_until_archived(self):
        filename = 'DMR文件转移清单（BW-2026100801-深圳由莱）.xlsx'
        triage_empty = {"ok": True, "data": {"messages": []}}
        triage_transfer = {"ok": True, "data": {"messages": [{"message_id": "transfer-1", "subject": "转移资料"}]}}
        details = {"ok": True, "data": {"messages": [{
            "message_id": "transfer-1", "subject": "转移资料", "internal_date": 1790213146000,
            "head_from": {"mail_address": "operator@example.com"},
            "attachments": [{"id": "att-1", "filename": filename, "is_inline": False}],
        }]}}
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp, transfer_manifest_keywords=("转移",))
            runner = FakeRunner([triage_empty, triage_transfer, details, ok_download_url()])
            downloader = FakeDownloader([write_file])
            uploader = FakeUploader(responses=[(503, {"error": "192不可用"})])
            failed = mail_sync.MailSync(config, runner=runner, downloader=downloader, uploader=uploader, log=lambda _: None).run()
            self.assertTrue(failed.failed())
            self.assertEqual(json.loads(config.state_file.read_text())["done"], [])
            self.assertTrue(uploader.calls[0][0].endswith('/api/integrations/mail/transfer-manifests'))

            retry_runner = FakeRunner([triage_empty, triage_transfer, details, ok_download_url()])
            retry_uploader = FakeUploader(responses=[(201, {"receipt_id": "TRANSFER-1", "archive_status": "archived", "status": "archived"})])
            successful = mail_sync.MailSync(config, runner=retry_runner, downloader=FakeDownloader([write_file]),
                                            uploader=retry_uploader, log=lambda _: None).run()
            self.assertEqual(successful.uploaded_files, 1)
            self.assertEqual(json.loads(config.state_file.read_text())["done"], ["transfer-1:att-1"])

    def test_intake_only_stages_transfer_manifest_without_archive_request(self):
        filename = 'DMR文件转移清单（BW-2026100801-深圳由莱）.xlsx'
        details = {"ok": True, "data": {"messages": [{
            "message_id": "transfer-stage", "subject": "BOM发放资料",
            "internal_date": 1790213146000,
            "head_from": {"mail_address": "operator@example.com"},
            "attachments": [{"id": "att-1", "filename": filename, "is_inline": False}],
        }]}}
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp, intake_only=True, include_keywords=("BOM",))
            runner = FakeRunner([{"ok": True, "data": {"messages": [{"message_id": "transfer-stage", "subject": "BOM发放资料"}]}}, details, ok_download_url()])
            uploader = FakeUploader(responses=[(202, {"archive_status": "staged", "receipt_id": "TRANSFER-1"})])
            stats = mail_sync.MailSync(config, runner=runner, downloader=FakeDownloader([write_file]),
                                       uploader=uploader, log=lambda _: None).run()
            self.assertEqual(stats.uploaded_files, 1)
            self.assertTrue(uploader.calls[0][0].endswith('?stage_only=true'))
            self.assertEqual(json.loads(config.state_file.read_text())["done"], ["transfer-stage:att-1"])

    def test_cli_command_has_timeout(self):
        import subprocess
        with patch('scripts.mail_sync.subprocess.run', side_effect=subprocess.TimeoutExpired(['lark-cli'], 120)) as run:
            with self.assertRaisesRegex(mail_sync.CliError, 'timed out'):
                mail_sync.CommandRunner().run(['lark-cli', 'mail', 'triage'])
        self.assertEqual(run.call_args.kwargs['timeout'], 120)

    def config(self, tmp, **overrides):
        base = {
            "dmr_base": "http://localhost:63870",
            "dmr_token": "test-token",
            "mailbox": "operator@example.com",
            "state_file": Path(tmp) / "processed.json",
            "include_keywords": ("建筑",),
        }
        base.update(overrides)
        return mail_sync.MailSyncConfig(**base)

    def test_triage_uses_filter_not_unsupported_folder_and_dry_run_does_not_save_state(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp, dry_run=True)
            runner = FakeRunner([ok_messages(), ok_details(), ok_download_url()])
            downloader = FakeDownloader([write_file])
            uploader = FakeUploader()

            stats = mail_sync.MailSync(config, runner=runner, downloader=downloader, uploader=uploader, log=lambda _: None).run()

            triage = runner.commands[0]
            self.assertIn("--filter", triage)
            self.assertNotIn("--folder", triage)
            self.assertIn('"folder": "SENT"', triage[triage.index("--filter") + 1])
            self.assertEqual(stats.dry_run_files, 1)
            self.assertFalse(config.state_file.exists())
            self.assertEqual(uploader.calls, [])

    def test_triage_follows_page_tokens_above_single_page_limit(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp, dry_run=True, triage_max=400)
            runner = FakeRunner([
                {"ok": True, "data": {"messages": [{"message_id": "msg-1", "subject": "建筑 第一页"}], "next_page_token": "page-2"}},
                {"ok": True, "data": {"messages": [{"message_id": "msg-2", "subject": "建筑 第二页"}]}},
            ])

            messages = mail_sync.MailSync(config, runner=runner, log=lambda _: None)._triage_messages()

            self.assertEqual([message["message_id"] for message in messages], ["msg-1", "msg-2"])
            self.assertNotIn("--page-token", runner.commands[0])
            self.assertIn("--page-token", runner.commands[1])
            self.assertEqual(runner.commands[1][runner.commands[1].index("--page-token") + 1], "page-2")

    def test_triage_rejects_repeated_page_token_and_missing_token_when_has_more(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp, dry_run=True, triage_max=400)
            repeated = FakeRunner([
                {"ok": True, "data": {"messages": [{"message_id": "msg-1", "subject": "建筑 第一页"}], "next_page_token": "same"}},
                {"ok": True, "data": {"messages": [{"message_id": "msg-2", "subject": "建筑 第二页"}], "next_page_token": "same"}},
            ])
            with self.assertRaisesRegex(mail_sync.CliError, "repeated page token"):
                mail_sync.MailSync(config, runner=repeated, log=lambda _: None)._triage_messages()

            missing = FakeRunner([
                {"ok": True, "data": {"messages": [{"message_id": "msg-1", "subject": "建筑 第一页"}], "has_more": True}},
            ])
            with self.assertRaisesRegex(mail_sync.CliError, "returned no page token"):
                mail_sync.MailSync(config, runner=missing, log=lambda _: None)._triage_messages()

    def test_batch_upload_marks_each_child_file_and_legacy_done_source(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp)
            runner = FakeRunner([ok_messages(), ok_details(), ok_download_url()])
            downloader = FakeDownloader([write_zip])
            uploader = FakeUploader()

            stats = mail_sync.MailSync(config, runner=runner, downloader=downloader, uploader=uploader, log=lambda _: None).run()

            self.assertEqual(stats.uploaded_files, 2)
            self.assertEqual(len(uploader.calls), 1)
            url, payload = uploader.calls[0]
            self.assertEqual(url, "http://localhost:63870/api/integrations/mail/batches")
            self.assertEqual(len(payload["attachments"]), 2)
            self.assertEqual(payload["message_id"], "msg-1")
            state = json.loads(config.state_file.read_text(encoding="utf-8"))
            self.assertIn("msg-1:att-1", state["done"])
            self.assertEqual(len(state["files"]), 2)
            self.assertTrue(all(item["status"] == "uploaded" for item in state["files"].values()))

    def test_existing_legacy_done_source_skips_attachment_without_download(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            state_file = Path(tmp) / "processed.json"
            state_file.write_text(json.dumps({"done": ["msg-1:att-1"]}), encoding="utf-8")
            config = self.config(tmp, state_file=state_file)
            runner = FakeRunner([ok_messages(), ok_details()])
            downloader = FakeDownloader([])
            uploader = FakeUploader()

            stats = mail_sync.MailSync(config, runner=runner, downloader=downloader, uploader=uploader, log=lambda _: None).run()

            self.assertEqual(stats.planned_files, 0)
            self.assertEqual(downloader.calls, [])
            self.assertEqual(uploader.calls, [])

    def test_failed_upload_does_not_mark_file_or_source_done(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp)
            runner = FakeRunner([ok_messages(), ok_details(), ok_download_url()])
            downloader = FakeDownloader([write_file])
            uploader = FakeUploader(error=mail_sync.MailSyncError("DMR unavailable"))

            stats = mail_sync.MailSync(config, runner=runner, downloader=downloader, uploader=uploader, log=lambda _: None).run()

            self.assertTrue(stats.failed())
            state = json.loads(config.state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["done"], [])
            self.assertEqual(state["files"], {})

    def test_message_batch_blocks_all_attachments_when_one_download_fails_then_retries_together(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            state_file = Path(tmp) / "processed.json"
            config = self.config(tmp, state_file=state_file)
            first_runner = FakeRunner([ok_messages(), ok_details_two_attachments(), ok_download_url(), ok_download_url()])
            first_downloader = FakeDownloader([write_file, fail_download])
            first_uploader = FakeUploader()

            first_stats = mail_sync.MailSync(
                config, runner=first_runner, downloader=first_downloader, uploader=first_uploader, log=lambda _: None
            ).run()

            self.assertTrue(first_stats.failed())
            self.assertEqual(first_stats.uploaded_files, 0)
            self.assertEqual(first_uploader.calls, [])
            first_state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(first_state["done"], [])
            self.assertEqual(first_state["files"], {})

            harness = MailIngestHarness()
            try:
                admin, _admin_user = harness.setup_admin()
                mail_client = harness.create_mail_client(admin, mailbox="operator@example.com")
                client = harness.client()
                retry_runner = FakeRunner([ok_messages(), ok_details_two_attachments(), ok_download_url(), ok_download_url()])
                retry_downloader = FakeDownloader([write_file, write_second_file])
                retry_uploader = BackendUploader(client, mail_client["bearer_token"])
                retry_config = self.config(tmp, state_file=state_file, dmr_token=mail_client["bearer_token"])

                retry_stats = mail_sync.MailSync(
                    retry_config, runner=retry_runner, downloader=retry_downloader, uploader=retry_uploader, log=lambda _: None
                ).run()

                self.assertEqual(retry_stats.uploaded_files, 2)
                self.assertEqual(len(retry_uploader.calls), 1)
                _url, payload = retry_uploader.calls[0]
                self.assertEqual(len(payload["attachments"]), 2)
                receipts = harness.store.execute("SELECT batch_id,attachment_id FROM mail_receipts ORDER BY attachment_id").fetchall()
                self.assertEqual(len(receipts), 2)
                self.assertEqual(len({row["batch_id"] for row in receipts}), 1)
                batch = admin.get(f"/api/batches/{receipts[0]['batch_id']}")
                self.assertEqual(batch.status_code, 200, batch.text)
                self.assertEqual(len(batch.json()["items"]), 2)
                retry_state = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(retry_state["done"], ["msg-1:att-1", "msg-1:att-2"])
                self.assertEqual(len(retry_state["files"]), 2)
            finally:
                harness.close()

    def test_attachment_grouping_blocks_only_the_failed_attachment(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config = self.config(tmp, batch_grouping="attachment")
            runner = FakeRunner([ok_messages(), ok_details_two_attachments(), ok_download_url(), ok_download_url()])
            downloader = FakeDownloader([write_file, fail_download])
            uploader = FakeUploader()

            stats = mail_sync.MailSync(config, runner=runner, downloader=downloader, uploader=uploader, log=lambda _: None).run()

            self.assertTrue(stats.failed())
            self.assertEqual(stats.uploaded_files, 1)
            self.assertEqual(len(uploader.calls), 1)
            _url, payload = uploader.calls[0]
            self.assertEqual(len(payload["attachments"]), 1)
            state = json.loads(config.state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["done"], ["msg-1:att-1"])
            self.assertEqual(len(state["files"]), 1)

    def test_zip_traversal_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            tmp_path = Path(tmp)
            traversal = tmp_path / "bad.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../escape.txt", b"bad")
            with self.assertRaises(mail_sync.ArchiveSafetyError):
                mail_sync.safe_extract(traversal, tmp_path / "out1", self.config(tmp))

            symlink = tmp_path / "symlink.zip"
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120777 << 16)
            with zipfile.ZipFile(symlink, "w") as archive:
                archive.writestr(info, "target")
            with self.assertRaises(mail_sync.ArchiveSafetyError):
                mail_sync.safe_extract(symlink, tmp_path / "out2", self.config(tmp))

    def test_cli_failure_uses_exit_code_and_ok_false(self):
        with self.assertRaisesRegex(mail_sync.CliError, "exit 1"):
            mail_sync.parse_json_envelope(mail_sync.CliResult(1, "", json.dumps({"ok": False, "error": {"message": "missing scope"}})))
        with self.assertRaisesRegex(mail_sync.CliError, "ok:false"):
            mail_sync.parse_json_envelope(mail_sync.CliResult(0, json.dumps({"ok": False, "error": {"message": "bad"}}), ""))

    def test_env_config_file_is_read_from_dmr_mail_config(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            config_path = Path(tmp) / "mail.json"
            state_path = Path(tmp) / "state.json"
            config_path.write_text(json.dumps({
                "dmr_base": "http://dmr.internal:8920",
                "dmr_token": "json-token",
                "mailbox": "dcc@example.test",
                "include_keywords": ["建筑", "BOM"],
                "exclude_keywords": ["作废"],
                "state_file": str(state_path),
            }), encoding="utf-8")
            args = argparse.Namespace(
                config=None, state_file=None, keyword=None, exclude_keyword=None,
                delivery_mode=None, batch_grouping=None, token=None, dmr_base=None,
                mailbox=None, folder=None, dry_run=False,
            )

            with patch.dict(os.environ, {"DMR_MAIL_CONFIG": str(config_path)}, clear=True):
                config = mail_sync.MailSyncConfig.from_sources(args)

            self.assertEqual(config.dmr_base, "http://dmr.internal:8920")
            self.assertEqual(config.dmr_token, "json-token")
            self.assertEqual(config.mailbox, "dcc@example.test")
            self.assertEqual(config.include_keywords, ("建筑", "BOM"))
            self.assertEqual(config.exclude_keywords, ("作废",))
            self.assertEqual(config.state_file, state_path)
            self.assertEqual(config.triage_max, 400)

    def test_deploy_service_points_to_dmr_mail_config_env(self):
        unit = (ROOT / "deploy" / "dmr-mail.service").read_text(encoding="utf-8")
        self.assertIn("Environment=DMR_MAIL_CONFIG=/etc/dmr/mail.json", unit)
        self.assertIn("ExecStart=/opt/dmr/.venv/bin/python /opt/dmr/scripts/mail_sync.py", unit)
        self.assertIn("--intake-only", unit)

    def test_script_batch_payload_is_accepted_by_real_backend_and_retries_idempotently(self):
        harness = MailIngestHarness()
        try:
            admin, _admin_user = harness.setup_admin()
            mail_client = harness.create_mail_client(admin, mailbox="operator@example.com")
            client = harness.client()
            with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
                config = self.config(tmp, dmr_token=mail_client["bearer_token"])
                runner = FakeRunner([ok_messages(), ok_details(), ok_download_url()])
                downloader = FakeDownloader([write_zip])
                uploader = BackendUploader(client, mail_client["bearer_token"])

                stats = mail_sync.MailSync(config, runner=runner, downloader=downloader, uploader=uploader, log=lambda _: None).run()

                self.assertEqual(stats.uploaded_files, 2)
                self.assertEqual(len(uploader.calls), 1)
                _url, payload = uploader.calls[0]
                self.assertEqual(len(payload["attachments"]), 2)
                receipts = sorted(harness.store.execute(
                    "SELECT batch_id,attachment_id FROM mail_receipts ORDER BY attachment_id"
                ).fetchall(), key=lambda row: row["attachment_id"])
                self.assertEqual(len(receipts), 2)
                self.assertEqual(len({row["batch_id"] for row in receipts}), 1)
                self.assertEqual([row["attachment_id"] for row in receipts], [
                    "att-1/73123bf92d10c4c14453c48c86be80e131ec0dd5041347e5564bf50a7939f11e_bom_unzipped/bom-a.xlsx",
                    "att-1/73123bf92d10c4c14453c48c86be80e131ec0dd5041347e5564bf50a7939f11e_bom_unzipped/bom-b.xlsx",
                ])
                batch_id = receipts[0]["batch_id"]
                batch = admin.get(f"/api/batches/{batch_id}")
                self.assertEqual(batch.status_code, 200, batch.text)
                self.assertEqual(len(batch.json()["items"]), 2)

                retry_status, retry_body = uploader.post_json(config.ingest_batches_url, payload)
                self.assertEqual(retry_status, 200, retry_body)
                self.assertEqual(retry_body["batch_id"], batch_id)
                self.assertEqual(len(retry_body["receipts"]), 2)
                self.assertEqual(harness.store.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"], 1)
                self.assertEqual(harness.store.execute("SELECT COUNT(*) AS n FROM mail_receipts").fetchone()["n"], 2)
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
