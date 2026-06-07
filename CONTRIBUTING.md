# Contributing

Thanks for your interest in Printer Nanny!

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,agent]"
python -m central.seed                 # demo data (admin/admin) — drops all tables
uvicorn central.main:app --reload      # http://localhost:8000
pytest && ruff check central agent tests scripts migrations
```

## Conventions

- **Python 3.9-compatible.** The Docker image runs 3.12, but code must run on
  3.9 — use `Optional[...]`, not `X | None`, anywhere evaluated at runtime
  (SQLAlchemy `Mapped[]`, FastAPI params, pydantic, dataclass fields). `from
  __future__ import annotations` is on in every module.
- **Lint/format:** `ruff` (line length 100). Keep `pytest` green.
- **SNMP is brand-agnostic** via RFC 3805 Printer MIB — avoid vendor-specific OIDs
  in the core; handle sentinel supply levels in `central/snmp_parse.py` (kept in
  sync with the agent's vendored copy by `tests/test_snmp_parse_parity.py`).
- **Migrations:** add an Alembic revision for any model change; keep upgrades
  inspector-guarded so they're safe on both fresh and existing DBs.
- **The agent stays self-contained** — no `import central` in `agent/`.

## Pull requests

1. Branch off `main`, keep changes focused.
2. Add/adjust tests; run `pytest` + `ruff` (CI runs both on 3.9 and 3.12).
3. Update docs (`README.md`, `CLAUDE.md`, `agent/README.md`) when behavior changes.
4. Note any new settings (they live in `central/runtime.py`, edited in the UI).

By contributing you agree your contributions are licensed under Apache-2.0.
