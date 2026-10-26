from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nas_agent.config import NasAgentConfig, ROOT
from nas_agent.platform import PlatformClient
from nas_agent.reader import ReadService, create_reader_app, safe_read_error_message
from nas_agent.synology import DeliveryError, FileStation
from nas_agent.worker import NasWorker
from backend.config import AppConfig
from backend.identity import IdentityService
from backend.nas import READ_BASE_URL_ENV, SYNC_TOKEN_ENV, install_nas
from backend.service import DMRStore, sha256_bytes


def config(tmp_path: Path) -> NasAgentConfig:
    safe_temp = ROOT / ".runtime" / "pytest-nas-agent" / hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    safe_temp.mkdir(parents=True, exist_ok=True)
    return NasAgentConfig(
        platform_base_url="https://dmr.example.test/api/",
        worker_token="worker-token",
        synology_base_url="https://nas.example.test/",
        temp_dir=safe_temp,
    ).validated()


def test_config_requires_https_and_920_runtime(tmp_path):
    with pytest.raises(ValueError, match="https"):
        NasAgentConfig("http://dmr.local/api/", "token", "https://nas.local/", temp_dir=tmp_path).validated()
    with pytest.raises(ValueError, match="inside"):
        NasAgentConfig("https://dmr.local/api/", "token", "https://nas.local/", temp_dir=Path("/tmp/outside")).validated()
    with pytest.raises(ValueError, match="TLS"):
        NasAgentConfig("https://dmr.local/api/", "token", "https://nas.local/", temp_dir=tmp_path, synology_verify_tls=False).validated()
    valid = NasAgentConfig("https://dmr.local/api", "token", "https://nas.local", temp_dir=ROOT / ".runtime" / "nas-test").validated()
    assert valid.platform_base_url == "https://dmr.local/api/"
    assert valid.synology_base_url == "https://nas.local/"


def test_platform_client_uses_storage_sync_prefix_and_bearer_token():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization"), request.content))
        return httpx.Response(200, json=[{"id": "job"}])

    client = httpx.Client(base_url="https://dmr.example.test/api/", transport=httpx.MockTransport(handler))
    platform = PlatformClient("https://dmr.example.test/api/", "secret-token", client)
    assert platform.post("claim", {"limit": 2}) == [{"id": "job"}]
    assert calls == [("POST", "/api/storage-sync/claim", "Bearer secret-token", b'{"limit":2}')]


