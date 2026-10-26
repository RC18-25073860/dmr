from __future__ import annotations

import mimetypes
import posixpath
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qs

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from .config import NasAgentConfig
from .platform import PlatformClient
from .synology import DeliveryError, FileStation


class ReadService:
    """Redeem short-lived read tickets and read with the viewer's own NAS account."""

    def __init__(self, config: NasAgentConfig, platform: PlatformClient | None = None):
        self.config = config.validated()
        self.platform = platform or PlatformClient(self.config.platform_base_url, self.config.worker_token)

    def authorize(self, ticket: str) -> dict[str, Any]:
        return self.platform.post("read/authorize", {"ticket": ticket})

    def fetch_to_temp(self, ticket: str, temp_root: Path | None = None) -> tuple[TemporaryDirectory, Path, dict[str, Any]]:
        job = self.authorize(ticket)
        root = temp_root or self.config.temp_dir
        root.mkdir(parents=True, exist_ok=True)
        temporary = TemporaryDirectory(prefix="dmr-nas-read-", dir=str(root))
        try:
            source_path = job.get("path") or job.get("target_path")
            if not source_path:
                raise DeliveryError("protocol", {"source": "platform", "stage": "read_authorize"})
            expected_sha256 = job.get("sha256")
            if not expected_sha256:
                raise DeliveryError("protocol", {"source": "platform", "stage": "read_hash"})
            name = job.get("name") or posixpath.basename(source_path)
            target = Path(temporary.name) / ("file" + Path(name).suffix)
            with httpx.Client(base_url=self.config.synology_base_url, verify=self.config.tls_verify(), timeout=120) as client:
                nas = FileStation(client)
                try:
                    nas.login(job["account"], job.pop("password"))
                    expected_size = int(job["size"]) if job.get("size") is not None else None
                    if expected_size is None:
                        expected_size = nas.size(source_path)
                    if expected_size is None:
                        raise DeliveryError("missing", {"source": "nas", "stage": "read_size"})
                    nas.download(source_path, target, expected_size)
                finally:
                    nas.logout()
            if hashlib.sha256(target.read_bytes()).hexdigest().lower() != expected_sha256.lower():
                raise DeliveryError("conflict", {"source": "nas", "stage": "read_hash"})
            job["name"] = name
            return temporary, target, job
        except Exception:
            temporary.cleanup()
            raise

    @staticmethod
    def response_metadata(local: Path, original_name: str, action: str) -> dict[str, str]:
        mime = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
        disposition = "inline" if action == "preview" and (mime == "application/pdf" or mime.startswith(("image/", "text/", "audio/", "video/"))) else "attachment"
        return {"filename": original_name, "media_type": mime, "content_disposition_type": disposition}


def safe_read_error_message(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403, 409}:
        return "访问凭证已过期、权限已变更或个人192账号不可用。"
    if isinstance(error, DeliveryError) and error.code in {"auth", "permission"}:
        return "个人192账号无权读取该文件，请联系管理员核对账号和目录权限。"
    return "无法读取NAS文件，请确认内网、个人192账号及目录权限后重试。"


async def _ticket_from_post(request: Request) -> str:
    media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        body = await request.json()
        ticket = body.get("ticket") if isinstance(body, dict) else None
    elif media_type == "application/x-www-form-urlencoded":
        body = (await request.body()).decode("utf-8", errors="replace")
        ticket = parse_qs(body).get("ticket", [None])[0]
    else:
        ticket = None
    if not isinstance(ticket, str) or not ticket.strip():
        raise ValueError("ticket is required")
    return ticket.strip()


def create_reader_app(config: NasAgentConfig, service: ReadService | None = None) -> FastAPI:
    cfg = config.validated()
    reader = service or ReadService(cfg)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cfg.reader_allowed_origin],
        allow_methods=["POST"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/read")
    async def read(request: Request):
        try:
            ticket = await _ticket_from_post(request)
            temporary, local, job = reader.fetch_to_temp(ticket)
            meta = reader.response_metadata(local, job["name"], job.get("action", "download"))
            return FileResponse(
                local,
                filename=meta["filename"],
                media_type=meta["media_type"],
                content_disposition_type=meta["content_disposition_type"],
                headers={"Cache-Control": "no-store", "Content-Security-Policy": "sandbox"},
                background=BackgroundTask(temporary.cleanup),
            )
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers={"Cache-Control": "no-store"})
        except Exception as error:
            message = safe_read_error_message(error)
            return HTMLResponse(
                '<!doctype html><meta charset="utf-8"><title>文件暂不可用</title>'
                '<main style="max-width:40rem;margin:12vh auto;font:16px/1.8 sans-serif;padding:2rem">'
                f"<h2>文件暂不可用</h2><p>{message}</p><p>可关闭此页，回到 DMR 平台。</p></main>",
                status_code=502,
                headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
            )

    return app


def main() -> None:
    config = NasAgentConfig.from_env()
    if not config.reader_ssl_cert or not config.reader_ssl_key:
        raise SystemExit("DMR_NAS_READER_SSL_CERT and DMR_NAS_READER_SSL_KEY are required for the HTTPS reader")
    uvicorn.run(
        create_reader_app(config),
        host=config.reader_host,
        port=config.reader_port,
        ssl_certfile=str(config.reader_ssl_cert),
        ssl_keyfile=str(config.reader_ssl_key),
        access_log=False,
    )


if __name__ == "__main__":
    main()
