"""Build invariants for the Docker images.

`deploy/Dockerfile` is operator-facing infrastructure whose failure modes are
silent in the worst possible way: a dependency it fails to install produces an
image that builds green, starts, serves the dashboard, and then breaks only on
the paths nobody exercises during an upgrade. It had two defects at once.

  * A hand-written `||` fallback list of packages, three behind
    `[project.dependencies]` -- missing `authlib`, `cryptography` and `tzdata`.
  * `RUN pip install --no-cache-dir -e . || true`, which turns any install
    failure at all into a green build.

They interact, and the interaction is the point. The fallback was never a
fallback: `pip install ".[postgres]"` with only `pyproject.toml` copied cannot
succeed, because `[tool.setuptools] packages` names `central` and
`printer_nanny_agent` and setuptools exits with `error: package directory
'central' does not exist` before resolving anything -- confirmed by building the
old file. So the drifted list was the only path that ever ran, and the three it
omitted arrived only via the `pip install -e .` below it, the step written
`|| true`. The image was therefore correct exactly as long as that step kept
succeeding, with its failure discarded by construction: one index outage or one
yanked release and the build stays green while the image has no SSO (authlib)
and cannot decrypt a single stored credential (cryptography).

These assert the file's *contract* -- installs come from pyproject and nothing
else, and no install step may swallow its own failure -- rather than its exact
contents, so ordinary edits don't trip them. They parse the Dockerfiles
directly, the way `test_compose_deployment.py` parses the compose file, so the
contract holds where no docker daemon is available. Actually building the image
is not done here: it is minutes long and would dominate the suite. It is the
required manual proof instead -- build it and import the three packages that
drifted inside the result.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

try:  # tomllib is stdlib from 3.11; this repo still runs on the system 3.9.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only taken on Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[1]
CENTRAL_DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"
AGENT_DOCKERFILE = REPO_ROOT / "deploy" / "agent.Dockerfile"
DOCKERFILES = [CENTRAL_DOCKERFILE, AGENT_DOCKERFILE]

DERIVE_SCRIPT = REPO_ROOT / "scripts" / "pyproject_requirements.py"
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
AGENT_PYPROJECT = REPO_ROOT / "agent" / "pyproject.toml"

# The extra deploy/Dockerfile builds with. Named once here so the two places the
# Dockerfile spells it (the derivation call and the project install) are checked
# against one value rather than against each other only.
IMAGE_EXTRA = "postgres"

# The three that drifted out of the hand-written list. Named explicitly because
# a rule nobody can point an example at is a rule that gets relaxed.
DRIFTED = ("authlib", "cryptography", "tzdata")

# Shell constructs that discard an exit status. `|| true` is the one this file
# was written for; the rest are the same idea spelled differently.
SWALLOWERS = (
    re.compile(r"\|\|\s*true\b"),
    re.compile(r"\|\|\s*:"),
    re.compile(r";\s*true\s*$"),
    re.compile(r";\s*:\s*$"),
    re.compile(r"\bset\s+\+e\b"),
    re.compile(r"\bexit\s+0\s*$"),
)

# Commands that put software into the image. A `||` inside one of these is the
# fallback shape that drifts; there is no benign version of it.
INSTALLERS = ("pip install", "pip3 install", "pip  install", "-m pip install", "apt-get install")

# pip flags that consume the token after them, so it is never a package name.
_VALUE_FLAGS = {
    "-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
    "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix",
    "--root", "--platform", "--python-version", "--implementation", "--abi",
    "--no-binary", "--only-binary", "--progress-bar", "--timeout", "--retries",
    "--proxy", "--cert", "--client-cert", "--upgrade-strategy", "--report",
    "--log", "--exists-action", "--src",
}
_EDITABLE_FLAGS = {"-e", "--editable"}

# Split a RUN body into shell segments, keeping the operators so a `||` between
# two installs is visible rather than parsed away.
_SEGMENT = re.compile(r"\s*(\|\||&&|;|\|)\s*")

# The name part of a PEP 508 requirement: `uvicorn[standard]>=0.29` -> uvicorn.
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


# --- Dockerfile parsing ----------------------------------------------------- #


def _instructions(text: str) -> List[Tuple[str, str]]:
    """(INSTRUCTION, body) pairs with `\\` continuations joined.

    Comment lines are dropped, including ones *inside* a continuation -- the
    builder drops those too, so a rule that read them would be asserting against
    something the daemon never sees.
    """
    out: List[Tuple[str, str]] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not current and not line:
            continue
        if line.endswith("\\"):
            current += line[:-1].rstrip() + " "
            continue
        current += line
        instruction, _, body = current.partition(" ")
        out.append((instruction.upper(), body.strip()))
        current = ""
    if current.strip():
        instruction, _, body = current.partition(" ")
        out.append((instruction.upper(), body.strip()))
    return out


def _bodies(text: str, instruction: str) -> List[str]:
    return [body for name, body in _instructions(text) if name == instruction]


def _segments(run_body: str) -> List[str]:
    """The individual commands in a RUN, operators removed."""
    parts = (part.strip() for part in _SEGMENT.split(run_body))
    return [part for part in parts if part and part not in {"&&", "||", ";", "|"}]


def _pip_targets(run_body: str) -> Tuple[List[str], List[str]]:
    """(positional install targets, `-r` requirements files) across one RUN."""
    targets: List[str] = []
    req_files: List[str] = []
    for segment in _segments(run_body):
        if "pip" not in segment or " install" not in segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:  # unbalanced quotes -- not our problem to interpret
            continue
        if "install" not in tokens:
            continue
        rest = tokens[tokens.index("install") + 1:]
        index = 0
        while index < len(rest):
            token = rest[index]
            if token in _VALUE_FLAGS:
                if token in {"-r", "--requirement"} and index + 1 < len(rest):
                    req_files.append(rest[index + 1])
                index += 2
                continue
            if token in _EDITABLE_FLAGS:
                if index + 1 < len(rest):
                    targets.append(rest[index + 1])
                index += 2
                continue
            if token.startswith("--") and "=" in token:
                flag, _, value = token.partition("=")
                if flag in {"--requirement"}:
                    req_files.append(value)
                index += 1
                continue
            if token.startswith("-"):
                index += 1
                continue
            targets.append(token)
            index += 1
    return targets, req_files


def _is_build_context_path(target: str) -> bool:
    """True for `.`, `.[extra]`, `./x`, `/build/x`, `agent/` -- a location.

    False for anything that names a distribution to fetch from an index, which
    is the only way a Dockerfile can hold a list that drifts from pyproject.
    """
    base = target.split("[", 1)[0]
    return base in {".", ".."} or "/" in base


def _declared_names(pyproject: Path) -> Dict[str, List[str]]:
    """{'dependencies': [...], '<extra>': [...]} of raw requirement strings.

    Read here independently of the derivation script, so the tests below compare
    the script's output against pyproject rather than against itself.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    groups = {"dependencies": list(project.get("dependencies") or [])}
    groups.update({k: list(v) for k, v in (project.get("optional-dependencies") or {}).items()})
    return groups


