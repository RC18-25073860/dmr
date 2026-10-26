from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def _https_url(value: str, name: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an https origin/base URL without credentials")
    return value.rstrip("/") + "/"


def _inside_920(path: Path, name: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError(f"{name} must be inside {ROOT}") from exc
    return resolved


@dataclass(frozen=True)
class NasAgentConfig:
    """Runtime settings for the LAN worker.

    This object deliberately avoids pydantic so it can be imported by the DMR
    project without adding new dependencies. Secrets are passed as strings but
    are never rendered by worker diagnostics.
    """

    platform_base_url: str
    worker_token: str
    synology_base_url: str
    temp_dir: Path = ROOT / ".runtime" / "nas-agent-tmp"
    synology_ca_file: Path | None = None
    synology_verify_tls: bool = True
    reader_allowed_origin: str | None = None
    reader_host: str = "127.0.0.1"
    reader_port: int = 8910
    reader_ssl_cert: Path | None = None
    reader_ssl_key: Path | None = None
    poll_interval_seconds: float = 300
    max_concurrency: int = 2

    @classmethod
    def from_env(cls) -> "NasAgentConfig":
        token = os.environ.get("DMR_NAS_WORKER_TOKEN", "").strip()
        if not token:
            raise ValueError("DMR_NAS_WORKER_TOKEN is required")
        temp_dir = Path(os.environ.get("DMR_NAS_TEMP_DIR", ROOT / ".runtime" / "nas-agent-tmp"))
        ca_raw = os.environ.get("DMR_NAS_CA_FILE")
        return cls(
            platform_base_url=os.environ["DMR_NAS_PLATFORM_URL"],
            worker_token=token,
            synology_base_url=os.environ["DMR_NAS_BASE_URL"],
            temp_dir=temp_dir,
            synology_ca_file=Path(ca_raw) if ca_raw else None,
            synology_verify_tls=os.environ.get("DMR_NAS_VERIFY_TLS", "true").lower() not in {"0", "false", "no"},
            reader_allowed_origin=os.environ.get("DMR_NAS_READER_ALLOWED_ORIGIN") or None,
            reader_host=os.environ.get("DMR_NAS_READER_HOST", "127.0.0.1"),
            reader_port=int(os.environ.get("DMR_NAS_READER_PORT", "8910")),
            reader_ssl_cert=Path(os.environ["DMR_NAS_READER_SSL_CERT"]) if os.environ.get("DMR_NAS_READER_SSL_CERT") else None,
            reader_ssl_key=Path(os.environ["DMR_NAS_READER_SSL_KEY"]) if os.environ.get("DMR_NAS_READER_SSL_KEY") else None,
            poll_interval_seconds=float(os.environ.get("DMR_NAS_POLL_SECONDS", "300")),
            max_concurrency=int(os.environ.get("DMR_NAS_MAX_CONCURRENCY", "2")),
        ).validated()

    def validated(self) -> "NasAgentConfig":
        if not self.worker_token.strip():
            raise ValueError("worker token is required")
        if not 1 <= self.max_concurrency <= 2:
            raise ValueError("max_concurrency must be 1 or 2")
        if not 1 <= self.poll_interval_seconds <= 300:
            raise ValueError("poll_interval_seconds must be between 1 and 300")
        if not self.synology_verify_tls:
            raise ValueError("synology TLS verification cannot be disabled")
        if self.synology_ca_file:
            _inside_920(self.synology_ca_file, "synology_ca_file")
        if self.reader_ssl_cert:
            _inside_920(self.reader_ssl_cert, "reader_ssl_cert")
        if self.reader_ssl_key:
            _inside_920(self.reader_ssl_key, "reader_ssl_key")
        return NasAgentConfig(
            platform_base_url=_https_url(self.platform_base_url, "platform_base_url"),
            worker_token=self.worker_token,
            synology_base_url=_https_url(self.synology_base_url, "synology_base_url"),
            temp_dir=_inside_920(self.temp_dir, "temp_dir"),
            synology_ca_file=self.synology_ca_file,
            synology_verify_tls=self.synology_verify_tls,
            reader_allowed_origin=_origin(self.reader_allowed_origin) if self.reader_allowed_origin else _origin(self.platform_base_url),
            reader_host=self.reader_host,
            reader_port=self.reader_port,
            reader_ssl_cert=self.reader_ssl_cert,
            reader_ssl_key=self.reader_ssl_key,
            poll_interval_seconds=self.poll_interval_seconds,
            max_concurrency=self.max_concurrency,
        )

    def tls_verify(self):
        if not self.synology_verify_tls:
            return False
        if self.synology_ca_file:
            return ssl.create_default_context(cafile=str(self.synology_ca_file))
        return True


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("reader allowed origin must be an explicit https origin")
    return f"{parsed.scheme}://{parsed.netloc}"
