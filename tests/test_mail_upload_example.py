import base64
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_RUNTIME = ROOT / "tests" / ".runtime"
TEST_RUNTIME.mkdir(parents=True, exist_ok=True)

from scripts import mail_upload_example


class MailUploadExampleTests(unittest.TestCase):
    def write_file(self, directory, name, data):
        path = Path(directory) / name
        path.write_bytes(data)
        return path

    def write_metadata(self, directory, **overrides):
        metadata = {
            "mailbox": "operator@example.com",
            "message_id": "message-001",
            "subject": "【建筑】附件下载测试",
            "sender": "operator@example.com",
            "received_at": "2026-09-22T17:58:37+08:00",
            "attachment_id": "attachment-001",
            "content_type": "text/plain",
            "biz_keyword": "建筑",
            "test_case": "DMR-MAIL-20260922-01",
        }
        metadata.update(overrides)
        path = Path(directory) / "metadata.json"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        return path

    def test_build_payload_puts_content_type_inside_attachment(self):
        data = b"hello dmr"
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            metadata = json.loads(self.write_metadata(tmp).read_text(encoding="utf-8"))

            payload = mail_upload_example.build_payload(Path("sample.txt"), data, metadata)

        self.assertNotIn("content_type", payload)
        self.assertEqual(payload["attachment"]["content_type"], "text/plain")
        self.assertEqual(payload["attachment"]["size"], len(data))
        self.assertEqual(payload["attachment"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(payload["attachment"]["content_base64"], base64.b64encode(data).decode())
        mail_upload_example.validate_payload(payload)

    def test_build_payload_rejects_unknown_metadata_field_before_validation(self):
        data = b"hello dmr"
        secret_value = "SECRET_BASE64_SHOULD_NOT_APPEAR"
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            metadata = json.loads(self.write_metadata(tmp, sha256=secret_value, attachment=secret_value).read_text(encoding="utf-8"))

        with self.assertRaisesRegex(mail_upload_example.MetadataError, "attachment, sha256") as raised:
            mail_upload_example.build_payload(Path("sample.txt"), data, metadata)
        self.assertNotIn(secret_value, str(raised.exception))

    def test_build_payload_rejects_non_object_metadata(self):
        with self.assertRaisesRegex(mail_upload_example.MetadataError, "must be an object"):
            mail_upload_example.build_payload(Path("sample.txt"), b"hello dmr", ["not", "an", "object"])

    def test_validate_payload_error_does_not_echo_input_values(self):
        data = b"SECRET_BASE64_SHOULD_NOT_APPEAR"
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            metadata = json.loads(self.write_metadata(tmp, received_at="not-a-date").read_text(encoding="utf-8"))
        payload = mail_upload_example.build_payload(Path("sample.txt"), data, metadata)

        with self.assertRaisesRegex(SystemExit, "received_at") as raised:
            mail_upload_example.validate_payload(payload)
        message = str(raised.exception)
        self.assertNotIn("not-a-date", message)
        self.assertNotIn(base64.b64encode(data).decode(), message)

    def test_dry_run_validates_and_never_opens_network(self):
        with tempfile.TemporaryDirectory(dir=TEST_RUNTIME) as tmp:
            data_path = self.write_file(tmp, "sample.txt", b"hello dmr")
            metadata_path = self.write_metadata(tmp)

            with patch.object(mail_upload_example, "build_opener") as opener:
                out = io.StringIO()
                with redirect_stdout(out):
                    mail_upload_example.main([str(data_path), "--metadata", str(metadata_path)])

            opener.assert_not_called()
            result = json.loads(out.getvalue())
            self.assertFalse(result["sent"])
            self.assertEqual(result["filename"], "sample.txt")
            self.assertEqual(result["content_type"], "text/plain")


if __name__ == "__main__":
    unittest.main()
