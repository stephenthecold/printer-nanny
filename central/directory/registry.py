"""Build a provider from a DirectoryConnection row.

The one place a stored secret is decrypted. Keeping that here rather than in
each provider means there is a single line to audit when asking "where does
plaintext credential material come into being", and the providers themselves
only ever see a string handed to them.
"""

from __future__ import annotations

from central import models as m
from central.directory.ad import ADProvider
from central.directory.base import DirectoryError, DirectoryProvider
from central.directory.entra import EntraProvider
from central.directory.google import GoogleProvider

_PROVIDERS = {
    m.DirectorySource.entra: EntraProvider,
    m.DirectorySource.google: GoogleProvider,
    m.DirectorySource.ad: ADProvider,
}

#: What the settings UI renders per provider: (field, label, help).
#: Secrets are NOT listed here -- they are a single separate field, written
#: once and never read back into the form.
CONFIG_FIELDS = {
    m.DirectorySource.entra: [
        ("tenant_id", "Directory (tenant) ID", "From the app registration overview"),
        ("client_id", "Application (client) ID", "The registered app's own id"),
    ],
    m.DirectorySource.google: [
        ("admin_email", "Admin to impersonate",
         "A Workspace super-admin; the Directory API refuses a bare service account"),
        ("customer_domain", "Primary domain", "e.g. example.com"),
    ],
    m.DirectorySource.ad: [
        ("server", "Domain controller", "hostname, or ldaps://dc.example.local"),
        ("base_dn", "Base DN", "e.g. DC=example,DC=local"),
        ("bind_dn", "Bind account DN", "e.g. CN=svc-printernanny,OU=Service,DC=example,DC=local"),
        ("user_filter", "User filter (optional)",
         "Defaults to (&(objectCategory=person)(objectClass=user))"),
    ],
}

SECRET_LABEL = {
    m.DirectorySource.entra: "Client secret",
    m.DirectorySource.google: "Service account JSON key",
    m.DirectorySource.ad: "Bind password",
}


def build_provider(conn: m.DirectoryConnection) -> DirectoryProvider:
    cls = _PROVIDERS.get(conn.provider)
    if cls is None:
        raise DirectoryError(f"no provider for {conn.provider}")

    from central.secrets import decrypt_value

    secret = decrypt_value(conn.secret) if conn.secret else ""
    return cls(config=conn.config or {}, secret=str(secret or ""))
