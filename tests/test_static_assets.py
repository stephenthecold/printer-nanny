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

# External macOS volumes can materialise AppleDouble metadata as ``._name``
# sidecars. They are binary filesystem bookkeeping, not Jinja templates.
TEMPLATES = sorted(
    template for template in TEMPLATE_DIR.glob("*.html")
    if not template.name.startswith("._")
)

# Anything that would pull a subresource from off-box at render time.
EXTERNAL_REF = re.compile(
    r"""(?:src|href)\s*=\s*["'](?:https?:)?//""",
    re.IGNORECASE,
)

CLASS_ATTR = re.compile(r'class="([^"]*)"', re.S)
JINJA_BLOCK = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
TAG = re.compile(r"<[^>]*>", re.S)
SINGLE_QUOTED = re.compile(r"'([^']*)'")

# Tokens that survive the utility-shape filter below but are still not Tailwind
# classes, so they are legitimately absent from the stylesheet.
NON_UTILITY_TOKENS = frozenset(
    {
        # A JS selector hook -- styled by nothing, queried by querySelectorAll.
        "bulk-row",
        # The placeholder host in the default agent pip source; agents.html tests
        # for it to decide whether the install command has been configured yet.
        "your-org",
    }
)

# Quoted literals inside Jinja are ambiguous: the same syntax carries class
# lists (`{{ 'bg-red-100' if bad else 'bg-white' }}`), comparison operands
# (`{% if status == 'critical' %}`) and, in _components.html, the *keys* of the
# variant maps (`{'neutral': 'bg-slate-100 text-slate-600'}`). Only the first is
# a class.
#
# Rather than parse Jinja, a literal is accepted only when EVERY one of its
# whitespace-separated tokens is shaped like a utility. That all-or-nothing rule
# is what separates a class list from the other things that live in quotes here:
#
#   'bg-red-100 text-red-700'      -> every token utility-shaped, accepted
#   'critical'                     -> bare word, rejected
#   'neutral'                      -> a variant-map key, rejected
#   '/manage/users'                -> a nav href, rejected
#   'printer-nanny-agent --version'-> '--version' is not utility-shaped, so the
#                                     whole literal is rejected
#
# Tokens taken from the *static* part of a class attribute skip this filter --
# there they are unambiguously classes, so checking all of them is stricter.
UTILITY_SHAPED = re.compile(r"^-?[a-z][a-z0-9]*[-:]")
KNOWN_BARE_UTILITIES = frozenset(
    {
        "block", "border", "capitalize", "container", "contents", "fixed",
        "flex", "grid", "hidden", "inline", "invisible", "italic", "isolate",
        "lowercase", "relative", "absolute", "rounded", "shadow", "static",
        "sticky", "table", "truncate", "underline", "uppercase", "visible",
    }
)


def _is_utility_shaped(token: str) -> bool:
    return bool(UTILITY_SHAPED.match(token)) or token in KNOWN_BARE_UTILITIES


def _class_list_literal(literal: str) -> list[str]:
    """Return the tokens of `literal` if it is entirely a class list, else []."""
    parts = literal.split()
    if not parts or not all(_is_utility_shaped(p) for p in parts):
        return []
    return parts


def _css_escape(token: str) -> str:
    """Escape a class name the way Tailwind writes it into a CSS selector."""
    return "".join("\\" + ch if ch in ":/.[]%#(),*" else ch for ch in token)


def _class_tokens(text: str) -> set[str]:
    """Collect candidate class tokens from one template's source.

    Static class-attribute text is taken verbatim; anything from a quoted Jinja
    literal is filtered to utility-shaped tokens (see UTILITY_SHAPED).
    """
    tokens: set[str] = set()

    for attr in CLASS_ATTR.findall(text):
        # Class names built inside Jinja: `{{ 'bg-red-100' if x else 'bg-white' }}`
        for block in JINJA_BLOCK.findall(attr):
            for literal in SINGLE_QUOTED.findall(block):
                tokens.update(_class_list_literal(literal))
        # ...and the static remainder once the Jinja is stripped out.
        tokens.update(JINJA_BLOCK.sub(" ", attr).split())

    # Variant maps hold canonical class strings outside any class="" attribute --
    # base.html's status colours, and every variant map in _components.html.
    # Restricted to `{% set %}` because a general sweep of Jinja blocks also
    # picks up macro *arguments* (autocomplete='new-password' is utility-shaped
    # but is not a class).
    for block in JINJA_BLOCK.findall(text):
        if "set " not in block:
            continue
        for literal in SINGLE_QUOTED.findall(block):
            tokens.update(_class_list_literal(literal))

    # Class strings that are raw macro-body text rather than a quoted literal:
    #
    #   {% macro input_cls(extra='') -%}
    #   w-full border border-slate-300 rounded px-3 py-2 text-sm ...
    #   {%- endmacro %}
    #
    # Tailwind's own extractor scans files as plain text and so generates these,
    # but without this pass the guard would not notice them going missing -- and
    # _components.html is precisely where one absent class renders every page
    # that uses the component unstyled. A line counts as a class list only when
    # it holds two or more tokens and all of them are utility-shaped, which
    # prose and markup never satisfy.
    stripped = TAG.sub(" ", JINJA_BLOCK.sub(" ", JINJA_COMMENT.sub(" ", text)))
    for line in stripped.splitlines():
        parts = line.split()
        if len(parts) >= 2 and all(_is_utility_shaped(p) for p in parts):
            tokens.update(parts)

    return {t for t in tokens if t}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_external_asset_references(template: Path) -> None:
    """No template may pull CSS/JS/fonts from off-box.

    An install with no route to the internet must render a complete dashboard.
    """
    offenders = [
        line.strip()
        for line in template.read_text(encoding="utf-8").splitlines()
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
    css = (STATIC_DIR / "tailwind.css").read_text(encoding="utf-8")

    missing: dict[str, set[str]] = {}
    for template in TEMPLATES:
        for token in _class_tokens(template.read_text(encoding="utf-8")):
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
