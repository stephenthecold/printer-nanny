"""Deployment invariants for docker-compose.yml.

The compose file is operator-facing infrastructure that no other test touches,
and its failure modes are silent: a credential default that drifts between the
db service and DATABASE_URL produces a stack that builds fine and then cannot
authenticate, and a dev-only service that quietly publishes a host port ships an
unauthenticated mail UI to every production install.

These assert the file's *contract* (every knob is a variable, the defaults agree
everywhere, dev services stay behind profiles) rather than its exact contents,
so ordinary edits don't trip them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# ${VAR:-default} -- the only interpolation form this file should use, because
# the bare ${VAR} form silently becomes an empty string when unset.
INTERPOLATION = re.compile(r"\$\{(\w+):-([^}]*)\}")

# Services that must never start without an explicit --profile.
OPT_IN_SERVICES = {"caddy", "agent", "mailhog"}


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PATH.read_text()


def test_every_variable_has_one_agreed_default(compose_text: str) -> None:
    """A variable's default must read the same everywhere it appears.

    POSTGRES_USER shows up in the db service, in both DATABASE_URLs and in the
    healthcheck. If one copy says `nanny` and another says something else, the
    stack comes up with api unable to log into its own database -- and the only
    symptom is an auth error deep in a container log.
    """
    defaults: dict[str, set[str]] = {}
    for name, default in INTERPOLATION.findall(compose_text):
        defaults.setdefault(name, set()).add(default)

    drifted = {k: sorted(v) for k, v in defaults.items() if len(v) > 1}
    assert not drifted, f"variables with conflicting defaults: {drifted}"


def test_no_bare_interpolation(compose_text: str) -> None:
    """`${VAR}` with no default resolves to "" and starts a broken container."""
    bare = re.findall(r"\$\{(\w+)\}", compose_text)
    assert not bare, f"interpolation without a default: {sorted(set(bare))}"


def test_postgres_credentials_are_variables(compose: dict) -> None:
    env = compose["services"]["db"]["environment"]
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert INTERPOLATION.fullmatch(str(env[key])), f"{key} is hardcoded: {env[key]!r}"


def test_api_and_worker_share_one_database_url(compose: dict) -> None:
    """They talk to the same database; a divergent URL is always a bug."""
    api = compose["services"]["api"]["environment"]["DATABASE_URL"]
    worker = compose["services"]["worker"]["environment"]["DATABASE_URL"]
    assert api == worker


def test_database_url_credentials_match_the_db_service(compose: dict) -> None:
    """The URL's user/password/db must be the same variables the db is built with."""
    db_env = compose["services"]["db"]["environment"]
    url = compose["services"]["api"]["environment"]["DATABASE_URL"]
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert str(db_env[key]) in url, f"{key} in the db service is not the one DATABASE_URL uses"


def test_db_healthcheck_follows_the_configured_role(compose: dict) -> None:
    """A probe pinned to a literal `nanny` passes while the real role is renamed."""
    test_cmd = " ".join(compose["services"]["db"]["healthcheck"]["test"])
    assert "POSTGRES_USER" in test_cmd and "POSTGRES_DB" in test_cmd
    # `$$` would be handed to the container's shell, where $$ is the PID.
    assert "$$" not in test_cmd


def test_worker_interval_is_configurable(compose: dict) -> None:
    command = compose["services"]["worker"]["command"]
    assert any("WORKER_INTERVAL" in str(part) for part in command)


@pytest.mark.parametrize("service", sorted(OPT_IN_SERVICES))
def test_optional_services_stay_behind_profiles(compose: dict, service: str) -> None:
    """Anything not part of the minimum production stack must be opt-in.

    mailhog is the one that matters most: it accepts unauthenticated mail and
    serves an unauthenticated web UI of everything it caught. It used to publish
    :8025 on every install, production included.
    """
    profiles = compose["services"][service].get("profiles")
    assert profiles, f"{service} would start by default"


def test_only_profiled_services_publish_host_ports(compose: dict) -> None:
    """The default stack publishes exactly one host port: the API."""
    publishers = {
        name: svc["ports"]
        for name, svc in compose["services"].items()
        if svc.get("ports") and not svc.get("profiles")
    }
    assert set(publishers) == {"api"}, f"unexpected default-profile port publishers: {publishers}"


def test_images_are_pinnable(compose: dict) -> None:
    """Every third-party image can be pinned or redirected to a private mirror."""
    for name, svc in compose["services"].items():
        if "image" in svc:
            assert INTERPOLATION.fullmatch(str(svc["image"])), \
                f"{name} pins {svc['image']!r} with no way to override it"


# --- resolution, via the real Compose implementation ------------------------ #

_HAS_COMPOSE = shutil.which("docker") is not None and subprocess.run(
    ["docker", "compose", "version"], capture_output=True
).returncode == 0

requires_compose = pytest.mark.skipif(not _HAS_COMPOSE, reason="docker compose not available")


def _resolved(**env_overrides: str) -> dict:
    """`docker compose config` -- what the daemon would actually be handed."""
    import os

    env = {**os.environ, **env_overrides}
    # An operator's .env in a dev checkout would otherwise leak into the result.
    env.pop("COMPOSE_FILE", None)
    out = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PATH), "config", "--format", "json"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@requires_compose
def test_defaults_resolve_to_the_documented_values() -> None:
    cfg = _resolved()
    api_env = cfg["services"]["api"]["environment"]
    assert api_env["DATABASE_URL"] == \
        "postgresql+psycopg://nanny:nanny@db:5432/printer_nanny"
    assert cfg["services"]["db"]["healthcheck"]["test"] == \
        ["CMD-SHELL", "pg_isready -U nanny -d printer_nanny"]


@requires_compose
def test_overrides_reach_every_consumer() -> None:
    """One .env change must move the db, both app services and the probe together."""
    cfg = _resolved(POSTGRES_USER="pn", POSTGRES_PASSWORD="s3cret", POSTGRES_DB="fleet")
    expected = "postgresql+psycopg://pn:s3cret@db:5432/fleet"
    assert cfg["services"]["api"]["environment"]["DATABASE_URL"] == expected
    assert cfg["services"]["worker"]["environment"]["DATABASE_URL"] == expected
    assert cfg["services"]["db"]["environment"]["POSTGRES_USER"] == "pn"
    assert "pg_isready -U pn -d fleet" in " ".join(cfg["services"]["db"]["healthcheck"]["test"])


@requires_compose
def test_worker_interval_reaches_the_command() -> None:
    cfg = _resolved(WORKER_INTERVAL="900")
    assert cfg["services"]["worker"]["command"][-2:] == ["--interval", "900"]


@requires_compose
def test_default_profile_is_the_minimum_stack() -> None:
    cfg = _resolved()
    assert set(cfg["services"]) == {"db", "api", "worker"}


@requires_compose
def test_shipped_override_example_is_usable_verbatim(tmp_path) -> None:
    """Operators copy the example first and edit second; it must parse as-is."""
    example = REPO_ROOT / "docker-compose.override.yml.example"
    assert example.exists()

    work = tmp_path / "stack"
    work.mkdir()
    (work / "docker-compose.yml").write_text(COMPOSE_PATH.read_text())
    (work / "docker-compose.override.yml").write_text(example.read_text())

    out = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        capture_output=True, text=True, cwd=str(work),
    )
    assert out.returncode == 0, out.stderr