def test_filestation_personal_session_upload_is_idempotent_and_destination_is_immutable(tmp_path):
    stored: dict[str, bytes] = {}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("query.cgi"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        name: {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7 if name == "SYNO.API.Auth" else 3}
                        for name in request.url.params["query"].split(",")
                    },
                },
            )
        raw = request.read()
        if b'filename=".uploading-' in raw:
            assert b'name="overwrite"\r\n\r\noverwrite' in raw
            assert b'name="create_parents"\r\n\r\nfalse' in raw
            assert b'name="_sid"\r\n\r\npersonal-session' in raw
            marker = b'filename="'
            filename = raw.split(marker, 1)[1].split(b'"', 1)[0].decode()
            stored["/dmr/UI06S/BOM/" + filename] = b"hello" if b"hello" in raw else b"WRONG"
            calls.append("upload")
            return httpx.Response(200, json={"success": True})
        fields = {key: value[0] for key, value in parse_qs(raw.decode()).items()}
        method = fields["method"]
        calls.append(method)
        if method == "login":
            assert fields["account"] == "rui-rui" and fields["passwd"] == "private-password"
            return httpx.Response(200, json={"success": True, "data": {"sid": "personal-session"}})
        assert fields["_sid"] == "personal-session"
        if method == "getinfo":
            path = json.loads(fields["path"])[0]
            if path not in stored:
                return httpx.Response(200, json={"success": False, "error": {"code": 408}})
            return httpx.Response(
                200,
                json={"success": True, "data": {"files": [{"isdir": False, "additional": {"size": len(stored[path])}}]}},
            )
        if method == "download":
            path = json.loads(fields["path"])[0]
            if path not in stored:
                return httpx.Response(200, json={"success": False, "error": {"code": 408}})
            return httpx.Response(200, content=stored[path], headers={"content-disposition": "attachment"})
        if method == "rename":
            path = json.loads(fields["path"])[0]
            name = json.loads(fields["name"])[0]
            stored[path.rsplit("/", 1)[0] + "/" + name] = stored.pop(path)
        return httpx.Response(200, json={"success": True, "data": {}})

    local = tmp_path / "payload.bin"
    local.write_bytes(b"hello")
    expected_sha = hashlib.sha256(b"hello").hexdigest()
    with httpx.Client(base_url="https://nas.example.test/", transport=httpx.MockTransport(handler)) as http:
        nas = FileStation(http)
        nas.login("rui-rui", "private-password")
        nas.deliver(local, "/dmr/UI06S/BOM/report.xlsx", 5, expected_sha256=expected_sha, temp_name=".uploading-job-a", scratch_dir=tmp_path)
        nas.deliver(local, "/dmr/UI06S/BOM/report.xlsx", 5, expected_sha256=expected_sha, temp_name=".uploading-job-a", scratch_dir=tmp_path)
        stored["/dmr/UI06S/BOM/wrong.xlsx"] = b"HELLO"
        with pytest.raises(DeliveryError, match="conflict"):
            nas.deliver(local, "/dmr/UI06S/BOM/wrong.xlsx", 5, expected_sha256=expected_sha, temp_name=".uploading-job-wrong", scratch_dir=tmp_path)
        stored["/dmr/UI06S/BOM/.uploading-job-stale"] = b"STALE"
        nas.deliver(local, "/dmr/UI06S/BOM/stale.xlsx", 5, expected_sha256=expected_sha, temp_name=".uploading-job-stale", scratch_dir=tmp_path)
        nas.deliver(local, "/dmr/UI06S/BOM/a.xlsx", 5, expected_sha256=expected_sha, temp_name=".uploading-job-a2", scratch_dir=tmp_path)
        nas.deliver(local, "/dmr/UI06S/BOM/b.xlsx", 5, expected_sha256=expected_sha, temp_name=".uploading-job-b2", scratch_dir=tmp_path)
        with pytest.raises(DeliveryError, match="conflict"):
            nas.deliver(local, "/dmr/UI06S/BOM/report.xlsx", 6, expected_sha256=expected_sha, temp_name=".uploading-job-size", scratch_dir=tmp_path)
        for invalid in ("/", "relative/report.xlsx", "/dmr/../other/report.xlsx"):
            with pytest.raises(DeliveryError, match="permission"):
                nas.destination(invalid)
        nas.logout()
    assert calls.count("upload") == 4
    assert stored["/dmr/UI06S/BOM/report.xlsx"] == b"hello"
    assert stored["/dmr/UI06S/BOM/stale.xlsx"] == b"hello"
    assert stored["/dmr/UI06S/BOM/a.xlsx"] == b"hello"
    assert stored["/dmr/UI06S/BOM/b.xlsx"] == b"hello"


