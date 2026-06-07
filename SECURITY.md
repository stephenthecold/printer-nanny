# Security

## Reporting a vulnerability

Please report security issues privately to **stephen.warren09@gmail.com** rather
than opening a public issue. We'll acknowledge within a few business days.

## Security model & hardening notes

- **Secrets.** `SECRET_KEY` signs dashboard sessions; set a strong value in
  production. The app **refuses to boot** on a non-SQLite (production) database if
  `SECRET_KEY` is unset or a known default. Agent API keys are stored only as
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
- **CSRF.** Dashboard mutations are same-origin form POSTs protected by
  `SameSite=Lax` session cookies. Per-form CSRF tokens are a planned addition.

## Before going to production
- Set `SECRET_KEY` (and don't commit it).
- Serve over TLS with a real hostname in the Caddyfile.
- Use strong SNMP communities (or SNMPv3) and least-privilege FreeScout/OIDC creds.
