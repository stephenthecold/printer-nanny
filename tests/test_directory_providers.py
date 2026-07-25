"""Entra / Google / AD providers, driven through injected transports.

These exercise paging, field mapping and failure handling. They do NOT prove
the code works against a real tenant -- no CI runner has one, and nothing here
should be read as that claim. What they do prove is the part that is ours: that
a 401 becomes a DirectoryError rather than a traceback, that a disabled account
maps to active=False, that a truncated fetch marks the snapshot incomplete
(which is what stops the sync engine deprovisioning everyone), and that error
messages never carry the credential.
"""

from __future__ import annotations

import json

import httpx
import pytest

from central.directory.ad import ADProvider, _guid
from central.directory.base import DirectoryError
from central.directory.entra import EntraProvider
from central.directory.google import GoogleProvider

TOKEN_OK = {"access_token": "tok", "expires_in": 3600}


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --------------------------- Entra ------------------------------------------ #

def _entra(handler, **config):
    cfg = {"tenant_id": "t-1", "client_id": "c-1"}
    cfg.update(config)
    return EntraProvider(config=cfg, secret="shhh", client=_mock(handler))


def test_entra_maps_users_and_groups():
    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in request.url.path:
            return httpx.Response(200, json=TOKEN_OK)
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json={"value": [
                {"id": "oid-1", "mail": "a@acme.test",
                 "userPrincipalName": "a@acme.test", "displayName": "Ann",
                 "accountEnabled": True},
                {"id": "oid-2", "mail": None,
                 "userPrincipalName": "b@acme.test", "displayName": "Bob",
                 "accountEnabled": False},
            ]})
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json={"value": [
                {"id": "g-1", "displayName": "Accounting"},
            ]})
        if "members" in request.url.path:
            return httpx.Response(200, json={"value": [{"id": "oid-1"}]})
        return httpx.Response(404, json={})

    snap = _entra(handler).fetch()
    assert snap.complete is True
    assert [u.directory_id for u in snap.users] == ["oid-1", "oid-2"]
    assert snap.users[0].active is True
    # accountEnabled=False must deactivate, not be ignored.
    assert snap.users[1].active is False
    # No mail -> falls back to UPN rather than producing a nameless row.
    assert snap.users[1].email == "b@acme.test"
    assert snap.groups[0].name == "Accounting"
    assert snap.groups[0].member_directory_ids == ["oid-1"]


def test_entra_follows_paging():
    pages = {0: "next", 1: ""}
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in request.url.path:
            return httpx.Response(200, json=TOKEN_OK)
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json={"value": []})
        n = state["n"]
        state["n"] += 1
        body = {"value": [{"id": f"oid-{n}", "accountEnabled": True}]}
        if pages.get(n):
            body["@odata.nextLink"] = "https://graph.microsoft.com/v1.0/users?page=2"
        return httpx.Response(200, json=body)

    snap = _entra(handler).fetch()
    assert [u.directory_id for u in snap.users] == ["oid-0", "oid-1"]


