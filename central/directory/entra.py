"""Microsoft Entra ID (Azure AD) via Microsoft Graph.

Client-credentials flow: the operator registers an app in their tenant, grants
it the application permissions ``User.Read.All`` and ``GroupMember.Read.All``,
and pastes the tenant id, client id and client secret here. App-only rather than
delegated because a directory sync runs unattended at 3am with nobody to
consent -- there is no user in the loop to have a token on behalf of.

The endpoints are fixed constants, never operator-supplied, so this cannot be
pointed at an arbitrary host by editing a settings field.
"""

from __future__ import annotations

from typing import Optional

import httpx

from central.directory.base import (
    DirectoryError,
    DirectoryGroup,
    DirectoryProvider,
    DirectorySnapshot,
    DirectoryUser,
)

_LOGIN = "https://login.microsoftonline.com"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = "https://graph.microsoft.com/.default"
_TIMEOUT = 30.0
# Bounded paging: a runaway or hostile tenant must not spin forever. Hitting the
# cap marks the snapshot INCOMPLETE rather than truncating silently, so the sync
# engine suppresses deactivations instead of reading the missing tail as leavers.
_MAX_PAGES = 200


class EntraProvider(DirectoryProvider):
    key = "entra"

    def __init__(self, config: Optional[dict] = None, secret: Optional[str] = None,
                 client: Optional[httpx.Client] = None):
        super().__init__(config, secret)
        # Injectable transport so tests exercise the paging and parsing without
        # a tenant. Nothing else about this class knows it is being tested.
        self._client = client

    # ------------------------------------------------------------------ auth --
    def _token(self, http: httpx.Client) -> str:
        tenant = str(self.config.get("tenant_id", "")).strip()
        client_id = str(self.config.get("client_id", "")).strip()
        try:
            resp = http.post(
                f"{_LOGIN}/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": self.secret,
                    "scope": _SCOPE,
                },
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise DirectoryError(f"cannot reach Entra login: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            # Deliberately does not echo the response body -- Azure error
            # payloads repeat the request, and this string is persisted.
            raise DirectoryError(f"Entra token request failed (HTTP {resp.status_code})")
        token = (resp.json() or {}).get("access_token")
        if not token:
            raise DirectoryError("Entra returned no access token")
        return token

    # ------------------------------------------------------------------ page --
    def _pages(self, http: httpx.Client, url: str, token: str):
        seen = 0
        while url:
            try:
                resp = http.get(url, headers={"Authorization": f"Bearer {token}"},
                                timeout=_TIMEOUT)
            except httpx.HTTPError as exc:
                raise DirectoryError(f"Graph request failed: {type(exc).__name__}") from exc
            if resp.status_code != 200:
                raise DirectoryError(f"Graph request failed (HTTP {resp.status_code})")
            body = resp.json() or {}
            yield body.get("value") or []
            url = body.get("@odata.nextLink") or ""
            seen += 1
            if seen >= _MAX_PAGES and url:
                raise _Truncated()

    # ----------------------------------------------------------------- fetch --
    def fetch(self) -> DirectorySnapshot:
        self._require("tenant_id", "client_id")
        if not self.secret:
            raise DirectoryError("missing client secret")

        http = self._client or httpx.Client()
        close = self._client is None
        complete = True
        try:
            token = self._token(http)

            users: list = []
            url = (f"{_GRAPH}/users?$select=id,mail,userPrincipalName,"
                   f"displayName,accountEnabled&$top=999")
            try:
                for batch in self._pages(http, url, token):
                    for item in batch:
                        oid = (item.get("id") or "").strip()
                        if not oid:
                            continue
                        users.append(DirectoryUser(
                            directory_id=oid,
                            email=item.get("mail") or item.get("userPrincipalName"),
                            upn=item.get("userPrincipalName"),
                            display_name=item.get("displayName"),
                            # Absent accountEnabled means "not told" -- treat as
                            # enabled rather than deprovisioning on a missing field.
                            active=item.get("accountEnabled", True) is not False,
                        ))
            except _Truncated:
                complete = False

            groups: list = []
            if str(self.config.get("sync_groups", "1")).strip() not in ("0", "false", ""):
                try:
                    for batch in self._pages(
                        http, f"{_GRAPH}/groups?$select=id,displayName&$top=999", token
                    ):
                        for item in batch:
                            gid = (item.get("id") or "").strip()
                            if not gid:
                                continue
                            groups.append(DirectoryGroup(
                                directory_id=gid,
                                name=item.get("displayName") or gid,
                                member_directory_ids=self._members(http, token, gid),
                            ))
                except _Truncated:
                    complete = False

            return DirectorySnapshot(users=users, groups=groups, complete=complete)
        finally:
            if close:
                http.close()

    def _members(self, http: httpx.Client, token: str, gid: str) -> list:
        out: list = []
        url = f"{_GRAPH}/groups/{gid}/members?$select=id&$top=999"
        try:
            for batch in self._pages(http, url, token):
                out.extend((i.get("id") or "").strip() for i in batch if i.get("id"))
        except _Truncated:
            # One oversized group is not grounds for suppressing deactivation
            # across the whole directory; its extra members are simply not
            # resolved this run.
            pass
        return out


class _Truncated(Exception):
    """Paging hit the cap -- internal signal, never surfaced to callers."""