def test_filestation_create_folder_uses_official_createfolder_without_recursive_parent():
    dirs = {"/体系文件", "/体系文件/2、首次量产BOM"}
    files = {"/体系文件/2、首次量产BOM/同名文件"}
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("query.cgi"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        name: {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7 if name == "SYNO.API.Auth" else 3}
                        for name in request.url.params["query"].split(",")
                    },
                },
            )
        fields = {key: value[0] for key, value in parse_qs(request.read().decode()).items()}
        calls.append(fields)
        method = fields["method"]
        if method == "login":
            return httpx.Response(200, json={"success": True, "data": {"sid": "sid"}})
        assert fields["_sid"] == "sid"
        if method == "getinfo":
            path = json.loads(fields["path"])[0]
            if path not in dirs:
                if path in files:
                    return httpx.Response(200, json={"success": True, "data": {"files": [{"isdir": False, "additional": {"size": 3}}]}})
                return httpx.Response(200, json={"success": False, "error": {"code": 408}})
            return httpx.Response(200, json={"success": True, "data": {"files": [{"isdir": True}]}})
        if method == "create":
            assert fields["api"] == "SYNO.FileStation.CreateFolder"
            assert fields["folder_path"] == json.dumps(["/体系文件/2、首次量产BOM"])
            assert fields["name"] == json.dumps(["W20新产品"])
            assert fields["force_parent"] == "false"
            dirs.add("/体系文件/2、首次量产BOM/W20新产品")
        return httpx.Response(200, json={"success": True, "data": {}})

    with httpx.Client(base_url="https://nas.example.test/", transport=httpx.MockTransport(handler)) as http:
        nas = FileStation(http)
        nas.login("owner", "password")
        assert nas.create_folder("/体系文件/2、首次量产BOM", "W20新产品") == "/体系文件/2、首次量产BOM/W20新产品"
        assert nas.create_folder("/体系文件/2、首次量产BOM", "W20新产品") == "/体系文件/2、首次量产BOM/W20新产品"
        with pytest.raises(DeliveryError, match="missing"):
            nas.create_folder("/体系文件/不存在父目录", "子目录")
        with pytest.raises(DeliveryError, match="conflict"):
            nas.create_folder("/体系文件/2、首次量产BOM", "同名文件")
        with pytest.raises(DeliveryError, match="permission"):
            nas.create_folder("/体系文件/2、首次量产BOM", "../bad")
    assert [call["method"] for call in calls].count("create") == 1