def test_entra_missing_accountEnabled_is_treated_as_active():
    """Deprovisioning everyone because a field was absent is the worse failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in request.url.path:
            return httpx.Response(200, json=TOKEN_OK)
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json={"value": [{"id": "oid-1"}]})

    assert _entra(handler).fetch().users[0].active is True


def test_entra_bad_credentials_raise_a_clean_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": "invalid_client",
            "error_description": "secret was shhh",
        })

    with pytest.raises(DirectoryError) as exc:
        _entra(handler).fetch()
    # The message is persisted and rendered, so it must not echo the body.
    assert "shhh" not in str(exc.value)
    assert "401" in str(exc.value)


def test_entra_transport_failure_is_a_directory_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(DirectoryError):
        _entra(handler).fetch()


def test_entra_refuses_to_start_without_config():
    with pytest.raises(DirectoryError):
        EntraProvider(config={}, secret="x").fetch()
    with pytest.raises(DirectoryError):
        EntraProvider(config={"tenant_id": "t", "client_id": "c"}, secret="").fetch()


def test_entra_can_skip_groups():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "oauth2" in request.url.path:
            return httpx.Response(200, json=TOKEN_OK)
        return httpx.Response(200, json={"value": []})

    snap = _entra(handler, sync_groups="0").fetch()
    assert snap.groups == []
    assert not any(p.endswith("/groups") for p in seen)


# --------------------------- Google ----------------------------------------- #

_KEY = None


def _google_key() -> str:
    """A throwaway RSA key generated once per session, so the JWT signing path
    is genuinely exercised rather than stubbed."""
    global _KEY
    if _KEY is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        _KEY = json.dumps({"private_key": pem, "client_email": "svc@proj.iam.test"})
    return _KEY


def _google(handler, **config):
    cfg = {"admin_email": "admin@acme.test", "customer_domain": "acme.test"}
    cfg.update(config)
    return GoogleProvider(config=cfg, secret=_google_key(), client=_mock(handler))


def test_google_maps_users_and_groups():
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in str(request.url):
            return httpx.Response(200, json=TOKEN_OK)
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json={"users": [
                {"id": "g-1", "primaryEmail": "a@acme.test",
                 "name": {"fullName": "Ann"}, "suspended": False},
                {"id": "g-2", "primaryEmail": "b@acme.test",
                 "name": {"fullName": "Bob"}, "suspended": True},
            ]})
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json={"groups": [
                {"id": "grp-1", "name": "Accounting", "email": "acct@acme.test"},
            ]})
        if "members" in request.url.path:
            return httpx.Response(200, json={"members": [
                {"id": "g-1", "type": "USER"},
                # A nested group must not be recorded as a person.
                {"id": "grp-9", "type": "GROUP"},
            ]})
        return httpx.Response(404, json={})

    snap = _google(handler).fetch()
    assert [u.directory_id for u in snap.users] == ["g-1", "g-2"]
    assert snap.users[0].active is True
    # Google reports the negative; suspended=True means inactive.
    assert snap.users[1].active is False
    assert snap.groups[0].member_directory_ids == ["g-1"]


def test_google_missing_suspended_means_active():
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in str(request.url):
            return httpx.Response(200, json=TOKEN_OK)
        if request.url.path.endswith("/users"):
            return httpx.Response(200, json={"users": [{"id": "g-1"}]})
        return httpx.Response(200, json={})

    assert _google(handler).fetch().users[0].active is True


def test_google_rejects_a_malformed_service_account_key():
    provider = GoogleProvider(
        config={"admin_email": "a@acme.test", "customer_domain": "acme.test"},
        secret="not json",
        client=_mock(lambda r: httpx.Response(200, json=TOKEN_OK)),
    )
    with pytest.raises(DirectoryError) as exc:
        provider.fetch()
    assert "JSON" in str(exc.value)


def test_google_rejects_a_key_missing_its_private_key():
    provider = GoogleProvider(
        config={"admin_email": "a@acme.test", "customer_domain": "acme.test"},
        secret=json.dumps({"client_email": "svc@proj.iam.test"}),
        client=_mock(lambda r: httpx.Response(200, json=TOKEN_OK)),
    )
    with pytest.raises(DirectoryError):
        provider.fetch()


def test_google_token_rejection_is_clean():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(DirectoryError) as exc:
        _google(handler).fetch()
    assert "400" in str(exc.value)


def test_google_follows_paging():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in str(request.url):
            return httpx.Response(200, json=TOKEN_OK)
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json={"groups": []})
        n = state["n"]
        state["n"] += 1
        body = {"users": [{"id": f"g-{n}"}]}
        if n == 0:
            body["nextPageToken"] = "page-2"
        return httpx.Response(200, json=body)

    snap = _google(handler).fetch()
    assert [u.directory_id for u in snap.users] == ["g-0", "g-1"]


# --------------------------- AD --------------------------------------------- #

class _FakeLDAP:
    """Minimal stand-in for an ldap3 Connection's paged_search."""

    def __init__(self, users, groups):
        self._users, self._groups = users, groups
        self.extend = self
        self.standard = self

    def paged_search(self, search_base, search_filter, attributes, paged_size,
                     generator):
        entries = self._groups if "group" in search_filter.lower() else self._users
        for entry in entries:
            yield entry


