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
    # Source IP / interface to bind to before sending SNMP packets. When set,
    # pysnmp's UdpTransportTarget uses localAddress=(bind_interface, 0). Lets
    # one agent serve multiple clients with overlapping internal CIDRs --
    # each tunnel terminates at a unique local IP on the agent host.
    bind_interface: Optional[str] = None


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
        """Walk an OID subtree. Capped at 512 rows for safety (Brother's binary
        status tables can be thousands of zero rows). Use ``walk_max`` for
        diagnostic dumps that need more, or to widen specific real tables."""
        raise NotImplementedError

    async def walk_max(
        self, host: str, base_oid: str, params: SnmpParams, max_rows: int
    ) -> Dict[str, str]:
        """Walk with a caller-controlled row cap. Default impls forward to walk().

        Implementations that can do better (pysnmp etc.) should override this so
        diagnostic walks (probe command) can pull more than 512 rows when the
        vendor's subtree is verbose -- the standard walk() stays bounded so
        ordinary polling stays predictable.
        """
        return await self.walk(host, base_oid, params)

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

        kwargs = {"timeout": params.timeout, "retries": params.retries}
        # localAddress lets the OS know which interface to send from -- the
        # multi-client agent path. Without it the OS picks based on the
        # routing table, which is wrong when two clients route the same
        # destination CIDR via different tunnels.
        if params.bind_interface:
            kwargs["localAddress"] = (params.bind_interface, 0)
        return await UdpTransportTarget.create((host, params.port), **kwargs)

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
            # pysnmp renders absent objects as "No Such Object/Instance ..." -- the
            # exact wording varies by version, so match on the prefix.
            if not text or text.startswith(("No Such Object", "No Such Instance")):
                out[oid] = None
            else:
                out[oid] = text
        return out

    async def walk(self, host: str, base_oid: str, params: SnmpParams) -> Dict[str, str]:
        return await self.walk_max(host, base_oid, params, 512)

    async def walk_max(
        self, host: str, base_oid: str, params: SnmpParams, max_rows: int
    ) -> Dict[str, str]:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            walk_cmd,
        )

        target = await self._target(host, params)
        results: Dict[str, str] = {}
        # lexicographicMode=False stops the walk at the end of the base_oid subtree;
        # without it pysnmp walks to the end of the device's entire MIB (slow/hang on
        # real printers). max_rows bounds pathological tables as a safety net.
        async for err_ind, err_stat, _err_idx, var_binds in walk_cmd(
            self._engine,
            self._auth(params),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
            maxRows=max_rows,
        ):
            if err_ind:
                raise SnmpError(f"{host}: {err_ind}")
            if err_stat:
                raise SnmpError(f"{host}: {err_stat.prettyPrint()}")
            for name, value in var_binds:
                results[str(name)] = value.prettyPrint()
        return results
