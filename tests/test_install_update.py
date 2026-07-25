"""`deploy/install.sh --update` must survive a locally-edited checkout.

The failure this covers, reported from a real install:

    ==> pulling latest from origin/main
    error: Your local changes to the following files would be overwritten by merge:
            docker-compose.yml
    Please commit your changes or stash them before you merge.
    Aborting

An operator who had edited docker-compose.yml was locked out of every future
update, with no path forward that the installer told them about. The updater now
stashes local edits, fast-forwards, and re-applies them.

The half that matters most is the conflict path. A failed `git stash pop` leaves
conflict markers in the working tree *and* keeps the stash entry, so a naive
implementation would hand a compose file containing `<<<<<<<` to
`docker compose build` and strand the operator's edits in a stash they were
never told about. These tests assert the unwind is total.

Everything runs against throwaway git repos; `--pull-only` stops the installer
before it touches Docker, so no daemon is required.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"

# install.sh checks for the docker CLI (not the daemon) before doing anything.
_HAS_DEPS = (
    shutil.which("git") is not None
    and shutil.which("docker") is not None
    and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
)

pytestmark = pytest.mark.skipif(not _HAS_DEPS, reason="git + docker CLI required")

BASE_COMPOSE = "services:\n  api:\n    image: base\n  db:\n    image: postgres:16\n"

_GIT_ENV = [
    "-c", "user.email=test@example.com", "-c", "user.name=test",
    "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_GIT_ENV, *args], cwd=str(cwd),
        capture_output=True, text=True, check=True,
    )


def _git_out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


class World:
    """An 'upstream' repo plus a 'site' clone, the shape install.sh expects."""

    def __init__(self, root: Path) -> None:
        self.up = root / "up"
        self.site = root / "site"
        self.up.mkdir(parents=True)

        # in_repo() wants docker-compose.yml + central/ + deploy/Dockerfile.
        (self.up / "central").mkdir()
        (self.up / "deploy").mkdir()
        (self.up / "central" / "__init__.py").touch()
        (self.up / "deploy" / "Dockerfile").touch()
        (self.up / "docker-compose.yml").write_text(BASE_COMPOSE)
        _git(self.up, "init", "-q", ".")
        _git(self.up, "add", "-A")
        _git(self.up, "commit", "-q", "-m", "base")

        _git(root, "clone", "-q", str(self.up), str(self.site))
        # --update refuses to run without one; it distinguishes update from first-run.
        (self.site / ".env").write_text("SECRET_KEY=test\n")

    def upstream_commit(self, compose: str) -> None:
        (self.up / "docker-compose.yml").write_text(compose)
        _git(self.up, "add", "-A")
        _git(self.up, "commit", "-q", "-m", "upstream change")

    def edit_compose(self, compose: str) -> None:
        (self.site / "docker-compose.yml").write_text(compose)

    def compose(self) -> str:
        return (self.site / "docker-compose.yml").read_text()

    def head(self) -> str:
        return _git_out(self.site, "rev-parse", "--short", "HEAD")

    def stashes(self) -> str:
        return _git_out(self.site, "stash", "list")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(INSTALL_SH), *args], cwd=str(self.site),
            capture_output=True, text=True,
        )


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_local_edit_is_stashed_pulled_and_reapplied(world: World) -> None:
    """The reported failure: edits and an upstream change to the same file."""
    world.edit_compose(
        "services:\n  api:\n    image: base\n  db:\n    image: postgres:16\n    shm_size: 256mb\n"
    )
    world.upstream_commit(
        "services:\n  api:\n    image: base\n    restart: unless-stopped\n"
        "  db:\n    image: postgres:16\n"
    )
    before = world.head()

    result = world.run("--update", "--pull-only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert world.head() != before, "checkout did not advance"
    assert "shm_size: 256mb" in world.compose(), "operator's edit was lost"
    assert "restart: unless-stopped" in world.compose(), "upstream change not applied"
    assert world.stashes() == "", "auto-stash was left behind"


def test_reapplied_edit_is_flagged_as_a_future_conflict(world: World) -> None:
    """Surviving this update isn't the same as being safe; say so."""
    world.edit_compose(BASE_COMPOSE + "  extra:\n    image: x\n")
    world.upstream_commit(BASE_COMPOSE.replace("image: base", "image: base\n    restart: always"))

    result = world.run("--update", "--pull-only")

    assert result.returncode == 0
    assert "--migrate-compose" in result.stdout


