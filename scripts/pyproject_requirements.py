#!/usr/bin/env python3
"""Print a pip requirements list derived from pyproject.toml's declared deps.

``deploy/Dockerfile`` installs dependencies in a layer of their own so that an
app-code change does not re-resolve and re-download the whole stack. That layer
is worth keeping: operators rebuild this image from ``deploy/install.sh
--update``, which runs ``docker compose build --pull`` on-prem, sometimes over a
link that makes a full re-download expensive.

That layer cannot simply run ``pip install .``. ``[tool.setuptools] packages``
names ``central`` and ``printer_nanny_agent``, and setuptools refuses to produce
metadata for a package directory that is not present -- with only
``pyproject.toml`` copied it exits with ``error: package directory 'central'
does not exist`` before it resolves a single dependency.

The workaround that used to sit there is what this replaces: a second copy of
the dependency list, hand-written into the Dockerfile as a ``||`` fallback. It
had drifted three packages behind ``[project.dependencies]`` -- ``authlib``,
``cryptography`` and ``tzdata``. What kept the image working anyway was the
``pip install -e .`` further down, which does resolve ``[project.dependencies]``
-- and which was written ``|| true``. So the drift was survivable only for
exactly as long as that step kept succeeding, and its failure was discarded by
construction: one index outage and the build stays green while the image has no
SSO and cannot decrypt a single stored credential.

A projection of pyproject.toml cannot drift from pyproject.toml. That is the
whole point of this file, and why the Dockerfile must never name a package.

Usage::

    pyproject_requirements.py <pyproject.toml> [extra ...]

An extra that pyproject does not declare is an error rather than an empty set:
a typo there would quietly drop psycopg and yield an image that cannot reach
Postgres at all. So is a project that declares no dependencies -- the failure
this script exists to prevent is precisely "installed nothing, exited 0".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, List, Sequence

try:  # tomllib is stdlib from 3.11; this repo still runs on the system 3.9.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only taken on Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]


class DerivationError(Exception):
    """The requirements could not be derived, so the build must stop."""


def requirements(pyproject: Path, extras: Iterable[str] = ()) -> List[str]:
    """Return ``[project.dependencies]`` plus each named optional-dependency set.

    Order is preserved and duplicates are left alone -- pip resolves them, and
    rewriting the requirer's own strings here would be one more place to drift.
    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DerivationError("cannot read {}: {}".format(pyproject, exc))
    except tomllib.TOMLDecodeError as exc:
        raise DerivationError("cannot parse {}: {}".format(pyproject, exc))

    project = data.get("project")
    if not isinstance(project, dict):
        raise DerivationError("{} declares no [project] table".format(pyproject))

    reqs = list(project.get("dependencies") or [])
    if not reqs:
        raise DerivationError(
            "{} declares no [project.dependencies]; refusing to emit an empty "
            "requirements list".format(pyproject)
        )

    optional = project.get("optional-dependencies") or {}
    for extra in extras:
        if extra not in optional:
            raise DerivationError(
                "{} declares no '{}' extra (has: {})".format(
                    pyproject, extra, ", ".join(sorted(optional)) or "none"
                )
            )
        reqs.extend(optional[extra])
    return reqs


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: pyproject_requirements.py <pyproject.toml> [extra ...]\n")
        return 2
    try:
        lines = requirements(Path(argv[1]), argv[2:])
    except DerivationError as exc:
        sys.stderr.write("pyproject_requirements: {}\n".format(exc))
        return 1
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main(sys.argv))
