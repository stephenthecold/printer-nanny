"""Async HTTP client for the central ingest API."""

from __future__ import annotations

from typing import List, Optional

import httpx


class CentralClient:
    def __init__(
        self, base_url: str, agent_id: int, api_key: str, *, verify_tls: bool = True
    ):
        self._base = base_url.rstrip("/")
        self._agent_id = agent_id
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            verify=verify_tls,
            timeout=30,
        )

    def _url(self, path: str) -> str:
        return f"{self._base}/api/v1/agents/{self._agent_id}{path}"

    async def heartbeat(self, version: Optional[str] = None) -> dict:
        resp = await self._client.post(self._url("/heartbeat"), json={"version": version})
        resp.raise_for_status()
        return resp.json()

    async def get_targets(self) -> List[dict]:
        resp = await self._client.get(self._url("/targets"))
        resp.raise_for_status()
        return resp.json()

    async def post_readings(self, readings: List[dict]) -> dict:
        resp = await self._client.post(self._url("/readings"), json={"readings": readings})
        resp.raise_for_status()
        return resp.json()

    async def post_discovered(self, devices: List[dict]) -> dict:
        resp = await self._client.post(self._url("/discovered"), json={"devices": devices})
        resp.raise_for_status()
        return resp.json()

    async def get_commands(self) -> List[dict]:
        resp = await self._client.get(self._url("/commands"))
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