@pytest.mark.parametrize("mode", ["success", "already", "hash-mismatch", "credentials-http", "auth"])
def test_worker_file_job_contract_and_secret_redaction(mode, monkeypatch, tmp_path, caplog):
    cfg = config(tmp_path)
    calls: list[str] = []
    outcomes: list[dict] = []
    content = b"hello"
    file_id = str(uuid4())
    job = {
        "id": file_id,
        "file_id": file_id,
        "name": "report.xlsx",
        "lease_token": "lease-token",
        "sha256": hashlib.sha256(b"different" if mode == "hash-mismatch" else content).hexdigest(),
        "target_path": "/dmr/UI06S/BOM/report.xlsx",
    }
    if mode == "already":
        job["size"] = len(content)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer worker-token"
        if request.url.path.endswith("/credentials"):
            if mode == "credentials-http":
                return httpx.Response(403, json={"detail": "secret-password"})
            return httpx.Response(200, json={"account": "document-owner", "password": "secret-password"})
        if request.url.path.endswith("/content"):
            assert request.headers["x-sync-lease"] == "lease-token"
            return httpx.Response(200, content=content, headers={"ETag": hashlib.sha256(content).hexdigest()})
        if request.url.path.endswith("/result"):
            outcomes.append(json.loads(request.content))
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True})

    platform = PlatformClient(cfg.platform_base_url, cfg.worker_token, httpx.Client(base_url=cfg.platform_base_url, transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer worker-token"}))
    worker = NasWorker(cfg, platform)
    temp_names: list[str] = []
    scratch_dirs: list[Path] = []

    class MockNAS:
        def __init__(self, *_):
            self.logged_in = False

        def login(self, account, password):
            assert (account, password) == ("document-owner", "secret-password")
            if mode == "auth":
                raise DeliveryError("auth")
            self.logged_in = True

        def already_delivered(self, target_path, size, *, expected_sha256, scratch):
            assert target_path == "/dmr/UI06S/BOM/report.xlsx" and size == 5
            assert expected_sha256 == hashlib.sha256(content).hexdigest()
            return mode == "already"

        def deliver(self, local, target_path, size, *, expected_sha256, temp_name, scratch_dir):
            assert expected_sha256 == hashlib.sha256(content).hexdigest()
            assert temp_name.startswith(".uploading-")
            assert local.read_bytes() == content
            temp_names.append(temp_name)
            scratch_dirs.append(scratch_dir)
            calls.append("nas-deliver")

        def logout(self):
            calls.append("nas-logout")

    monkeypatch.setattr("nas_agent.worker.FileStation", MockNAS)
    result = worker.run_file_job(job)
    assert result == outcomes[0]
    assert "secret-password" not in caplog.text
    if mode == "success":
        assert result["success"] is True
        assert result["target_path"] == "/dmr/UI06S/BOM/report.xlsx"
        assert result["sha256"] == hashlib.sha256(content).hexdigest()
        assert "nas-deliver" in calls
        worker.run_file_job(job)
        assert len(set(temp_names)) == 2
        assert len(set(scratch_dirs)) == 2
    if mode == "already":
        assert result["success"] is True
        assert "nas-deliver" not in calls
    if mode == "hash-mismatch":
        assert result["success"] is False
        assert result["error_code"] == "conflict"
        assert result["diagnostic"]["stage"] == "content_hash"
    if mode == "credentials-http":
        assert result["success"] is False
        assert result["error_code"] == "credentials"
        assert "content" not in "\n".join(calls)
    if mode == "auth":
        assert result["success"] is False
        assert result["error_code"] == "auth"
    assert not list(tmp_path.rglob("*.part"))


def test_worker_account_check_uses_personal_filestation_without_upload(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    outcomes = []
    check = {"id": "user-1", "lease_token": "check-lease"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/credentials"):
            return httpx.Response(200, json={"account": "reader-user", "password": "check-password"})
        if request.url.path.endswith("/result"):
            outcomes.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    platform = PlatformClient(cfg.platform_base_url, cfg.worker_token, httpx.Client(base_url=cfg.platform_base_url, transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer worker-token"}))
    worker = NasWorker(cfg, platform)
    temp_names: list[str] = []
    scratch_dirs: list[Path] = []

    class MockNAS:
        def __init__(self, *_):
            pass

        def login(self, account, password):
            assert (account, password) == ("reader-user", "check-password")

        def logout(self):
            pass

    monkeypatch.setattr("nas_agent.worker.FileStation", MockNAS)
    assert worker.run_account_check(check)["success"] is True
    assert outcomes == [{"lease_token": "check-lease", "success": True}]


def test_worker_directory_job_creates_folder_and_reports_exact_path(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    outcomes = []
    job = {
        "id": "DIR-JOB-1",
        "record_id": "REC-1",
        "parent_path": "/体系文件/2、首次量产BOM",
        "folder_name": "W20新产品",
        "nas_path": "/体系文件/2、首次量产BOM/W20新产品",
        "lease_token": "dir-lease",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/credentials"):
            return httpx.Response(200, json={"account": "dir-admin", "password": "secret-password"})
        if request.url.path.endswith("/result"):
            outcomes.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    platform = PlatformClient(cfg.platform_base_url, cfg.worker_token, httpx.Client(base_url=cfg.platform_base_url, transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer worker-token"}))
    worker = NasWorker(cfg, platform)

    class MockNAS:
        def __init__(self, *_):
            pass

        def login(self, account, password):
            assert (account, password) == ("dir-admin", "secret-password")

        def create_folder(self, parent_path, folder_name):
            assert parent_path == job["parent_path"]
            assert folder_name == job["folder_name"]
            return job["nas_path"]

        def logout(self):
            pass

    monkeypatch.setattr("nas_agent.worker.FileStation", MockNAS)
    assert worker.run_directory_job(job) == {"lease_token": "dir-lease", "success": True, "nas_path": job["nas_path"]}
    assert outcomes == [{"lease_token": "dir-lease", "success": True, "nas_path": job["nas_path"]}]


def test_worker_directory_credentials_conflict_does_not_post_result_or_login(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    calls = []
    job = {
        "id": "DIR-JOB-409",
        "record_id": "REC-1",
        "parent_path": "/体系文件",
        "folder_name": "旧任务",
        "nas_path": "/体系文件/旧任务",
        "lease_token": "dir-lease",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/credentials"):
            return httpx.Response(409, json={"detail": "记录或目录映射已变化，请重新提交创建"})
        if request.url.path.endswith("/result"):
            raise AssertionError("worker must not post result after platform already failed the job")
        return httpx.Response(200, json={"ok": True})

    platform = PlatformClient(cfg.platform_base_url, cfg.worker_token, httpx.Client(base_url=cfg.platform_base_url, transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer worker-token"}))
    worker = NasWorker(cfg, platform)

    class MockNAS:
        def __init__(self, *_):
            pass

        def login(self, *_):
            raise AssertionError("worker must not log in after credential rejection")

        def logout(self):
            pass

    monkeypatch.setattr("nas_agent.worker.FileStation", MockNAS)
    result = worker.run_directory_job(job)
    assert result["success"] is False
    assert result["error_code"] == "credentials"
    assert calls == ["/api/storage-sync/directory-creation-jobs/DIR-JOB-409/credentials"]


def test_reader_redeems_ticket_and_downloads_with_viewer_credentials(monkeypatch, tmp_path):
    cfg = config(ROOT / ".runtime" / "nas-agent-test-reader")
    calls: list[tuple[str, str, str]] = []

    class FakePlatform:
        def post(self, path, body=None):
            assert path == "read/authorize"
            assert body == {"ticket": "short-ticket"}
            return {
                "account": "viewer-account",
                "password": "viewer-password",
                "path": "/dmr/UI06S/BOM/report.xlsx",
                "size": 5,
                "sha256": hashlib.sha256(b"hello").hexdigest(),
                "name": "report.xlsx",
                "action": "download",
            }

    class MockNAS:
        def __init__(self, *_):
            pass

        def login(self, account, password):
            calls.append(("login", account, password))

        def download(self, source_path, local, expected_size):
            calls.append(("download", source_path, str(expected_size)))
            local.write_bytes(b"hello")

        def logout(self):
            calls.append(("logout", "", ""))

    monkeypatch.setattr("nas_agent.reader.FileStation", MockNAS)
    service = ReadService(cfg, FakePlatform())
    temporary, local, job = service.fetch_to_temp("short-ticket", temp_root=tmp_path)
    try:
        assert local.read_bytes() == b"hello"
        assert job["name"] == "report.xlsx"
        assert "password" not in job
        assert calls == [
            ("login", "viewer-account", "viewer-password"),
            ("download", "/dmr/UI06S/BOM/report.xlsx", "5"),
            ("logout", "", ""),
        ]
    finally:
        temporary.cleanup()


def test_reader_rejects_same_size_wrong_hash_download(monkeypatch, tmp_path):
    cfg = config(ROOT / ".runtime" / "nas-agent-test-reader-hash")

    class FakePlatform:
        def post(self, path, body=None):
            return {
                "account": "viewer-account",
                "password": "viewer-password",
                "path": "/dmr/UI06S/BOM/report.xlsx",
                "size": 5,
                "sha256": hashlib.sha256(b"right").hexdigest(),
                "name": "report.xlsx",
                "action": "download",
            }

    class MockNAS:
        def __init__(self, *_):
            pass

        def login(self, account, password):
            pass

        def download(self, source_path, local, expected_size):
            local.write_bytes(b"wrong")

        def logout(self):
            pass

    monkeypatch.setattr("nas_agent.reader.FileStation", MockNAS)
    service = ReadService(cfg, FakePlatform())
    with pytest.raises(DeliveryError, match="conflict") as error:
        service.fetch_to_temp("short-ticket", temp_root=tmp_path)
    assert error.value.diagnostic["stage"] == "read_hash"


def test_reader_app_accepts_post_ticket_without_get_url_or_multipart(tmp_path):
    safe_temp = ROOT / ".runtime" / "nas-agent-test-reader-app"
    safe_temp.mkdir(parents=True, exist_ok=True)
    cfg = NasAgentConfig(
        platform_base_url="https://dmr.example.test/api/",
        worker_token="worker-token",
        synology_base_url="https://nas.example.test/",
        temp_dir=safe_temp,
        reader_allowed_origin="https://dmr.example.test",
    ).validated()
    tickets = []

    class FakeService:
        def fetch_to_temp(self, ticket):
            tickets.append(ticket)
            temporary = tempfile.TemporaryDirectory(dir=tmp_path)
            local = Path(temporary.name) / "file.txt"
            local.write_text("hello", encoding="utf-8")
            return temporary, local, {"name": "file.txt", "action": "preview"}

        @staticmethod
        def response_metadata(local, original_name, action):
            return ReadService.response_metadata(local, original_name, action)

    client = TestClient(create_reader_app(cfg, FakeService()))
    get_response = client.get("/read?ticket=do-not-log-this")
    assert get_response.status_code == 405
    json_response = client.post("/read", json={"ticket": "json-ticket"}, headers={"Origin": "https://dmr.example.test"})
    assert json_response.status_code == 200
    assert json_response.headers["access-control-allow-origin"] == "https://dmr.example.test"
    assert json_response.headers["content-security-policy"] == "sandbox"
    form_response = client.post(
        "/read",
        content="ticket=form-ticket",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://dmr.example.test"},
    )
    assert form_response.status_code == 200
    assert tickets == ["json-ticket", "form-ticket"]


def test_safe_read_error_message_does_not_render_exception_secret():
    error = DeliveryError("auth", {"source": "nas", "password": "secret"})
    message = safe_read_error_message(error)
    assert "secret" not in message
    assert "192账号" in message


def test_worker_integrates_with_actual_storage_sync_app(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    store = DMRStore(data_dir, None)
    identity = IdentityService(store)
    app = FastAPI()
    app_config = AppConfig(root=ROOT, data_dir=data_dir, database_url=None, auth_mode="local", static_root=ROOT / "frontend" / "dist")
    nas_service = install_nas(app, store, identity, app_config)
    admin_token, admin_user = identity.setup("管理员", "admin", "AdminPass123")
    admin = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    admin.cookies.set("dmr_session", admin_token)
    env = patch.dict(os.environ, {SYNC_TOKEN_ENV: "worker-token", READ_BASE_URL_ENV: "https://reader.example.test/read"})
    env.start()
    try:
        owner = identity.create_user(
            {
                "username": "owner-worker",
                "display_name": "owner-worker",
                "password": "OwnerWorker123",
                "role": "uploader",
                "active": True,
                "scopes": [{"registrant": "*", "project_category": "*", "product": "*"}],
                "can_download": True,
                "can_history": True,
            },
            admin_user,
        )
        store.execute("UPDATE identity_users SET must_change_password=0 WHERE id=?", (owner["id"],))
        assert admin.put(f"/api/users/{owner['id']}/nas-account", json={"account": "owner-nas", "password": "OwnerNasPwd"}).status_code == 200
        assert admin.post(f"/api/users/{owner['id']}/nas-account/check").status_code == 200

        class MockNAS:
            uploaded: dict[str, bytes] = {}

            def __init__(self, *_):
                pass

            def login(self, account, password):
                assert (account, password) == ("owner-nas", "OwnerNasPwd")

            def already_delivered(self, target_path, expected_size, *, expected_sha256, scratch):
                existing = self.uploaded.get(target_path)
                if existing is None:
                    return False
                if hashlib.sha256(existing).hexdigest() != expected_sha256 or len(existing) != expected_size:
                    raise DeliveryError("conflict")
                return True

            def deliver(self, local, target_path, expected_size, *, expected_sha256, temp_name, scratch_dir):
                assert temp_name.startswith(".uploading-")
                payload = local.read_bytes()
                assert len(payload) == expected_size
                assert hashlib.sha256(payload).hexdigest() == expected_sha256
                self.uploaded[target_path] = payload

            def size(self, path):
                payload = self.uploaded.get(path)
                return len(payload) if payload is not None else None

            def download(self, path, local, expected_size):
                payload = self.uploaded[path]
                assert len(payload) == expected_size
                local.write_bytes(payload)

            def logout(self):
                pass

        monkeypatch.setattr("nas_agent.worker.FileStation", MockNAS)
        monkeypatch.setattr("nas_agent.reader.FileStation", MockNAS)

        transport = httpx.ASGITransport(app=app)
        async_client = httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1/api/",
            headers={"Authorization": "Bearer worker-token"},
        )

        class SyncASGIClient:
            def __init__(self, inner):
                self.inner = inner
                self.headers = inner.headers

            def post(self, url, json=None):
                async def call():
                    return await self.inner.post(url, json=json)

                return anyio.run(call)

            def stream(self, method, url, headers=None):
                class Stream:
                    def __enter__(self_nonlocal):
                        async def call():
                            return await self.inner.request(method, url, headers=headers)

                        self_nonlocal.response = anyio.run(call)
                        return self_nonlocal.response

                    def __exit__(self_nonlocal, *_):
                        anyio.run(self_nonlocal.response.aclose)

                return Stream()

            def close(self):
                anyio.run(self.inner.aclose)

        import anyio

        integration_temp = ROOT / ".runtime" / "nas-agent-integration"
        integration_temp.mkdir(parents=True, exist_ok=True)
        worker_cfg = NasAgentConfig(
            platform_base_url="https://dmr.example.test/api/",
            worker_token="worker-token",
            synology_base_url="https://nas.example.test/",
            temp_dir=integration_temp,
        ).validated()
        worker = NasWorker(worker_cfg, PlatformClient(worker_cfg.platform_base_url, worker_cfg.worker_token, SyncASGIClient(async_client)))

        assert worker.run_once() == {"account_checks": 1, "directory_jobs": 0, "file_jobs": 0}
        assert store.execute("SELECT check_status FROM nas_credentials WHERE user_id=?", (owner["id"],)).fetchone()["check_status"] == "verified"

        now = "2026-09-22T10:00:00"
        record_id = "REC-WORKER"
        store.execute(
            """INSERT INTO records(id,business_code,name,registrant,type,project_category,project_category_source,product,module,version,previous,file_name,date,controlled,source,owner,size,source_type,linked_bom,metadata,created_at,updated_at,edit_revision)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record_id, "BOM-WORKER", "W20 BOM", "深圳金茉", "成品", "家电", "人工确认", "W20", "BOM", "20260922", "", "", "", "", "seed", "tester", "", "本地上传", "", "{}", now, now, 0),
        )
        batch_id = "UPLOAD-WORKER"
        store.execute("INSERT INTO batches(id,status,created_at,updated_at,summary,owner_user_id) VALUES(?,?,?,?,?,?)", (batch_id, "committed", now, now, "{}", owner["id"]))
        content = b"actual backend bytes"
        _blob_id, rel_path, _size = store.store_blob(content, "worker.xlsx")
        file_id = "FILE-WORKER"
        sha = sha256_bytes(content)
        store.execute(
            """INSERT INTO files(id,record_id,name,version,status,date,source,size,type,storage,sha256,blob_path,metadata,linked_bom,link_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, record_id, "worker.xlsx", "20260922", "当前有效", "2026-09-22", batch_id, "20 B", "XLS", "本地保存", sha, rel_path, "{}", "", "未关联", now),
        )
        assert admin.post("/api/nas/directory-mappings", json={"record_id": record_id, "nas_path": "/体系文件/2、首次量产BOM/W20"}).status_code == 200
        nas_service.enqueue_file(file_id, admin_user)
        assert worker.run_once() == {"account_checks": 0, "directory_jobs": 0, "file_jobs": 1}
        job = store.execute("SELECT * FROM nas_sync_jobs WHERE file_id=?", (file_id,)).fetchone()
        assert job["status"] == "synced", job["last_error"]
        assert store.execute("SELECT storage FROM files WHERE id=?", (file_id,)).fetchone()["storage"] == "本地保存 + NAS已同步"
        assert MockNAS.uploaded[job["target_path"]] == content

        owner_token = identity.login("owner-worker", "OwnerWorker123")[0]
        owner_client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        owner_client.cookies.set("dmr_session", owner_token)
        ticket = owner_client.post(f"/api/nas/files/{file_id}/read-ticket").json()
        assert ticket["mode"] == "nas"
        read_service = ReadService(worker_cfg, worker.platform)
        temporary, local, auth_job = read_service.fetch_to_temp(ticket["ticket"], temp_root=tmp_path)
        try:
            assert local.read_bytes() == content
            assert auth_job["target_path"] == job["target_path"]
            assert auth_job["name"] == "worker.xlsx"
        finally:
            temporary.cleanup()
            owner_client.close()
    finally:
        env.stop()
        admin.close()
        store.close()
