"""Guards on the vendored frontend assets in central/static/.

Printer Nanny is self-hosted, frequently on segmented MSP management VLANs with
no outbound internet. Tailwind and htmx used to load from cdn.tailwindcss.com
and unpkg.com, so those installs rendered an unstyled dashboard in which every
htmx-driven control was inert. Both are now served from the image.

Two regressions are cheap to introduce and expensive to notice, so both are
pinned here:

1. Someone adds a CDN ``<script>``/``<link>`` back. Works perfectly on any
   developer machine; breaks exactly the deployments that cannot report it.
2. Someone uses a Tailwind class no template used before and does not re-run
   scripts/build-assets.sh. The stylesheet is tree-shaken against the templates,
   so the class is simply absent and the element silently renders unstyled --
   no error, no console warning, nothing to grep for.

Neither test needs network or node.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "central" / "dashboard" / "templates"
STATIC_DIR = REPO_ROOT / "central" / "static"

TEMPLATES = sorted(TEMPLATE_DIR.glob("*.html"))

# Anything that would pull a subresource from off-box at render time.
EXTERNAL_REF = re.compile(
    r"""(?:src|href)\s*=\s*["'](?:https?:)?//""",
    re.IGNORECASE,
)

CLASS_ATTR = re.compile(r'class="([^"]*)"', re.S)
JINJA_BLOCK = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
SINGLE_QUOTED = re.compile(r"'([^']*)'")

# Tokens that appear in a class="" context but are not Tailwind utilities, so
# they are legitimately absent from the stylesheet.
#
# Most are Jinja *comparison operands* -- `{{ 'text-red-700' if s == 'critical'
# else ... }}` puts 'critical' inside the attribute even though only the branch
# results are ever emitted as classes. The extractor below deliberately
# over-collects (taking every quoted literal) rather than trying to parse Jinja
# expressions: over-collecting yields a false failure that a human resolves by
# adding a line here, while under-collecting yields a missed regression that
# ships an unstyled element. Only the first failure mode is self-announcing.
NON_UTILITY_TOKENS = frozenset(
    {
        # JS selector hooks -- styled by nothing, queried by querySelectorAll.
        "bulk-row",
        # Enum values compared against in Jinja conditionals.
        "bool",
        "critical",
        "insecure-snmp",
        "never_ran",
        "offline",
        "ok",
        "online",
        "open",
        "status",
        "warning",
    }
)


def _css_escape(token: str) -> str:
    """Escape a class name the way Tailwind writes it into a CSS selector."""
    return "".join("\\" + ch if ch in ":/.[]%#(),*" else ch for ch in token)


def _class_tokens(text: str) -> set[str]:
    """Collect candidate class tokens from one template's source."""
    tokens: set[str] = set()
    for attr in CLASS_ATTR.findall(text):
        # Class names built inside Jinja: `{{ 'bg-red-100' if x else 'bg-white' }}`
        for block in JINJA_BLOCK.findall(attr):
            for literal in SINGLE_QUOTED.findall(block):
                tokens.update(literal.split())
        # ...and the static remainder once the Jinja is stripped out.
        tokens.update(JINJA_BLOCK.sub(" ", attr).split())

    # `{% set colors = {'ok': 'bg-green-100 text-green-800', ...} %}` in base.html
    # holds classes outside any class="" attribute.
    for block in JINJA_BLOCK.findall(text):
        if "set " not in block:
            continue
        for literal in SINGLE_QUOTED.findall(block):
            if any(literal.startswith(p) for p in ("bg-", "text-", "border-")):
                tokens.update(literal.split())

    return {t for t in tokens if t}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_external_asset_references(template: Path) -> None:
    """No template may pull CSS/JS/fonts from off-box.

    An install with no route to the internet must render a complete dashboard.
    """
    offenders = [
        line.strip()
        for line in template.read_text().splitlines()
        if EXTERNAL_REF.search(line)
    ]
    assert not offenders, (
        f"{template.name} references an external asset:\n  "
        + "\n  ".join(offenders)
        + "\n\nVendor it into central/static/ instead -- installs on isolated "
        "management VLANs cannot fetch it, and the dashboard degrades silently."
    )


def test_vendored_assets_present() -> None:
    """The two files base.html depends on must exist and be non-trivial."""
    css = STATIC_DIR / "tailwind.css"
    js = STATIC_DIR / "htmx.min.js"
    for asset in (css, js):
        assert asset.is_file(), f"{asset} is missing; run scripts/build-assets.sh"
    # A truncated or empty build would still satisfy is_file().
    assert css.stat().st_size > 5_000, "tailwind.css looks truncated"
    assert js.stat().st_size > 20_000, "htmx.min.js looks truncated"


def test_every_template_class_is_in_the_vendored_css() -> None:
    """Catch a template using a class the tree-shaken stylesheet lacks.

    This is the failure mode that makes vendoring risky: the CSS is generated
    from whatever classes existed at build time, so a newly-added class is not
    an error anywhere -- the element just renders unstyled. Failing here turns
    "forgot to run scripts/build-assets.sh" into a red test instead of a layout
    bug an operator finds later.
    """
    css = (STATIC_DIR / "tailwind.css").read_text()

    missing: dict[str, set[str]] = {}
    for template in TEMPLATES:
        for token in _class_tokens(template.read_text()):
            if token in NON_UTILITY_TOKENS:
                continue
            if f".{_css_escape(token)}" not in css:
                missing.setdefault(token, set()).add(template.name)

    assert not missing, (
        "These classes are used in templates but absent from the vendored CSS:\n"
        + "\n".join(
            f"  {tok:40s} {', '.join(sorted(files))}"
            for tok, files in sorted(missing.items())
        )
        + "\n\nRun scripts/build-assets.sh and commit the result. If a token is "
        "not a Tailwind utility (a JS hook, or a Jinja comparison operand that "
        "the extractor over-collected), add it to NON_UTILITY_TOKENS."
    )


def test_static_mount_serves_the_assets() -> None:
    """The files must actually be reachable at the URL base.html asks for.

    Present-on-disk and served-at-/static are different claims; only the second
    one is what the browser experiences.
    """
    from fastapi.testclient import TestClient

    from central.main import app

    with TestClient(app) as client:
        css = client.get("/static/tailwind.css")
        assert css.status_code == 200
        assert "text/css" in css.headers["content-type"]
        # Upgrades must not leave a browser pairing new HTML with old CSS.
        assert css.headers.get("cache-control") == "no-cache"

        js = client.get("/static/htmx.min.js")
        assert js.status_code == 200
        assert js.headers.get("cache-control") == "no-cache"

        # Unauthenticated: login.html needs the stylesheet before a session exists.
        assert "set-cookie" not in {k.lower() for k in css.headers}


def test_static_mount_rejects_traversal() -> None:
    """The mount must not serve anything outside central/static/."""
    from fastapi.testclient import TestClient

    from central.main import app

    with TestClient(app) as client:
        for attempt in (
            "/static/../config.py",
            "/static/../../pyproject.toml",
            "/static/%2e%2e/config.py",
        ):
            assert client.get(attempt).status_code in (
                307,
                308,
                400,
                404,
            ), f"{attempt} escaped the static root"
