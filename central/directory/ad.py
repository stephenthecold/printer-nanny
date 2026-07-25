"""On-premises Active Directory over LDAP.

**Reachability caveat, stated up front because it decides whether this provider
is usable at all:** this queries the domain controller *from central*. That
works when central can route to the DC -- a single-tenant install, an MSP whose
client networks bridge at HQ (the same topology `subnets.bind_interface`
already serves), or a site-to-site VPN. It does **not** work for a customer
whose DC sits behind their firewall with no inbound path, which is the common
MSP case. Those need the query relayed through the site agent, which already
holds a command channel and is already inside that network. That is a larger
build (a new command type, agent-side LDAP, a new ingest endpoint) and is not
in this module; the provider interface is the seam it would plug into, since
an agent-relayed variant produces exactly the same DirectorySnapshot.

Identity is ``objectGUID``, not ``sAMAccountName`` or email: the GUID survives
renames, OU moves and mailbox changes, and those are the exact events that
would otherwise resurrect a person as a stranger.

ldap3 is an optional extra (`pip install -e ".[directory-ad]"`). When it is
absent this provider raises a DirectoryError naming the missing package rather
than exploding at import, so a deployment that syncs only Entra and Google
never has to carry an LDAP stack.
"""

from __future__ import annotations

import uuid
from typing import Optional

from central.directory.base import (
    DirectoryError,
    DirectoryGroup,
    DirectoryProvider,
    DirectorySnapshot,
    DirectoryUser,
)

# Bit 2 of userAccountControl is ACCOUNTDISABLE.
_ACCOUNTDISABLE = 0x0002
_PAGE = 500
_MAX_PAGES = 400


def _guid(value: object) -> str:
    """Normalise objectGUID to a canonical lowercase UUID string.

    ldap3 hands this back as raw bytes or as a '{...}' string depending on
    version and formatter config. Normalising here means the stored
    directory_id does not change shape under a library upgrade -- which would
    otherwise orphan every previously-synced row.
    """
    if isinstance(value, bytes):
        try:
            return str(uuid.UUID(bytes_le=value))
        except ValueError:
            return value.hex()
    text = str(value or "").strip().strip("{}")
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text


class ADProvider(DirectoryProvider):
    key = "ad"

    def __init__(self, config: Optional[dict] = None, secret: Optional[str] = None,
                 connection=None):
        super().__init__(config, secret)
        # Injectable connection so the parsing and DN-resolution logic is
        # testable without a domain controller.
        self._connection = connection

    def _connect(self):
        if self._connection is not None:
            return self._connection
        try:
            from ldap3 import ALL, Connection, Server
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise DirectoryError(
                "on-prem AD sync needs the ldap3 package "
                '(pip install -e ".[directory-ad]")'
            ) from exc

        host = str(self.config.get("server", "")).strip()
        if "://" in host and not host.lower().startswith(("ldap://", "ldaps://")):
            # The operator supplies this, so refuse anything that is not LDAP
            # rather than handing an arbitrary scheme to the connector.
            raise DirectoryError("server must be an ldap:// or ldaps:// URL or a hostname")
        use_ssl = host.lower().startswith("ldaps://") or bool(
            self.config.get("use_ssl", True)
        )
        try:
            server = Server(host, get_info=ALL, use_ssl=use_ssl)
            conn = Connection(
                server,
                user=str(self.config.get("bind_dn", "")).strip(),
                password=self.secret,
                auto_bind=True,
                raise_exceptions=True,
            )
        except Exception as exc:  # noqa: BLE001 - ldap3 raises a wide family
            raise DirectoryError(
                f"cannot bind to the domain controller: {type(exc).__name__}"
            ) from exc
        return conn

    def _search(self, conn, base: str, filt: str, attrs: list):
        """Paged search returning (entries, complete)."""
        entries, complete = [], True
        try:
            generator = conn.extend.standard.paged_search(
                search_base=base, search_filter=filt,
                attributes=attrs, paged_size=_PAGE, generator=True,
            )
            for count, entry in enumerate(generator):
                if entry.get("type") != "searchResEntry":
                    continue
                entries.append(entry)
                if count > _PAGE * _MAX_PAGES:
                    complete = False
                    break
        except Exception as exc:  # noqa: BLE001
            raise DirectoryError(f"LDAP search failed: {type(exc).__name__}") from exc
        return entries, complete

    def fetch(self) -> DirectorySnapshot:
        self._require("server", "base_dn", "bind_dn")
        if not self.secret:
            raise DirectoryError("missing bind password")

        base = str(self.config["base_dn"]).strip()
        conn = self._connect()

        user_filter = str(
            self.config.get("user_filter")
            or "(&(objectCategory=person)(objectClass=user))"
        ).strip()
        entries, complete = self._search(
            conn, base, user_filter,
            ["objectGUID", "sAMAccountName", "userPrincipalName", "mail",
             "displayName", "userAccountControl"],
        )

        users: list = []
        # member attributes hold DNs, so a DN -> GUID map is what makes group
        # membership resolvable at all.
        dn_to_guid: dict = {}
        for entry in entries:
            attrs = entry.get("attributes") or {}
            guid = _guid(attrs.get("objectGUID"))
            if not guid:
                continue
            dn = str(entry.get("dn") or "").lower()
            if dn:
                dn_to_guid[dn] = guid
            try:
                uac = int(attrs.get("userAccountControl") or 0)
            except (TypeError, ValueError):
                uac = 0
            users.append(DirectoryUser(
                directory_id=guid,
                email=attrs.get("mail") or attrs.get("userPrincipalName"),
                upn=attrs.get("userPrincipalName") or attrs.get("sAMAccountName"),
                display_name=attrs.get("displayName") or attrs.get("sAMAccountName"),
                active=not (uac & _ACCOUNTDISABLE),
            ))

        groups: list = []
        if str(self.config.get("sync_groups", "1")).strip() not in ("0", "false", ""):
            group_filter = str(
                self.config.get("group_filter") or "(objectClass=group)"
            ).strip()
            g_entries, g_complete = self._search(
                conn, base, group_filter, ["objectGUID", "cn", "member"]
            )
            complete = complete and g_complete
            for entry in g_entries:
                attrs = entry.get("attributes") or {}
                gid = _guid(attrs.get("objectGUID"))
                if not gid:
                    continue
                raw_members = attrs.get("member") or []
                if isinstance(raw_members, str):
                    raw_members = [raw_members]
                members = [
                    dn_to_guid[str(dn).lower()]
                    for dn in raw_members
                    if str(dn).lower() in dn_to_guid
                ]
                groups.append(DirectoryGroup(
                    directory_id=gid,
                    name=str(attrs.get("cn") or gid),
                    member_directory_ids=members,
                ))

        return DirectorySnapshot(users=users, groups=groups, complete=complete)
