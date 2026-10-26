from __future__ import annotations

import hashlib
import logging
import posixpath
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx

from .config import NasAgentConfig
from .platform import PlatformClient
from .synology import DeliveryError, FileStation

log = logging.getLogger("dmr.nas_agent")


def failure_details(error: Exception) -> tuple[str, int | None, str]:
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {401, 403}:
            return "platform_auth", status, "核对 DMR_NAS_WORKER_TOKEN"
        if status in {404, 405} or 300 <= status < 400:
            return "platform_endpoint", status, "核对平台 storage-sync 接口路径"
        if status == 429:
            return "rate_limited", status, "平台限流，稍后重试"
        return "platform_http", status, "检查平台服务日志"
    if isinstance(error, httpx.TimeoutException):
        return "timeout", None, "检查内网 worker 到平台的连接"
    if isinstance(error, httpx.TransportError):
        return "transport", None, "检查 DNS、代理、HTTPS 证书"
    return "internal", None, "检查 worker 日志和版本"


class NasWorker:
    """Claim DMR archive jobs and deliver bytes to 192 via personal accounts."""

    def __init__(self, config: NasAgentConfig, platform: PlatformClient | None = None):
        self.config = config.validated()
        self.platform = platform or PlatformClient(self.config.platform_base_url, self.config.worker_token)
        self.stopping = threading.Event()

    def close(self) -> None:
        self.platform.close()

    def _post(self, path: str, body: dict | None = None):
        return self.platform.post(path, body)

    def claim_account_checks(self):
        return self._post("account-checks/claim")

    def claim_directory_jobs(self, limit: int | None = None):
        return self._post("directory-creation-jobs/claim", {"limit": limit or 1})

    def claim_files(self, limit: int | None = None):
        return self._post("claim", {"limit": limit or self.config.max_concurrency})

    def keep_lease(self, path: str, lease: dict, done: threading.Event, lost: threading.Event) -> None:
        while not done.wait(30):
            try:
                self._post(f"{path}/heartbeat", lease)
            except httpx.HTTPError as error:
                category, status, hint = failure_details(error)
                log.warning("lease_heartbeat_failed path=%s category=%s status=%s hint=%s", path, category, status, hint)
                lost.set()
                return

    def download_content(self, job: dict, local: Path) -> tuple[int, str]:
        expected_size = int(job["size"]) if job.get("size") is not None else None
        expected_sha256 = job.get("sha256")
        size = 0
        digest = hashlib.sha256()
        with self.platform.stream_content(job.get("file_id", job["id"]), job["lease_token"]) as response:
            response.raise_for_status()
            header_sha = response.headers.get("etag", "").strip('"') or response.headers.get("x-content-sha256", "")
            if header_sha and not expected_sha256:
                expected_sha256 = header_sha
            with local.open("wb") as target:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if expected_size is not None and size > expected_size:
                        raise DeliveryError("protocol", {"source": "platform", "stage": "content_size"})
                    digest.update(chunk)
                    target.write(chunk)
        calculated = digest.hexdigest()
        if expected_size is not None and size != expected_size:
            raise DeliveryError("network", {"source": "platform", "stage": "content_size"})
        if expected_sha256 and calculated.lower() != expected_sha256.lower():
            raise DeliveryError("conflict", {"source": "platform", "stage": "content_hash"})
        return size, calculated

    def run_account_check(self, job: dict) -> dict:
        return self._run(job, check=True)

    def run_directory_job(self, job: dict) -> dict:
        path = f"directory-creation-jobs/{job['id']}"
        lease = {"lease_token": job["lease_token"]}
        outcome = {**lease, "success": False, "error_code": "protocol"}
        stage = "credentials"
        try:
            with httpx.Client(base_url=self.config.synology_base_url, verify=self.config.tls_verify(), timeout=120) as client:
                nas = FileStation(client)
                try:
                    credentials = self._post(f"{path}/credentials", lease)
                    if credentials.get("error"):
                        raise DeliveryError("credentials", {"source": "platform", "stage": stage})
                    try:
                        stage = "nas_login"
                        nas.login(credentials["account"], credentials["password"])
                    finally:
                        credentials.clear()
                    stage = "nas_create_folder"
                    created = nas.create_folder(job["parent_path"], job["folder_name"])
                    if created != job["nas_path"]:
                        raise DeliveryError("protocol", {"source": "worker", "stage": "created_path"})
                    outcome.update({"success": True, "nas_path": created})
                finally:
                    nas.logout()
        except DeliveryError as error:
            if stage == "credentials" and error.code == "credentials":
                return outcome
            outcome["error_code"] = "permission" if error.code == "missing" else error.code
            outcome["diagnostic"] = error.diagnostic or {"source": "worker", "stage": stage}
        except httpx.HTTPStatusError as error:
            if stage == "credentials" and error.response.status_code == 409:
                outcome["error_code"] = "credentials"
                outcome["diagnostic"] = {"source": "platform", "stage": stage, "http_status": error.response.status_code}
                return outcome
            outcome["error_code"] = "credentials" if stage == "credentials" and error.response.status_code in {401, 403, 409} else "network"
            outcome["diagnostic"] = {"source": "platform", "stage": stage, "http_status": error.response.status_code}
        except httpx.HTTPError:
            outcome["error_code"] = "network"
            outcome["diagnostic"] = {"source": "platform", "stage": stage}
        except Exception:
            outcome["error_code"] = "protocol"
            outcome["diagnostic"] = {"source": "worker", "stage": stage}
        if outcome.get("success"):
            outcome.pop("error_code", None)
            outcome.pop("diagnostic", None)
        self._post(f"{path}/result", outcome)
        return outcome

    def run_file_job(self, job: dict) -> dict:
        return self._run(job, check=False)

    def _run(self, job: dict, *, check: bool) -> dict:
        subject_id = job["user_id"] if check and job.get("user_id") else job.get("file_id", job["id"])
        path = f"{'account-checks' if check else 'files'}/{subject_id}"
        lease = {"lease_token": job["lease_token"]}
        done = threading.Event()
        lost = threading.Event()
        heartbeat = None
        local: Path | None = None
        stage = "credentials"
        outcome = {**lease, "success": False, "error_code": "protocol"}
        delivered_size = 0
        delivered_sha256 = ""
        try:
            if not check:
                heartbeat = threading.Thread(target=self.keep_lease, args=(path, lease, done, lost), daemon=True)
                heartbeat.start()
            with httpx.Client(base_url=self.config.synology_base_url, verify=self.config.tls_verify(), timeout=120) as client:
                nas = FileStation(client)
                try:
                    credentials = self._post(f"{path}/credentials", lease)
                    try:
                        stage = "nas_login"
                        nas.login(credentials["account"], credentials["password"])
                    finally:
                        credentials.clear()
                    if check:
                        outcome["success"] = True
                    else:
                        stage = "nas_destination"
                        expected_size = int(job["size"]) if job.get("size") is not None else None
                        target_path = job["target_path"]
                        target_name = job.get("name") or posixpath.basename(target_path)
                        lease_token = str(job["lease_token"])
                        job_nonce = uuid4().hex
                        job_key = f"{job.get('file_id', job['id'])}:{lease_token}:{job_nonce}"
                        scratch_dir = self.config.temp_dir / ("job-" + hashlib.sha256(job_key.encode("utf-8")).hexdigest()[:24])
                        if expected_size is None:
                            already = False
                        else:
                            scratch = scratch_dir / "existing.remote"
                            already = nas.already_delivered(target_path, expected_size, expected_sha256=job["sha256"], scratch=scratch)
                        if not already:
                            scratch_dir.mkdir(parents=True, exist_ok=True)
                            local_name = hashlib.sha256((job_key + ":source").encode("utf-8")).hexdigest() + ".part"
                            local = scratch_dir / local_name
                            stage = "download_staging"
                            delivered_size, delivered_sha256 = self.download_content(job, local)
                            expected_size = delivered_size
                            if lost.is_set():
                                raise DeliveryError("network", {"source": "worker", "stage": "lease"})
                            stage = "nas_upload"
                            temp_name = ".uploading-" + hashlib.sha256((target_path + ":" + lease_token + ":" + job_nonce).encode("utf-8")).hexdigest()[:24]
                            nas.deliver(
                                local,
                                target_path,
                                expected_size,
                                expected_sha256=delivered_sha256,
                                temp_name=temp_name,
                                scratch_dir=scratch_dir,
                            )
                        else:
                            delivered_size = expected_size
                            delivered_sha256 = job.get("sha256", "")
                        outcome.update({"target_path": target_path, "sha256": delivered_sha256})
                        outcome["success"] = True
                finally:
                    nas.logout()
        except DeliveryError as error:
            outcome["error_code"] = "permission" if error.code == "missing" else error.code
            outcome["diagnostic"] = error.diagnostic or {"source": "worker", "stage": stage}
        except httpx.HTTPStatusError as error:
            outcome["error_code"] = "credentials" if stage == "credentials" and error.response.status_code in {401, 403, 409} else "network"
            outcome["diagnostic"] = {"source": "platform", "stage": stage, "http_status": error.response.status_code}
        except httpx.HTTPError:
            outcome["error_code"] = "network"
            outcome["diagnostic"] = {"source": "platform", "stage": stage}
        except OSError:
            outcome["error_code"] = "disk"
            outcome["diagnostic"] = {"source": "worker", "stage": stage}
        except Exception:
            outcome["error_code"] = "protocol"
            outcome["diagnostic"] = {"source": "worker", "stage": stage}
        finally:
            done.set()
            if heartbeat:
                heartbeat.join(timeout=35)
            if local:
                try:
                    parent = local.parent
                    local.unlink(missing_ok=True)
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
                except OSError:
                    log.warning("temporary_file_cleanup_failed job=%s", job.get("id"))
        if outcome.get("success"):
            outcome.pop("error_code", None)
            outcome.pop("diagnostic", None)
        self._post(f"{path}/result", outcome)
        return outcome

    def run_once(self) -> dict[str, int]:
        counts = {"account_checks": 0, "directory_jobs": 0, "file_jobs": 0}
        for check in self.claim_account_checks():
            self.run_account_check(check)
            counts["account_checks"] += 1
        for job in self.claim_directory_jobs():
            self.run_directory_job(job)
            counts["directory_jobs"] += 1
        for job in self.claim_files(self.config.max_concurrency):
            self.run_file_job(job)
            counts["file_jobs"] += 1
        return counts

    def run_forever(self) -> None:
        while not self.stopping.is_set():
            try:
                self.run_once()
            except Exception as error:
                category, status, hint = failure_details(error)
                log.warning("platform_poll_failed category=%s status=%s hint=%s", category, status, hint)
            self.stopping.wait(self.config.poll_interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = NasWorker(NasAgentConfig.from_env())
    try:
        worker.run_forever()
    finally:
        worker.close()


if __name__ == "__main__":
    main()
