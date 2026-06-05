"""SNMP access behind a small async interface.

The rest of the agent (poller, discovery) depends only on ``SnmpBackend``, so it
is fully testable with a fake backend and never imports pysnmp. ``PysnmpBackend``
is the real implementation for production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class SnmpParams:
    community: str = "public"
    version: str = "2c"  # "1" or "2c"
    port: int = 161
    timeout: float = 2.0
    retries: int = 1


class SnmpError(Exception):
    """Raised when an SNMP operation fails (timeout, no response, auth)."""


class SnmpBackend:
    """Interface: implementations fetch scalar OIDs and walk subtrees."""

    async def get(
        self, host: str, oids: List[str], params: SnmpParams
    ) -> Dict[str, Optional[str]]:
        """Return {oid: value} for each requested scalar OID (None if absent)."""
        raise NotImplementedError

    async def walk(self, host: str, base_oid: str, params: SnmpParams) -> Dict[str, str]:
        """Return {full_oid: value} for every node under ``base_oid``."""
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - optional cleanup hook
        return None


class PysnmpBackend(SnmpBackend):
    """pysnmp 7 (asyncio v3arch hlapi) implementation."""

    def __init__(self) -> None:
        # Imported lazily so tests / non-SNMP commands don't require pysnmp.
        from pysnmp.hlapi.v3arch.asyncio import SnmpEngine

        self._engine = SnmpEngine()

    def _auth(self, params: SnmpParams):
        from pysnmp.hlapi.v3arch.asyncio import CommunityData

        # mpModel: 0 = SNMPv1, 1 = SNMPv2c.
        mp_model = 0 if params.version == "1" else 1
        return CommunityData(params.community, mpModel=mp_model)

    async def _target(self, host: str, params: SnmpParams):
        from pysnmp.hlapi.v3arch.asyncio import UdpTransportTarget

        return await UdpTransportTarget.create(
            (host, params.port), timeout=params.timeout, retries=params.retries
        )

    async def get(
        self, host: str, oids: List[str], params: SnmpParams
    ) -> Dict[str, Optional[str]]:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            get_cmd,
        )

        target = await self._target(host, params)
        var_types = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        err_ind, err_stat, _err_idx, var_binds = await get_cmd(
            self._engine, self._auth(params), target, ContextData(), *var_types
        )
        if err_ind:
            raise SnmpError(f"{host}: {err_ind}")
        if err_stat:
            raise SnmpError(f"{host}: {err_stat.prettyPrint()}")

        out: Dict[str, Optional[str]] = {oid: None for oid in oids}
        for oid, (name, value) in zip(oids, var_binds):
            text = value.prettyPrint()
            # pysnmp renders absent objects as "No Such Object/Instance ..." — the
            # exact wording varies by version, so match on the prefix.
            if not text or text.startswith(("No Such Object", "No Such Instance")):
                out[oid] = None
            else:
                out[oid] = text
        return out

    async def walk(self, host: str, base_oid: str, params: SnmpParams) -> Dict[str, str]:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            walk_cmd,
        )

        target = await self._target(host, params)
        results: Dict[str, str] = {}
        async for err_ind, err_stat, _err_idx, var_binds in walk_cmd(
            self._engine,
            self._auth(params),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
        ):
            if err_ind:
                raise SnmpError(f"{host}: {err_ind}")
            if err_stat:
                raise SnmpError(f"{host}: {err_stat.prettyPrint()}")
            for name, value in var_binds:
                results[str(name)] = value.prettyPrint()
        return results
