# Security

## Reporting a vulnerability

Please report security issues privately to **stephen.warren09@gmail.com** rather
than opening a public issue. We'll acknowledge within a few business days.

## Security model & hardening notes

- **Secrets.** `SECRET_KEY` signs dashboard sessions and derives the key that
  encrypts stored credentials. Key safety is **no longer conditioned on the
  database backend** -- it used to be, which meant a real SQLite install ran on a
  published key from `.env.example`. Now: where a key can be persisted, an unset
  or known-default one is **generated** and written `0600` (warning rather than
  refusing does not reach the operator who did nothing, which is the case this
  guards); where it cannot be persisted, the app **refuses to boot**. Agent API keys are stored only as
  SHA-256 hashes and shown once at enrollment (rotate from the UI). The one-time
  key is held server-side, never placed in the session cookie.
- **Sessions.** Cookies are signed (`itsdangerous`), `HttpOnly`, `SameSite=Lax`,
  and marked `Secure` in production. Put the server behind TLS (the bundled Caddy
  config provisions certs when you set a hostname).
- **Roles.** `admin` (full), `tech` (manage clients/sites/printers/agents),
  `client_readonly` (scoped read-only to one client). Management and Settings are
  admin/tech only; credential-issuing actions are gated accordingly.
- **Agent transport.** Agents dial out over HTTPS with a bearer key and verify
  TLS by default; no inbound ports are needed at sites.
- **`/install-agent.sh`** is intentionally public (like `get.docker.com`); the
  secret is the per-agent key supplied as an argument, never embedded in the script.
- **Operator-supplied URLs (SSRF).** The OIDC issuer and FreeScout base URL are
  set by admins. They are trusted inputs; use HTTPS and only point them at hosts
  you control. (Hardening these with an allowlist / private-range blocking is a
  good future addition.)
- **CSRF.** Per-form synchronizer tokens (`central/csrf.py`), bound to the
  session, rotated on login, compared with `hmac.compare_digest`, and declared as
  an **app-level dependency** so a router added later inherits the check instead
  of quietly shipping without it. `SameSite=Lax` cookies are defence in depth,
  not the defence -- `Lax` permits top-level GET navigation, which is why the
  whole-database backup download is a POST.

- **Sessions.** Signed cookies with no server-side store, so nothing can delete
  one; `User.session_epoch` is stamped in at login and compared per request, and
  bumping it is what makes logout, a password change and an admin reset actually
  revoke every outstanding session.

- **SSO.** An SSO login will not adopt an existing **password** account unless an
  operator turns on `oidc.link_local_accounts`. Matching on email alone made
  "an address the IdP will issue" enough to become the bootstrap admin, and
  guest/self-service sign-up is an ordinary IdP feature. A SCIM connector
  likewise may only manage the accounts it provisioned.

## Before going to production
- Set `SECRET_KEY` (and don't commit it).
- Serve over TLS with a real hostname in the Caddyfile.
- Use strong SNMP communities (or SNMPv3) and least-privilege FreeScout/OIDC creds.