def _distribution_names(pyproject: Path) -> List[str]:
    names = []
    for reqs in _declared_names(pyproject).values():
        for req in reqs:
            match = _REQ_NAME.match(req)
            if match:
                names.append(match.group(1))
    return sorted(set(names))


@pytest.fixture(scope="module")
def central_text() -> str:
    return CENTRAL_DOCKERFILE.read_text(encoding="utf-8")


# --- the contract ----------------------------------------------------------- #


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_no_install_step_swallows_its_own_failure(dockerfile: Path) -> None:
    """`RUN pip install -e . || true` builds green and ships a broken image.

    Every reason an install can fail -- an index outage, a yanked release, a
    dependency that needs a compiler the slim image doesn't have -- becomes a
    successful build whose breakage first appears on an operator's SSO login.
    """
    for body in _bodies(dockerfile.read_text(encoding="utf-8"), "RUN"):
        for pattern in SWALLOWERS:
            assert not pattern.search(body), \
                "{}: RUN discards an exit status ({}): {}".format(
                    dockerfile.name, pattern.pattern, body)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_no_install_step_has_a_fallback(dockerfile: Path) -> None:
    """`pip install X || pip install <hand-written list>` is the drift itself.

    The second half is a copy of pyproject that nothing keeps in step, and the
    first half failing is exactly when it gets used -- so the copy runs while
    being the least likely to be current.
    """
    for body in _bodies(dockerfile.read_text(encoding="utf-8"), "RUN"):
        if not any(marker in body for marker in INSTALLERS):
            continue
        assert "||" not in body, \
            "{}: install step has a fallback branch: {}".format(dockerfile.name, body)


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfiles_never_name_a_distribution(dockerfile: Path) -> None:
    """A package name in a RUN is a second dependency list, by definition.

    This is the structural half of the contract: if the Dockerfile cannot name a
    package, it cannot omit one either. Checked two ways -- every pip target
    must be a path in the build context, and no declared distribution name may
    appear in any RUN at all.
    """
    text = dockerfile.read_text(encoding="utf-8")
    for body in _bodies(text, "RUN"):
        targets, _ = _pip_targets(body)
        for target in targets:
            assert _is_build_context_path(target), \
                "{}: pip installs the named distribution {!r}; install from " \
                "pyproject instead".format(dockerfile.name, target)

    declared = set(_distribution_names(ROOT_PYPROJECT)) | set(_distribution_names(AGENT_PYPROJECT))
    run_text = "\n".join(_bodies(text, "RUN"))
    named = sorted(
        name for name in declared
        if re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", run_text, re.IGNORECASE)
    )
    assert not named, "{}: RUN names declared dependencies {}".format(dockerfile.name, named)


