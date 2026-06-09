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
    version: str = "2c"  # "1" | "2c" | "3"
    port: int = 161
    timeout: float = 2.0
    retries: int = 1
    # Source IP / interface to bind to before sending SNMP packets. When set,
    # pysnmp's UdpTransportTarget uses localAddress=(bind_interface, 0). Lets
    # one agent serve multiple clients with overlapping internal CIDRs --
    # each tunnel terminates at a unique local IP on the agent host.
    bind_interface: Optional[str] = None
    # SNMPv3 (USM) authentication / privacy. Used only when version == "3".
    # security_level: noAuthNoPriv | authNoPriv | authPriv
    # auth_protocol:  MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512
    # priv_protocol:  DES | 3DES | AES128 | AES192 | AES256
    # All v3 fields default to None; security_level defaults to noAuthNoPriv
    # when only ``v3_user`` is set.
    v3_user: Optional[str] = None
    v3_security_level: Optional[str] = None
    v3_auth_protocol: Optional[str] = None
    v3_auth_password: Optional[str] = None
    v3_priv_protocol: Optional[str] = None
    v3_priv_password: Optional[str] = None
    v3_context_name: Optional[str] = None


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
        from pysnmp.hlapi.v3arch.asyncio import CommunityData, UsmUserData

        if params.version == "3":
            return _build_v3_auth(params, UsmUserData)
        # mpModel: 0 = SNMPv1, 1 = SNMPv2c.
        mp_model = 0 if params.version == "1" else 1
        return CommunityData(params.community, mpModel=mp_model)

    def _context(self, params: SnmpParams):
        """ContextData with optional engine context name for SNMPv3."""
        from pysnmp.hlapi.v3arch.asyncio import ContextData

        if params.version == "3" and params.v3_context_name:
            return ContextData(contextName=params.v3_context_name)
        return ContextData()

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
            ObjectIdentity,
            ObjectType,
            get_cmd,
        )

        target = await self._target(host, params)
        var_types = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        err_ind, err_stat, _err_idx, var_binds = await get_cmd(
            self._engine, self._auth(params), target, self._context(params), *var_types
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
            self._context(params),
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


def _v3_proto_maps():
    """Return (auth_map, priv_map) wired to real pysnmp protocol objects.

    Lazy import so callers that don't need real pysnmp protos (the unit-test
    branch-coverage path, dry runs, etc.) can call ``_build_v3_auth`` with a
    fake UsmUserData and skip the proto lookup entirely.
    """
    from pysnmp.hlapi.v3arch.asyncio import (
        usmAesCfb128Protocol,
        usmAesCfb192Protocol,
        usmAesCfb256Protocol,
        usmDESPrivProtocol,
        usmHMACMD5AuthProtocol,
        usmHMACSHAAuthProtocol,
        usmHMAC128SHA224AuthProtocol,
        usmHMAC192SHA256AuthProtocol,
        usmHMAC256SHA384AuthProtocol,
        usmHMAC384SHA512AuthProtocol,
        usmNoAuthProtocol,
        usmNoPrivProtocol,
        usm3DESEDEPrivProtocol,
    )

    auth_map = {
        "MD5": usmHMACMD5AuthProtocol,
        "SHA": usmHMACSHAAuthProtocol,
        "SHA1": usmHMACSHAAuthProtocol,
        "SHA224": usmHMAC128SHA224AuthProtocol,
        "SHA256": usmHMAC192SHA256AuthProtocol,
        "SHA384": usmHMAC256SHA384AuthProtocol,
        "SHA512": usmHMAC384SHA512AuthProtocol,
        None: usmNoAuthProtocol,
        "": usmNoAuthProtocol,
        "NONE": usmNoAuthProtocol,
    }
    priv_map = {
        "DES": usmDESPrivProtocol,
        "3DES": usm3DESEDEPrivProtocol,
        "AES": usmAesCfb128Protocol,
        "AES128": usmAesCfb128Protocol,
        "AES192": usmAesCfb192Protocol,
        "AES256": usmAesCfb256Protocol,
        None: usmNoPrivProtocol,
        "": usmNoPrivProtocol,
        "NONE": usmNoPrivProtocol,
    }
    return auth_map, priv_map


def _build_v3_auth(params: SnmpParams, UsmUserData, _maps=None):
    """Translate SnmpParams.v3_* fields into a pysnmp UsmUserData.

    The mapping mirrors the strings used in the UI and config -- if a value we
    don't recognize comes through, we fall back to the no-auth/no-priv default
    so a typo in settings doesn't raise an opaque pysnmp error at poll time.

    Security levels:
      noAuthNoPriv -> just (userName,)
      authNoPriv   -> + authProtocol + authKey
      authPriv     -> + privProtocol + privKey

    ``_maps`` is a test seam: pass a (auth_map, priv_map) pair to skip the
    pysnmp lazy import. Production callers leave it None.
    """
    user = params.v3_user or ""
    level = (params.v3_security_level or "noAuthNoPriv").strip()
    if level == "noAuthNoPriv":
        return UsmUserData(user)
    # Only authNoPriv / authPriv need the pysnmp proto objects.
    auth_map, priv_map = _maps if _maps is not None else _v3_proto_maps()
    auth_key = (params.v3_auth_protocol or "").strip().upper()
    priv_key = (params.v3_priv_protocol or "").strip().upper()
    auth_proto = auth_map.get(auth_key, auth_map.get(""))
    priv_proto = priv_map.get(priv_key, priv_map.get(""))

    if level == "authPriv":
        return UsmUserData(
            user,
            authKey=params.v3_auth_password or "",
            privKey=params.v3_priv_password or "",
            authProtocol=auth_proto,
            privProtocol=priv_proto,
        )
    # authNoPriv
    return UsmUserData(
        user,
        authKey=params.v3_auth_password or "",
        authProtocol=auth_proto,
    )
