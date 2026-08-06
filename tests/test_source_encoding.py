"""Every source file is UTF-8 without a BOM.

This exists because the failure actually happened, in this repo, during the flash
refactor: two route modules were rewritten with PowerShell 5.1's
``Get-Content -Raw`` / ``Set-Content -Encoding utf8``, which does two damaging
things at once and reports neither.

* ``Get-Content`` decoded the UTF-8 bytes as the system ANSI codepage, so every
  em dash came back as three characters; writing them out re-encoded that
  misreading permanently. ``central/dashboard/routes.py`` acquired six of them.
* ``Set-Content -Encoding utf8`` prepends a **BOM**. Python 3 tolerates one at
  the top of a module, so the code still ran and the suite still passed -- which
  is exactly what makes it worth a test rather than a code review note.

``tests/test_install_endpoints.py`` already documents this trap for the config
files the installers *generate*. Nothing covered the source tree itself, so the
damage above was found by eye. Now it is found by the suite.

The BOM check is deliberately not limited to ``.py``: a BOM in a Jinja template
is emitted into the HTML as a zero-width no-break space before the doctype,
which puts some browsers into quirks mode.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Extensions whose bytes we own and whose encoding we therefore pin.
CHECKED_SUFFIXES = {".py", ".html", ".toml", ".md", ".yml", ".yaml", ".sh", ".ps1"}

#: Vendored build artifacts: not ours to re-encode.
SKIP_PARTS = {"static", "node_modules", ".git", "graphify-out"}

UTF8_BOM = b"\xef\xbb\xbf"


def _tracked_files():
    """Ask git, so untracked scratch files and caches are never in scope."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files"],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        pytest.skip(f"git not available: {exc}")
    for line in out.splitlines():
        path = REPO / line
        if path.suffix.lower() not in CHECKED_SUFFIXES:
            continue
        if SKIP_PARTS.intersection(path.parts):
            continue
        if path.is_file():
            yield path


def test_no_source_file_carries_a_utf8_bom():
    """Python tolerates a BOM, so nothing else in the suite would notice one."""
    offenders = [
        str(p.relative_to(REPO)) for p in _tracked_files()
        if p.read_bytes().startswith(UTF8_BOM)
    ]
    assert not offenders, (
        "UTF-8 BOM found -- almost certainly a PowerShell `Set-Content -Encoding "
        f"utf8` round trip. Rewrite with Python or -Encoding utf8NoBOM: {offenders}"
    )


def test_every_source_file_decodes_as_utf8():
    offenders = []
    for path in _tracked_files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{path.relative_to(REPO)} ({exc.reason} at byte {exc.start})")
    assert not offenders, offenders


def test_no_source_file_contains_mojibake():
    """The signature of UTF-8 decoded as CP1252 and written back.

    Checked as text rather than bytes so the message names the file a human has
    to open. These sequences do not occur naturally in this codebase -- every one
    is a mis-decoded punctuation mark.
    """
    signatures = ("â€”", "â€“", "â€™", "â€œ", "Â·", "Ã©")
    offenders = []
    for path in _tracked_files():
        text = path.read_bytes().decode("utf-8", errors="replace")
        hits = [s for s in signatures if s in text]
        if hits:
            offenders.append(f"{path.relative_to(REPO)}: {hits}")
    assert not offenders, (
        "mojibake found -- a UTF-8 file was read as CP1252 and rewritten: "
        f"{offenders}"
    )