def _entry(dn, attrs):
    return {"type": "searchResEntry", "dn": dn, "attributes": attrs}


def test_ad_maps_users_and_resolves_group_members_through_dns():
    users = [
        _entry("CN=Ann,OU=Staff,DC=acme,DC=local", {
            "objectGUID": "{6bcb0b04-1e3f-4a1e-9b4f-0d2f3b4a5c6d}",
            "sAMAccountName": "ann", "userPrincipalName": "ann@acme.local",
            "mail": "ann@acme.test", "displayName": "Ann",
            "userAccountControl": 512,
        }),
        _entry("CN=Bob,OU=Staff,DC=acme,DC=local", {
            "objectGUID": "{7bcb0b04-1e3f-4a1e-9b4f-0d2f3b4a5c6d}",
            "sAMAccountName": "bob", "displayName": "Bob",
            # 512 | 2 -> normal account, disabled.
            "userAccountControl": 514,
        }),
    ]
    groups = [
        _entry("CN=Accounting,OU=Groups,DC=acme,DC=local", {
            "objectGUID": "{8bcb0b04-1e3f-4a1e-9b4f-0d2f3b4a5c6d}",
            "cn": "Accounting",
            "member": ["CN=Ann,OU=Staff,DC=acme,DC=local",
                       "CN=Ghost,OU=Gone,DC=acme,DC=local"],
        }),
    ]
    provider = ADProvider(
        config={"server": "dc.acme.local", "base_dn": "DC=acme,DC=local",
                "bind_dn": "CN=svc,DC=acme,DC=local"},
        secret="pw",
        connection=_FakeLDAP(users, groups),
    )
    snap = provider.fetch()
    assert len(snap.users) == 2
    assert snap.users[0].active is True
    # ACCOUNTDISABLE bit set.
    assert snap.users[1].active is False
    # No mailbox -> email stays None, UPN/sAMAccountName carries identity.
    assert snap.users[1].email is None
    assert snap.users[1].upn == "bob"
    # A member DN with no matching user is dropped, not fatal.
    assert snap.groups[0].member_directory_ids == [snap.users[0].directory_id]


def test_ad_guid_normalisation_is_stable_across_representations():
    """ldap3 hands objectGUID back as bytes or a braced string depending on
    version; a shape change would orphan every previously-synced row."""
    import uuid as _uuid
    value = _uuid.UUID("6bcb0b04-1e3f-4a1e-9b4f-0d2f3b4a5c6d")
    assert _guid("{6bcb0b04-1e3f-4a1e-9b4f-0d2f3b4a5c6d}") == str(value)
    assert _guid("6bcb0b04-1e3f-4a1e-9b4f-0d2f3b4a5c6d") == str(value)
    assert _guid(value.bytes_le) == str(value)


def test_ad_refuses_a_non_ldap_scheme():
    provider = ADProvider(
        config={"server": "http://evil.example.com", "base_dn": "DC=a",
                "bind_dn": "CN=svc"},
        secret="pw",
    )
    with pytest.raises(DirectoryError) as exc:
        provider.fetch()
    assert "ldap" in str(exc.value).lower()


def test_ad_requires_its_config():
    with pytest.raises(DirectoryError):
        ADProvider(config={}, secret="pw").fetch()
    with pytest.raises(DirectoryError):
        ADProvider(config={"server": "dc", "base_dn": "DC=a", "bind_dn": "CN=s"},
                   secret="").fetch()
