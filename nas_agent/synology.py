from __future__ import annotations

import json
import posixpath
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx


class DeliveryError(Exception):
    """Stable failure category for platform callbacks.

    The message is only the category; diagnostics must never include passwords,
    SIDs, raw URLs, or response bodies.
    """

    def __init__(self, code: str, diagnostic: dict[str, Any] | None = None):
        self.code = code
        self.diagnostic = diagnostic
        super().__init__(code)


class FileStation:
    """Small Synology File Station adapter using one personal session per job."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.sid: str | None = None
        self.apis: dict[str, dict[str, Any]] = {}

    @contextmanager
    def operation(self, api: str, method: str):
        spec = self.apis.get(api)
        diagnostic = {
            "source": "nas",
            "api": api,
            "method": method,
            "api_version": self.version(api, spec) if spec else (1 if api == "SYNO.API.Info" else None),
            "http_status": None,
            "nas_code": None,
        }
        try:
            yield
        except DeliveryError as error:
            error.diagnostic = {**diagnostic, **(error.diagnostic or {})}
            raise
        except httpx.HTTPStatusError as error:
            diagnostic["http_status"] = error.response.status_code
            raise DeliveryError("protocol", diagnostic) from None
        except httpx.HTTPError:
            raise DeliveryError("network", diagnostic) from None
        except (ValueError, KeyError, TypeError):
            raise DeliveryError("protocol", diagnostic) from None

    @staticmethod
    def version(api: str, spec: dict[str, Any] | None) -> int:
        if not spec:
            return 1
        return min(int(spec["maxVersion"]), 7 if api == "SYNO.API.Auth" else 3 if api.endswith("Upload") else 2)

    def decode(self, response: httpx.Response, *, auth: bool = False) -> dict[str, Any]:
        response.raise_for_status()
        result = response.json()
        if result.get("success"):
            return result.get("data", {})
        code = result.get("error", {}).get("code")
        diagnostic = {"http_status": response.status_code, "nas_code": code if isinstance(code, int) else None}
        if auth:
            raise DeliveryError("auth", diagnostic)
        if code == 408:
            raise DeliveryError("missing", diagnostic)
        mapped = {
            402: "permission",
            403: "permission",
            407: "permission",
            411: "permission",
            414: "conflict",
            415: "disk",
            416: "disk",
            105: "permission",
            106: "auth",
            107: "auth",
            119: "auth",
        }
        raise DeliveryError(mapped.get(code, "protocol"), diagnostic)

    def discover(self) -> None:
        names = [
            "SYNO.API.Auth",
            "SYNO.FileStation.Info",
            "SYNO.FileStation.List",
            "SYNO.FileStation.Upload",
            "SYNO.FileStation.Rename",
            "SYNO.FileStation.Download",
            "SYNO.FileStation.CreateFolder",
        ]
        with self.operation("SYNO.API.Info", "query"):
            self.apis = self.decode(
                self.client.get(
                    "webapi/query.cgi",
                    params={"api": "SYNO.API.Info", "version": "1", "method": "query", "query": ",".join(names)},
                )
            )

    def parameters(self, api: str, method: str, **kwargs: Any) -> dict[str, Any]:
        spec = self.apis[api]
        version = self.version(api, spec)
        if version < int(spec["minVersion"]):
            raise DeliveryError("protocol")
        return {
            "api": api,
            "version": str(version),
            "method": method,
            **({"_sid": self.sid} if self.sid else {}),
            **kwargs,
        }

    def request(self, api: str, method: str, **kwargs: Any) -> dict[str, Any]:
        with self.operation(api, method):
            return self.decode(
                self.client.post("webapi/" + self.apis[api]["path"], data=self.parameters(api, method, **kwargs)),
                auth=api == "SYNO.API.Auth",
            )

    def login(self, account: str, password: str) -> None:
        self.discover()
        data = self.request(
            "SYNO.API.Auth",
            "login",
            account=account,
            passwd=password,
            session="FileStation",
            format="sid",
        )
        self.sid = data["sid"]
        self.request("SYNO.FileStation.Info", "get")

    def verify_account(self, account: str, password: str) -> None:
        self.login(account, password)

    def logout(self) -> None:
        if not self.sid:
            return
        try:
            self.request("SYNO.API.Auth", "logout", session="FileStation")
        finally:
            self.sid = None

    @staticmethod
    def destination(target: str) -> str:
        normalized = posixpath.normpath(target)
        if normalized != target or not target.startswith("/") or target.startswith("//") or target == "/":
            raise DeliveryError("permission")
        if "/../" in target or target.endswith("/..") or "/./" in target:
            raise DeliveryError("permission")
        return normalized

    @staticmethod
    def folder_name(name: str) -> str:
        if not name or name in {".", ".."} or "/" in name or "\\" in name or any(ord(char) < 32 for char in name):
            raise DeliveryError("permission")
        return name

    def info(self, path: str) -> dict[str, Any] | None:
        try:
            data = self.request("SYNO.FileStation.List", "getinfo", path=json.dumps([path]), additional='["size"]')
        except DeliveryError as error:
            if error.code == "missing":
                return None
            raise
        rows = data.get("files", [])
        if len(rows) != 1:
            raise DeliveryError("protocol")
        row = rows[0]
        if row.get("code") == 408:
            return None
        if row.get("code"):
            raise DeliveryError(
                "permission",
                {
                    "source": "nas",
                    "api": "SYNO.FileStation.List",
                    "method": "getinfo",
                    "http_status": 200,
                    "nas_code": row["code"],
                },
            )
        return row

    def directory_exists(self, path: str) -> bool:
        self.destination(path)
        row = self.info(path)
        return bool(row and row.get("isdir"))

    def create_folder(self, parent_path: str, name: str) -> str:
        parent_path = self.destination(parent_path)
        name = self.folder_name(name)
        target = self.destination(f"{parent_path.rstrip('/')}/{name}")
        parent = self.info(parent_path)
        if not parent or not parent.get("isdir"):
            raise DeliveryError("missing", {"stage": "create_parent"})
        existing = self.info(target)
        if existing and existing.get("isdir"):
            return target
        if existing:
            raise DeliveryError("conflict", {"stage": "create_target"})
        api = "SYNO.FileStation.CreateFolder"
        try:
            self.request(api, "create", folder_path=json.dumps([parent_path]), name=json.dumps([name]), force_parent="false")
        except DeliveryError as error:
            if error.code == "conflict" and self.directory_exists(target):
                return target
            raise
        if not self.directory_exists(target):
            raise DeliveryError("protocol", {"stage": "create_verify"})
        return target

    def size(self, path: str) -> int | None:
        row = self.info(path)
        if row is None:
            return None
        if row.get("isdir"):
            raise DeliveryError("protocol")
        return int(row["additional"]["size"])

    def remote_sha256(self, path: str, expected_size: int, scratch: Path) -> str | None:
        if self.size(path) is None:
            return None
        self.download(path, scratch, expected_size)
        try:
            return hashlib.sha256(scratch.read_bytes()).hexdigest()
        finally:
            try:
                scratch.unlink(missing_ok=True)
            except OSError:
                pass

    def already_delivered(self, target: str, expected_size: int, *, expected_sha256: str, scratch: Path) -> bool:
        target = self.destination(target)
        existing = self.size(target)
        if existing is None:
            return False
        if existing != expected_size:
            raise DeliveryError("conflict")
        actual_sha256 = self.remote_sha256(target, expected_size, scratch)
        if actual_sha256 == expected_sha256.lower():
            return True
        raise DeliveryError("conflict", {"source": "nas", "stage": "existing_hash_mismatch"})

    def deliver(self, local: Path, target: str, expected_size: int, *, expected_sha256: str, temp_name: str, scratch_dir: Path) -> None:
        target = self.destination(target)
        expected_sha256 = expected_sha256.lower()
        scratch_dir.mkdir(parents=True, exist_ok=True)
        target_scratch = scratch_dir / (hashlib.sha256((target + ":target").encode("utf-8")).hexdigest() + ".remote")
        if self.already_delivered(target, expected_size, expected_sha256=expected_sha256, scratch=target_scratch):
            return
        directory, name = posixpath.split(target)
        if "/" in temp_name or temp_name in {".", ".."} or not temp_name.startswith(".uploading-"):
            raise DeliveryError("permission")
        temp = f"{directory}/{temp_name}"
        if name.startswith(".uploading-"):
            raise DeliveryError("conflict")
        temp_scratch = scratch_dir / (hashlib.sha256((target + ":temp").encode("utf-8")).hexdigest() + ".remote")
        temp_ok = False
        if self.size(temp) == expected_size:
            temp_ok = self.remote_sha256(temp, expected_size, temp_scratch) == expected_sha256
        if not temp_ok:
            api = "SYNO.FileStation.Upload"
            params = self.parameters(api, "upload", path=directory, create_parents="false")
            params["overwrite"] = "overwrite" if params["version"] == "3" else "true"
            with self.operation(api, "upload"), local.open("rb") as content:
                self.decode(
                    self.client.post(
                        "webapi/" + self.apis[api]["path"],
                        data=params,
                        files={"file": (temp_name, content, "application/octet-stream")},
                    )
                )
        if self.size(temp) != expected_size:
            raise DeliveryError("protocol")
        if self.remote_sha256(temp, expected_size, temp_scratch) != expected_sha256:
            raise DeliveryError("conflict", {"source": "nas", "stage": "temp_hash_mismatch"})
        self.request("SYNO.FileStation.Rename", "rename", path=json.dumps([temp]), name=json.dumps([name]))
        if self.size(target) != expected_size:
            raise DeliveryError("protocol")
        if self.remote_sha256(target, expected_size, target_scratch) != expected_sha256:
            raise DeliveryError("conflict", {"source": "nas", "stage": "final_hash_mismatch"})

    def download(self, source_path: str, local: Path, expected_size: int) -> None:
        self.destination(source_path)
        api = "SYNO.FileStation.Download"
        size = 0
        with self.operation(api, "download"):
            with self.client.stream(
                "POST",
                "webapi/" + self.apis[api]["path"],
                data=self.parameters(api, "download", path=json.dumps([source_path]), mode="download"),
            ) as response:
                response.raise_for_status()
                if "json" in response.headers.get("content-type", "") and not response.headers.get("content-disposition"):
                    response.read()
                    self.decode(response)
                    raise DeliveryError("protocol")
                with local.open("wb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > expected_size:
                            raise DeliveryError("conflict")
                        stream.write(chunk)
        if size != expected_size:
            raise DeliveryError("network")