def test_conflict_rolls_the_whole_update_back(world: World) -> None:
    """On conflict the tree must be byte-for-byte what it was, and nothing hidden."""
    world.edit_compose("services:\n  api:\n    image: MINE\n  db:\n    image: postgres:16\n")
    world.upstream_commit("services:\n  api:\n    image: THEIRS\n  db:\n    image: postgres:16\n")
    before = world.head()

    result = world.run("--update", "--pull-only")

    assert result.returncode != 0, "a conflicted update must not report success"
    assert world.head() == before, "checkout moved despite the failure"
    assert "image: MINE" in world.compose(), "operator's edit was lost"
    assert "<<<<<<<" not in world.compose(), "conflict markers left in the compose file"
    assert world.stashes() == "", "edits stranded in an unannounced stash"
    assert _git_out(world.site, "diff", "--name-only", "--diff-filter=U") == "", \
        "unmerged entries left in the index"
    assert "--migrate-compose" in result.stderr, "no way forward offered"


def test_clean_checkout_updates_without_stashing(world: World) -> None:
    world.upstream_commit("services:\n  api:\n    image: base2\n")
    before = world.head()

    result = world.run("--update", "--pull-only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert world.head() != before
    assert "stashing" not in result.stdout


def test_up_to_date_checkout_is_a_no_op(world: World) -> None:
    result = world.run("--update", "--pull-only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already at latest" in result.stdout


def test_migrate_compose_preserves_edits_and_restores_the_file(world: World) -> None:
    edited = BASE_COMPOSE.replace("image: base", "image: base\n    shm_size: 1g")
    world.edit_compose(edited)

    result = world.run("--migrate-compose")

    assert result.returncode == 0, result.stdout + result.stderr
    # The tracked file is upstream's again, so future pulls can't conflict on it.
    assert world.compose() == BASE_COMPOSE
    backups = list(world.site.glob("docker-compose.yml.bak.*"))
    assert len(backups) == 1, "the edited file must be recoverable in full"
    assert backups[0].read_text() == edited

    override = world.site / "docker-compose.override.yml"
    assert override.exists()
    assert "shm_size: 1g" in override.read_text(), "edits not carried into the override"

    import yaml
    yaml.safe_load(override.read_text())  # must be loadable, not just written


def test_migrate_compose_then_update_succeeds(world: World) -> None:
    """The end-to-end point: migrating unblocks the update that used to abort."""
    world.edit_compose(BASE_COMPOSE.replace("image: base", "image: MINE"))
    assert world.run("--migrate-compose").returncode == 0

    world.upstream_commit("services:\n  api:\n    image: THEIRS\n  db:\n    image: postgres:16\n")
    result = world.run("--update", "--pull-only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "image: THEIRS" in world.compose()


def test_migrate_compose_on_a_clean_tree_changes_nothing(world: World) -> None:
    result = world.run("--migrate-compose")

    assert result.returncode == 0
    assert list(world.site.glob("docker-compose.yml.bak.*")) == []
    assert not (world.site / "docker-compose.override.yml").exists()


def test_migrate_compose_never_clobbers_an_existing_override(world: World) -> None:
    """An override in place is operator-authored config, not scratch space."""
    override = world.site / "docker-compose.override.yml"
    override.write_text("services:\n  api:\n    shm_size: 64m\n")
    world.edit_compose(BASE_COMPOSE.replace("image: base", "image: MINE"))

    result = world.run("--migrate-compose")

    assert result.returncode == 0, result.stdout + result.stderr
    assert override.read_text() == "services:\n  api:\n    shm_size: 64m\n"
    migrated = list(world.site.glob("docker-compose.override.yml.migrated-*"))
    assert len(migrated) == 1
    assert "does NOT read" in result.stdout, "operator not told the file is inert"
