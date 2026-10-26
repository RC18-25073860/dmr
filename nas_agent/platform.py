from __future__ import annotations

from typing import Any

import httpx


class PlatformClient:
    """HTTP client for the DMR `/api/storage-sync/*` worker API."""

    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=base_url,
            timeout=30,
            headers={"Authorization": f"Bearer {token}"},
        )
        if client is not None:
            self.client.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = self.client.post("storage-sync/" + path.lstrip("/"), json=body or {})
        response.raise_for_status()
        return response.json()

    def stream_content(self, file_id: str, lease_token: str):
        return self.client.stream(
            "GET",
            f"storage-sync/files/{file_id}/content",
            headers={"X-Sync-Lease": lease_token},
        )
