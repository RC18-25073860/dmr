"""Explicit opt-in HTTP upload example. Does nothing without --send; never reads mail."""
import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError

from backend.mail_ingest import MailAttachmentIngestRequest


class MetadataError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a credential to a redirect target.


TOP_LEVEL_FIELDS = {
    "mailbox",
    "message_id",
    "subject",
    "sender",
    "received_at",
    "biz_keyword",
    "test_case",
}
ATTACHMENT_METADATA_FIELDS = {"attachment_id", "content_type"}


def build_payload(file_path: Path, data: bytes, metadata: dict):
    if not isinstance(metadata, dict):
        raise MetadataError("metadata JSON must be an object")
    unknown = sorted(set(metadata) - TOP_LEVEL_FIELDS - ATTACHMENT_METADATA_FIELDS)
    if unknown:
        raise MetadataError("Unknown metadata field(s): " + ", ".join(unknown))
    attachment_id = metadata.get("attachment_id")
    payload = {key: metadata[key] for key in TOP_LEVEL_FIELDS if key in metadata}
    attachment = {
        "attachment_id": attachment_id,
        "filename": file_path.name,
        "content_base64": base64.b64encode(data).decode(),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if "content_type" in metadata:
        attachment["content_type"] = metadata["content_type"]
    payload["attachment"] = attachment
    return payload


def validate_payload(payload: dict):
    try:
        MailAttachmentIngestRequest.model_validate(payload)
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_input=False, include_context=False):
            field = ".".join(str(part) for part in error.get("loc", ())) or "<payload>"
            details.append(f"- {field}: {error.get('msg')} ({error.get('type')})")
        raise SystemExit("Payload does not match DMR mail ingest schema:\n" + "\n".join(details)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--metadata", type=Path, required=True,
                        help="JSON containing mailbox/message_id/subject/sender/received_at and attachment_id")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    meta = json.loads(args.metadata.read_text(encoding="utf-8"))
    data = args.file.read_bytes()
    if not data or len(data) > 50 * 1024 * 1024:
        parser.error("File must be nonempty and at most 50 MiB")
    try:
        payload = build_payload(args.file, data, meta)
    except MetadataError as exc:
        parser.error(str(exc))
    validate_payload(payload)
    if not args.send:
        print(json.dumps({"sent": False, "filename": args.file.name, "size": len(data),
                          "message_id": payload.get("message_id"),
                          "attachment_id": payload["attachment"].get("attachment_id"),
                          "content_type": payload["attachment"].get("content_type")}, ensure_ascii=False))
        return
    url = os.environ.get("DMR_MAIL_BASE_URL", "").rstrip("/")
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        parser.error("Use a base origin without credentials/path/query")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
        parser.error("HTTPS is required except local loopback testing")
    token = os.environ.get("DMR_MAIL_TOKEN", "")
    if not token:
        parser.error("DMR_MAIL_TOKEN is required; never put it in the metadata JSON")
    request = Request(url + "/api/integrations/mail/attachments",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=120) as response:
            print(response.status, response.read().decode())
    except HTTPError as exc:
        print(exc.code, exc.read().decode())
        raise SystemExit(1)

if __name__ == "__main__":
    main()
