"""Independent local migration contract checks (all fixtures stay inside 920)."""
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_backend import Harness, b64, xlsx_bom

ROOT = Path(__file__).resolve().parents[1]


class MigrationContractTests(unittest.TestCase):
    def fixture(self, roles="admin", mode="local"):
        env = patch.dict(os.environ, {"DMR_AUTH_MODE": mode, "DMR_LOCAL_ROLES": roles, "DMR_LOCAL_USER": "migration-test-user"})
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(prefix="contract-", dir=ROOT / "tests")
        self.addCleanup(tmp.cleanup)
        harness = Harness(Path(tmp.name))
        self.addCleanup(harness.close)
        return harness

    def test_reader_cannot_write_or_escalate_through_headers(self):
        h = self.fixture("reader")
        self.assertEqual(h.request("GET", "/api/state")["records"], [])
        payload = json.dumps({"files": []}).encode()
        for headers in ({}, {"X-DMR-Roles": "admin", "X-DMR-User": "admin", "X-Role": "admin"}):
            code, _, _ = h.raw("POST", "/api/analyze", payload, {"Content-Type": "application/json", **headers})
            self.assertEqual(code, 403)
        self.assertEqual(h.request("GET", "/api/state")["batches"], [])

    def test_uploader_can_analyze_but_not_archive(self):
        h = self.fixture("uploader")
        payload = {"files": [{"name": "BOM-ROLE_V1.0-W20【测试主体】.xlsx", "data": b64(xlsx_bom("BOM-ROLE", "V1.0", "W20", "测试主体"))}]}
        batch = h.request("POST", "/api/analyze", payload)
        self.assertEqual(batch["summary"]["matched"], 1)
        h.request("POST", "/api/commit", {"batch_id": batch["batch_id"]}, status=403)
        state = h.request("GET", "/api/state")
        self.assertEqual(state["records"], [])
        self.assertEqual(state["operationLogs"], [])  # Upload-only role cannot read global audit.
        self.assertTrue(any("测试用户 [" in item["user"] for item in h.server_mod.STORE.state()["operationLogs"]))

    def test_unconfigured_enterprise_identity_fails_closed(self):
        h = self.fixture(mode="enterprise")
        h.request("GET", "/api/state", status=503)
        h.request("POST", "/api/analyze", {"files": []}, status=503)

    def test_invalid_body_shapes_are_client_errors(self):
        h = self.fixture()
        for body in (b"null", b"[]", b'"text"', b'{"files":null}', b'{"files":[null]}', b'{"files":[{"name":"bad","data":3}]}'):
            code, _, _ = h.raw("POST", "/api/analyze", body, {"Content-Type": "application/json"})
            self.assertIn(code, (400, 422), body)
        self.assertEqual(h.request("GET", "/api/state")["batches"], [])

    def test_exact_origin_and_invalid_ports_rejected(self):
        h = self.fixture()
        for origin in (f"http://localhost:{h.port}", f"https://127.0.0.1:{h.port}", "http://127.0.0.1.evil.invalid", "http://127.0.0.1:not-a-port", "null"):
            code, _, _ = h.raw("POST", "/api/sync", b"{}", {"Content-Type": "application/json", "Origin": origin})
            self.assertEqual(code, 403, origin)
        h.request("POST", "/api/sync", {}, headers={"Origin": f"http://127.0.0.1:{h.port}"})

    def test_framework_and_integration_status_are_truthful(self):
        h = self.fixture()
        health = h.request("GET", "/api/health")
        self.assertEqual(health["backend"], "fastapi")
        spec = h.request("GET", "/openapi.json")
        for route in ("/api/analyze", "/api/review", "/api/commit"):
            self.assertIn(route, spec["paths"])
            self.assertIn("requestBody", spec["paths"][route]["post"])
        capabilities = h.request("GET", "/api/capabilities")
        self.assertFalse(capabilities["auth"]["enterpriseConfigured"])
        self.assertTrue(all(value == "未接入" for value in capabilities["externalSystems"].values()))


if __name__ == "__main__":
    unittest.main()