def test_every_requirements_file_is_derived_in_the_same_run(central_text: str) -> None:
    """`pip install -r` may only read a file pyproject just produced.

    A checked-in requirements.txt would be the hand-written list again, one file
    over. Deriving it in the same RUN means it cannot be stale: it does not
    exist until the moment it is used, and it is built from pyproject.
    """
    for body in _bodies(central_text, "RUN"):
        _, req_files = _pip_targets(body)
        for req_file in req_files:
            assert DERIVE_SCRIPT.name in body, \
                "pip reads {} but nothing in that RUN derived it: {}".format(req_file, body)
            assert re.search(r">\s*" + re.escape(req_file) + r"(\s|$)", body), \
                "{} is installed but not written by the derivation in that RUN: {}".format(
                    req_file, body)


def test_the_image_installs_the_project_itself(central_text: str) -> None:
    """Something must make pip resolve `[project.dependencies]` directly.

    The derived requirements layer is a cache, not the source of truth. This
    step is what proves the declared set is satisfied against pyproject at the
    end of the build -- and it is why removing `|| true` matters.
    """
    installs_project = False
    for body in _bodies(central_text, "RUN"):
        targets, _ = _pip_targets(body)
        for target in targets:
            if target.split("[", 1)[0] == ".":
                installs_project = True
                assert target == ".[{}]".format(IMAGE_EXTRA), \
                    "project install requests {!r}, not the {!r} extra the " \
                    "derivation layer was built with".format(target, IMAGE_EXTRA)
    assert installs_project, "no RUN installs the project itself from pyproject"


def test_the_project_install_is_editable(central_text: str) -> None:
    """Dropping `-e` here builds green and 500s on every dashboard page.

    Verified against the real wheel rather than assumed: `pip wheel .` inside
    the built image produces 53 entries with **zero** templates and zero static
    files. `[tool.setuptools.package-data]` declares data for
    printer_nanny_agent only, and there is no MANIFEST.in, so a non-editable
    install puts a `central` in site-packages that has its Python modules and
    none of its Jinja templates or its vendored Tailwind/htmx. Editable keeps
    /app itself on the import path, where those files are.
    """
    editable = [
        body for body in _bodies(central_text, "RUN")
        if any(flag in shlex.split(body) for flag in _EDITABLE_FLAGS)
    ]
    assert editable, (
        "the project is installed non-editable; central/'s templates and static "
        "assets are not package-data and would be missing from the image"
    )


def test_derivation_and_project_install_request_the_same_extras(central_text: str) -> None:
    """Otherwise the cached layer silently covers a different set than the app."""
    derivation = [
        body for body in _bodies(central_text, "RUN") if DERIVE_SCRIPT.name in body
    ]
    assert derivation, "no RUN derives requirements from pyproject"
    for body in derivation:
        assert re.search(r"\b" + re.escape(IMAGE_EXTRA) + r"\b", body), \
            "derivation does not request the {!r} extra: {}".format(IMAGE_EXTRA, body)


