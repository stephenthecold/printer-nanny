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

    async def heartbeat(
        self,
        version: Optional[str] = None,
        install_path: Optional[str] = None,
        last_update_result: Optional[dict] = None,
    ) -> dict:
        """Post a heartbeat; carry diagnostic fields when present.

        The diagnostic fields are only sent once after a self-update attempt
        (read by the runner from the result-marker file, then dropped) so the
        steady-state heartbeat is still essentially {version}.
        """
        payload: dict = {"version": version}
        if install_path is not None:
            payload["install_path"] = install_path
        if last_update_result is not None:
            payload["last_update_result"] = last_update_result
        resp = await self._client.post(self._url("/heartbeat"), json=payload)
        resp.raise_for_status()
        return resp.json()

    async def get_config(self) -> dict:
        resp = await self._client.get(self._url("/config"))
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
