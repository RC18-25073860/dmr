import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.auth import check_request
from backend.config import AppConfig, load_config, validate_deployment_config


def request(method="GET", host="dmr.example.com", origin=None, fetch_site=None, extra=None):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    for key, value in (extra or {}).items():
        headers.append((key.lower().encode(), value.encode()))
    return Request({"type": "http", "method": method, "path": "/", "headers": headers})


def config(**overrides):
    values = dict(
        root=ROOT,
        data_dir=ROOT / "tests" / ".runtime" / "deployment-config",
        database_url=None,
        auth_mode="local",
        static_root=ROOT / "frontend" / "dist",
        environment="production",
        bind_host="127.0.0.1",
        port=8920,
        allowed_hosts=("dmr.example.com", "dmr.example.com:443"),
        trusted_origins=("https://dmr.example.com", "https://dmr.example.com:443"),
        public_origin="https://dmr.example.com",
        cookie_secure=True,
        setup_allowed=False,
    )
    values.update(overrides)
    return AppConfig(**values)


class DeploymentConfigTests(unittest.TestCase):
    def assert_forbidden(self, req, cfg):
        with self.assertRaises(HTTPException) as ctx:
            check_request(req, cfg)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_accepts_exact_configured_host_and_origin(self):
        cfg = config()
        check_request(request("POST", "dmr.example.com", "https://dmr.example.com"), cfg)
        check_request(request("POST", "dmr.example.com:443", "https://dmr.example.com:443"), cfg)

    def test_rejects_host_suffix_port_mismatch_and_wildcard_style_attacks(self):
        cfg = config()
        for host in (
            "dmr.example.com.evil.invalid",
            "evil-dmr.example.com",
            "dmr.example.com:444",
            "dmr.example.com@evil.invalid",
            "dmr.example.com/path",
        ):
            self.assert_forbidden(request("GET", host, "https://dmr.example.com"), cfg)

    def test_rejects_untrusted_null_and_malformed_origins(self):
        cfg = config()
        for origin in (
            "https://dmr.example.com.evil.invalid",
            "https://dmr.example.com:444",
            "http://dmr.example.com",
            "null",
            "https://dmr.example.com/path",
            "http://127.0.0.1:not-a-port",
        ):
            self.assert_forbidden(request("POST", "dmr.example.com", origin), cfg)

    def test_rejects_cross_site_fetch_even_when_origin_header_is_missing(self):
        cfg = config(environment="local", allowed_hosts=("127.0.0.1",), trusted_origins=("http://127.0.0.1",), public_origin=None, cookie_secure=False)
        self.assert_forbidden(request("GET", "127.0.0.1", fetch_site="cross-site"), cfg)

    def test_production_unsafe_methods_require_origin(self):
        cfg = config()
        self.assert_forbidden(request("POST", "dmr.example.com"), cfg)
        check_request(request("GET", "dmr.example.com"), cfg)

    def test_local_mode_allows_cli_or_test_requests_without_origin(self):
        cfg = config(environment="local", allowed_hosts=("127.0.0.1", "127.0.0.1:8920"), trusted_origins=("http://127.0.0.1", "http://127.0.0.1:8920"), public_origin=None, cookie_secure=False)
        check_request(request("POST", "127.0.0.1"), cfg)
        check_request(request("POST", "127.0.0.1:8920", "http://127.0.0.1:8920"), cfg)

    def test_local_mode_rejects_loopback_origin_that_does_not_match_host(self):
        cfg = config(
            environment="local",
            allowed_hosts=("127.0.0.1", "localhost"),
            trusted_origins=("http://127.0.0.1", "http://localhost"),
            public_origin=None,
            cookie_secure=False,
        )
        self.assert_forbidden(request("POST", "127.0.0.1", "http://localhost"), cfg)

    def test_ignores_forged_forwarded_host_and_proto_headers(self):
        cfg = config()
        self.assert_forbidden(
            request(
                "POST",
                "evil.invalid",
                "https://dmr.example.com",
                extra={"x-forwarded-host": "dmr.example.com", "x-forwarded-proto": "https"},
            ),
            cfg,
        )

    def test_production_config_rejects_missing_https_public_origin_and_insecure_cookie(self):
        with self.assertRaises(RuntimeError):
            validate_deployment_config(config(public_origin="http://dmr.example.com"))
        with self.assertRaises(RuntimeError):
            validate_deployment_config(config(cookie_secure=False))
        with self.assertRaises(RuntimeError):
            validate_deployment_config(config(), explicit_hosts=False, explicit_origins=True)
        with self.assertRaises(RuntimeError):
            validate_deployment_config(config(), explicit_hosts=True, explicit_origins=False)
        with self.assertRaises(RuntimeError):
            validate_deployment_config(config(setup_allowed=True))
        with self.assertRaises(RuntimeError):
            validate_deployment_config(config(trusted_origins=("https://dmr.example.com", "http://internal.example.com")))
        with self.assertRaises(RuntimeError):
            validate_deployment_config(config(trusted_origins=("https://dmr.example.com:not-a-port",)))

    def test_load_config_parses_deployment_environment(self):
        with tempfile.TemporaryDirectory(prefix="deployment-config-", dir=ROOT / "tests" / ".runtime") as tmp:
            env = {
                "DMR_ENV": "production",
                "DMR_PORT": "9443",
                "DMR_DATA_DIR": tmp,
                "DMR_BIND_HOST": "0.0.0.0",
                "DMR_ALLOWED_HOSTS": "dmr.example.com,dmr.example.com:443",
                "DMR_TRUSTED_ORIGINS": "https://dmr.example.com,https://dmr.example.com:443",
                "DMR_PUBLIC_ORIGIN": "https://dmr.example.com",
                "DMR_COOKIE_SECURE": "true",
            }
            with patch.dict(os.environ, env, clear=True):
                loaded = load_config()
        self.assertEqual(loaded.bind_host, "0.0.0.0")
        self.assertEqual(loaded.port, 9443)
        self.assertEqual(loaded.allowed_hosts, ("dmr.example.com", "dmr.example.com:443"))
        self.assertEqual(loaded.trusted_origins, ("https://dmr.example.com", "https://dmr.example.com:443"))
        self.assertTrue(loaded.cookie_secure)
        self.assertFalse(loaded.setup_allowed)

    def test_local_config_keeps_setup_allowed_for_empty_local_database(self):
        with tempfile.TemporaryDirectory(prefix="deployment-local-", dir=ROOT / "tests" / ".runtime") as tmp:
            env = {
                "DMR_ENV": "local",
                "DMR_DATA_DIR": tmp,
                "DMR_PORT": "8920",
            }
            with patch.dict(os.environ, env, clear=True):
                loaded = load_config()
        self.assertTrue(loaded.setup_allowed)
        self.assertFalse(loaded.cookie_secure)


if __name__ == "__main__":
    unittest.main()