def test_copied_paths_exist_in_the_repo() -> None:
    """A COPY of a renamed script fails the build; say so here instead."""
    for dockerfile in DOCKERFILES:
        for body in _bodies(dockerfile.read_text(encoding="utf-8"), "COPY"):
            parts = shlex.split(body)
            sources = [p for p in parts[:-1] if not p.startswith("--")]
            for source in sources:
                if source == ".":
                    continue
                assert (REPO_ROOT / source).exists(), \
                    "{} copies {} which is not in the repo".format(dockerfile.name, source)


# --- the derivation itself -------------------------------------------------- #


def _derive(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DERIVE_SCRIPT), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )


def test_derivation_emits_exactly_what_pyproject_declares() -> None:
    """The projection must be a projection -- no filtering, no reordering."""
    result = _derive(str(ROOT_PYPROJECT), IMAGE_EXTRA)
    assert result.returncode == 0, result.stderr
    emitted = result.stdout.strip().splitlines()

    groups = _declared_names(ROOT_PYPROJECT)
    expected = groups["dependencies"] + groups[IMAGE_EXTRA]
    assert emitted == expected


def test_derivation_carries_every_declared_runtime_dependency() -> None:
    """The point of the whole exercise, stated as a name-by-name check."""
    result = _derive(str(ROOT_PYPROJECT), IMAGE_EXTRA)
    assert result.returncode == 0, result.stderr
    emitted_names = {
        _REQ_NAME.match(line).group(1).lower()
        for line in result.stdout.strip().splitlines() if _REQ_NAME.match(line)
    }
    declared = {name.lower() for name in _distribution_names(ROOT_PYPROJECT)}
    runtime = {
        _REQ_NAME.match(req).group(1).lower()
        for req in _declared_names(ROOT_PYPROJECT)["dependencies"]
    }
    missing = runtime - emitted_names
    assert not missing, "declared runtime dependencies not in the image: {}".format(sorted(missing))
    assert declared >= emitted_names


@pytest.mark.parametrize("package", DRIFTED)
def test_the_packages_that_drifted_are_declared_and_installed(package: str) -> None:
    """Regression, named.

    authlib: `/auth/oidc` 500s. cryptography: `central.secrets` cannot decrypt a
    single stored credential -- verified in the built image, a Fernet round trip
    through `encrypt_value`/`decrypt_value`.

    tzdata is the quiet one, and it is worth stating accurately rather than
    dramatically: `python:3.12-slim` does currently ship Debian's tzdata (443
    zone files, measured in the image), so `zoneinfo` resolves without the wheel
    today. Nothing installed depends on that package though, so an upstream
    slimming pass removes it with no signal here -- and `central.suppression`
    degrades an unresolvable zone to UTC rather than raising, by design, which
    is exactly why nothing would report it. Declared means pinned.
    """
    runtime = {
        _REQ_NAME.match(req).group(1).lower()
        for req in _declared_names(ROOT_PYPROJECT)["dependencies"]
    }
    assert package in runtime, "{} is no longer a declared dependency".format(package)

    result = _derive(str(ROOT_PYPROJECT), IMAGE_EXTRA)
    assert result.returncode == 0, result.stderr
    assert re.search(r"(?m)^" + re.escape(package) + r"\b", result.stdout), \
        "{} is declared but the image would not install it".format(package)


def test_derivation_refuses_an_unknown_extra() -> None:
    """A typo must stop the build, not quietly drop psycopg from the image."""
    result = _derive(str(ROOT_PYPROJECT), "postgress")
    assert result.returncode != 0
    assert "postgress" in result.stderr


def test_derivation_refuses_a_project_with_no_dependencies(tmp_path: Path) -> None:
    """"Installed nothing, exited 0" is the failure this file exists to stop."""
    empty = tmp_path / "pyproject.toml"
    empty.write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    result = _derive(str(empty))
    assert result.returncode != 0
    assert "dependencies" in result.stderr


def test_derivation_refuses_an_unreadable_pyproject(tmp_path: Path) -> None:
    result = _derive(str(tmp_path / "absent.toml"))
    assert result.returncode != 0
    assert result.stdout.strip() == ""
