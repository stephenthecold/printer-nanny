"""The Alembic revision chain must be a single walkable line.

This exists because ten features were built concurrently, each authoring its own
revision. Every way that can go wrong is silent until someone runs
``alembic upgrade head`` on a real deployment:

* a revision pointing at a parent that does not exist (the author's assigned
  slot landed in a different order),
* two revisions claiming the same parent (a fork -- alembic then reports
  "Multiple head revisions are present" and refuses to upgrade at all),
* two files declaring the same revision id,
* a cycle.

The suite would not otherwise notice. Most tests build their schema with
``Base.metadata.create_all()`` -- revision 0001 is itself ``create_all`` -- so
the migration path is exercised by comparatively little, and a broken chain
still lets almost every test pass. That is precisely the blindness that let a
fresh-Postgres bootstrap ship broken.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"

_REVISION = re.compile(r'^revision\s*=\s*"([^"]+)"', re.M)
_DOWN = re.compile(r'^down_revision\s*=\s*(?:"([^"]+)"|None)', re.M)


def _chain() -> "dict[str, str | None]":
    """{revision_id: down_revision_id_or_None} across every revision file."""
    chain: "dict[str, str | None]" = {}
    for path in sorted(VERSIONS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        rev = _REVISION.search(text)
        assert rev, f"{path.name} declares no revision id"
        down = _DOWN.search(text)
        assert rev.group(1) not in chain, (
            f"{path.name} reuses revision id {rev.group(1)!r}"
        )
        chain[rev.group(1)] = down.group(1) if down and down.group(1) else None
    return chain


def test_every_parent_exists():
    chain = _chain()
    dangling = {
        rev: parent
        for rev, parent in chain.items()
        if parent is not None and parent not in chain
    }
    assert not dangling, (
        "revisions naming a parent that does not exist "
        "(alembic upgrade head will fail):\n  "
        + "\n  ".join(f"{r} -> {p}" for r, p in sorted(dangling.items()))
    )


def test_there_is_exactly_one_base_and_one_head():
    chain = _chain()
    bases = [rev for rev, parent in chain.items() if parent is None]
    parents = {p for p in chain.values() if p is not None}
    heads = [rev for rev in chain if rev not in parents]

    assert len(bases) == 1, f"expected exactly one base revision, found {bases}"
    assert len(heads) == 1, (
        "multiple heads -- alembic refuses to upgrade with a forked chain: "
        f"{heads}"
    )


def test_no_two_revisions_share_a_parent():
    """A shared parent IS the fork that produces multiple heads."""
    chain = _chain()
    seen: "dict[str, list[str]]" = {}
    for rev, parent in chain.items():
        if parent is not None:
            seen.setdefault(parent, []).append(rev)
    forks = {p: kids for p, kids in seen.items() if len(kids) > 1}
    assert not forks, f"these parents have more than one child: {forks}"


def test_the_chain_reaches_every_revision_without_a_cycle():
    chain = _chain()
    parents = {p for p in chain.values() if p is not None}
    head = next(rev for rev in chain if rev not in parents)

    walked = []
    seen = set()
    cursor: "str | None" = head
    while cursor is not None:
        assert cursor not in seen, f"cycle in the revision chain at {cursor!r}"
        seen.add(cursor)
        walked.append(cursor)
        cursor = chain[cursor]

    missing = set(chain) - seen
    assert not missing, (
        f"walking from head reached {len(walked)} of {len(chain)} revisions; "
        f"orphaned: {sorted(missing)}"
    )


def test_each_docstring_names_the_predecessor_its_code_actually_declares():
    """The `Revises:` line and `down_revision` must agree.

    Nine of them did not. Alembic never reads the docstring, so nothing broke --
    but the real order after 0033 is
    0038 -> 0034 -> 0036 -> 0037 -> 0041 -> 0035 -> 0039 -> 0040 -> ...,
    and every one of those files claimed a different parent. Anyone reasoning
    about ordering from the files learned the wrong order, which is precisely the
    condition that produces an accidental branch on the next merge -- and a
    branched chain is discovered by `alembic upgrade` refusing to run, usually on
    someone's deployment.
    """
    wrong = []
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        declared = re.search(r'^down_revision\s*=\s*["\']([^"\']+)["\']', text, re.M)
        claimed = re.search(r"^Revises:\s*(\S+)\s*$", text, re.M)
        if not declared or not claimed:
            continue
        if declared.group(1) != claimed.group(1):
            wrong.append(f"{path.name}: docstring says {claimed.group(1)!r}, "
                         f"code says {declared.group(1)!r}")
    assert not wrong, "\n".join(wrong)
