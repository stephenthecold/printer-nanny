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

    async def get_device_definitions(self, since: str = "") -> dict:
        """Fetch the device/model definition feed, sending the version we hold.

        The size cap is applied to the RAW BYTES, before anything is parsed.
        That ordering is the point: a JSON bomb has to be refused by its length,
        because by the time it has been decoded far enough to have a shape, it
        has already cost whatever it was going to cost.

        A central that predates this feature answers 404. That is not an error
        worth a traceback -- it is "no definitions", which is the state this
        whole feature degrades to safely -- so it comes back as an empty
        unchanged response rather than raising.
        """
        from printer_nanny_agent.definitions import MAX_PAYLOAD_BYTES

        resp = await self._client.get(
            self._url("/device-definitions"), params={"since": since or ""}
        )
        if resp.status_code == 404:
            return {"version": since or "", "changed": False}
        resp.raise_for_status()
        if len(resp.content) > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"device definition feed is {len(resp.content)} bytes, over the "
                f"{MAX_PAYLOAD_BYTES}-byte cap; refusing to parse it"
            )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("device definition feed is not an object")
        return payload

    async def post_remote_result(self, request_id: int, result: dict) -> dict:
        """Report the outcome of one remote-hands request.

        A central that predates this feature answers 404, which is not worth a
        traceback: the command could not have come from it, so there is nothing
        to report to. Any other status still raises -- a 413 (body over the cap)
        or a 401 is something the agent's log should carry.
        """
        resp = await self._client.post(
            self._url(f"/remote-results/{int(request_id)}"), json=result
        )
        if resp.status_code == 404:
            return {"accepted": False, "reason": "central does not know this request"}
        resp.raise_for_status()
        return resp.json()

    async def get_commands(self) -> List[dict]:
        resp = await self._client.get(self._url("/commands"))
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()
