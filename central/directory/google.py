"""Google Workspace via the Admin SDK Directory API.

Service account with domain-wide delegation: the operator creates a service
account, grants it the read-only directory scopes in the Workspace admin
console, and pastes the JSON key here along with an admin address to impersonate.
The impersonation is not optional -- the Directory API refuses a bare service
account, because a service account is not a member of the customer's domain.

The JWT is assembled and signed here rather than pulling in google-auth: the
whole exchange is one RS256 assertion against a fixed token endpoint, and
`cryptography` is already a dependency for encryption-at-rest. That is a smaller
surface than a transitive SDK tree, and it keeps the signing explicit enough to
audit.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Optional

import httpx

from central.directory.base import (
    DirectoryError,
    DirectoryGroup,
    DirectoryProvider,
    DirectorySnapshot,
    DirectoryUser,
)

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DIRECTORY = "https://admin.googleapis.com/admin/directory/v1"
_SCOPES = (
    "https://www.googleapis.com/auth/admin.directory.user.readonly "
    "https://www.googleapis.com/auth/admin.directory.group.readonly "
    "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
)
_TIMEOUT = 30.0
_MAX_PAGES = 200


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class GoogleProvider(DirectoryProvider):
    key = "google"

    def __init__(self, config: Optional[dict] = None, secret: Optional[str] = None,
                 client: Optional[httpx.Client] = None):
        super().__init__(config, secret)
        self._client = client

    # ------------------------------------------------------------------ auth --
    def _assertion(self, subject: str) -> str:
        try:
            key_data = json.loads(self.secret or "{}")
        except ValueError as exc:
            raise DirectoryError("service account key is not valid JSON") from exc
        pem = key_data.get("private_key")
        issuer = key_data.get("client_email")
        if not pem or not issuer:
            raise DirectoryError(
                "service account key missing private_key or client_email"
            )

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": issuer,
            "sub": subject,          # the admin being impersonated
            "scope": _SCOPES,
            "aud": _TOKEN_URI,
            "iat": now,
            # 3600 is Google's ceiling; a longer expiry is rejected outright.
            "exp": now + 3600,
        }
        signing_input = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + "." + _b64(json.dumps(claims, separators=(",", ":")).encode())
        ).encode("ascii")
        try:
            private_key = serialization.load_pem_private_key(pem.encode(), password=None)
            signature = private_key.sign(
                signing_input, padding.PKCS1v15(), hashes.SHA256()
            )
        except Exception as exc:  # noqa: BLE001 - key material problems, not transport
            raise DirectoryError("could not sign with the service account key") from exc
        return signing_input.decode("ascii") + "." + _b64(signature)

    def _token(self, http: httpx.Client) -> str:
        subject = str(self.config.get("admin_email", "")).strip()
        assertion = self._assertion(subject)
        try:
            resp = http.post(
                _TOKEN_URI,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise DirectoryError(f"cannot reach Google token endpoint: "
                                 f"{type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise DirectoryError(f"Google token request failed (HTTP {resp.status_code})")
        token = (resp.json() or {}).get("access_token")
        if not token:
            raise DirectoryError("Google returned no access token")
        return token

    # ------------------------------------------------------------------ page --
    def _pages(self, http: httpx.Client, path: str, token: str, params: dict):
        page_token, seen = "", 0
        while True:
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            try:
                resp = http.get(f"{_DIRECTORY}{path}", params=query,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=_TIMEOUT)
            except httpx.HTTPError as exc:
                raise DirectoryError(f"Directory API request failed: "
                                     f"{type(exc).__name__}") from exc
            if resp.status_code != 200:
                raise DirectoryError(
                    f"Directory API request failed (HTTP {resp.status_code})"
                )
            body = resp.json() or {}
            yield body
            page_token = body.get("nextPageToken") or ""
            seen += 1
            if not page_token:
                return
            if seen >= _MAX_PAGES:
                raise _Truncated()

    # ----------------------------------------------------------------- fetch --
    def fetch(self) -> DirectorySnapshot:
        self._require("admin_email", "customer_domain")
        if not self.secret:
            raise DirectoryError("missing service account key")

        domain = str(self.config["customer_domain"]).strip()
        http = self._client or httpx.Client()
        close = self._client is None
        complete = True
        try:
            token = self._token(http)

            users: list = []
            try:
                for body in self._pages(http, "/users", token,
                                        {"domain": domain, "maxResults": 500}):
                    for item in body.get("users") or []:
                        uid = (item.get("id") or "").strip()
                        if not uid:
                            continue
                        name = item.get("name") or {}
                        users.append(DirectoryUser(
                            directory_id=uid,
                            email=item.get("primaryEmail"),
                            upn=item.get("primaryEmail"),
                            display_name=name.get("fullName"),
                            # Google reports the negative ("suspended"), so a
                            # missing field means active, not disabled.
                            active=not item.get("suspended", False),
                        ))
            except _Truncated:
                complete = False

            groups: list = []
            if str(self.config.get("sync_groups", "1")).strip() not in ("0", "false", ""):
                try:
                    for body in self._pages(http, "/groups", token,
                                            {"domain": domain, "maxResults": 200}):
                        for item in body.get("groups") or []:
                            gid = (item.get("id") or "").strip()
                            if not gid:
                                continue
                            groups.append(DirectoryGroup(
                                directory_id=gid,
                                name=item.get("name") or item.get("email") or gid,
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
        try:
            for body in self._pages(http, f"/groups/{gid}/members", token,
                                    {"maxResults": 200}):
                for member in body.get("members") or []:
                    # USER only: a nested GROUP member would otherwise be
                    # recorded as a person who does not exist.
                    if (member.get("type") or "USER").upper() != "USER":
                        continue
                    mid = (member.get("id") or "").strip()
                    if mid:
                        out.append(mid)
        except _Truncated:
            pass
        return out


class _Truncated(Exception):
    """Paging hit the cap -- internal signal, never surfaced to callers."""
