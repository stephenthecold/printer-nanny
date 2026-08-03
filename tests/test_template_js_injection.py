"""HTML escaping is not JavaScript escaping — enforced across every template.

Jinja's autoescaping is correct for an HTML *text* or *attribute* context and
gives no protection at all in a *script* context. An inline event handler is a
script context wearing an attribute's clothes::

    onsubmit="return confirm('Delete {{ name }}?')"

Autoescaping turns the operator's ``'`` into ``&#39;``, but the HTML parser
decodes character references *before* the attribute value is handed to the
JavaScript compiler, so JS sees a bare ``'``, the string literal closes, and a
client named ``x'); alert(1); //`` executes. Escaping ran and did nothing.

``|tojson`` is **not** the fix, and this is the trap worth stating loudly because
it was shipped here as one. ``tojson`` escapes ``<``, ``>``, ``&`` and ``'`` —
but not ``"`` — and it emits the JSON string's own surrounding double quotes. So
inside a *double-quoted* attribute the very first character it writes terminates
the attribute, and everything after it is parsed as more attributes on the tag.
A subscription named ``a" onmouseover="alert(1)`` rendered a genuine
``onmouseover`` handler on the form element. ``tojson`` is safe in a ``<script>``
block or a single-quoted attribute, and nowhere else.

The rule this module enforces is therefore a bright line rather than a judgement
call: **no Jinja expression of any kind inside an inline event-handler
attribute.** Values travel in ``data-`` attributes and are read back through
``dataset``, where they are only ever data and never parsed as script. A bright
line is the point — "escape it correctly for the nested context" is a rule that
gets got wrong, and got wrong twice here already.

The scan is over template *source* because that is where the rule lives and
because it covers every branch, including the ones a given fixture never
renders. ``test_scanner_catches_the_shapes_it_is_meant_to_catch`` runs the
scanner against known-bad samples so it cannot quietly rot into a no-op that
passes because it stopped matching anything.
"""

from __future__ import annotations

import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "central" / "dashboard" / "templates"

# Inline event-handler attributes: on* = "..." or on* = '...'. An attribute value
# cannot contain its own delimiter, so the negated character classes correctly
# span newlines for handlers written across several lines.
_HANDLER = re.compile(
    r"""\bon[a-zA-Z]+\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)')""",
    re.VERBOSE,
)
# A Jinja expression or statement of any kind.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
# Jinja comments. Stripped before scanning: they are never rendered, so they
# cannot be a sink — and the comments explaining this very rule quote the
# attack strings, which would otherwise report themselves as vulnerabilities.
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
# javascript:/vbscript: URL schemes, which are script contexts in an href/src.
_SCRIPT_URL = re.compile(r"""(?:href|src|action|formaction)\s*=\s*["']?\s*(?:javascript|vbscript):""", re.I)


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def strip_comments(text: str) -> str:
    """Blank out Jinja comments, preserving newlines so line numbers survive."""
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def scan_handlers(text: str):
    """Yield (line, handler_source) for every inline handler carrying Jinja."""
    text = strip_comments(text)
    for match in _HANDLER.finditer(text):
        body = match.group("dq") if match.group("dq") is not None else match.group("sq")
        if _JINJA.search(body):
            yield _line_of(text, match.start()), match.group(0)


def test_no_inline_event_handler_interpolates_a_jinja_expression():
    """The core invariant. Any hit is a stored-XSS sink, not a style nit."""
    findings = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for line, handler in scan_handlers(text):
            findings.append(f"{path.relative_to(TEMPLATES.parents[2])}:{line}: {handler.strip()[:160]}")
    assert not findings, (
        "Inline event handlers must not interpolate Jinja expressions — HTML "
        "escaping is not JS escaping, and |tojson breaks out of a double-quoted "
        "attribute. Pass the value in a data- attribute and read it via "
        "this.dataset.<name> instead:\n  " + "\n  ".join(findings)
    )


def test_no_javascript_url_schemes():
    """A ``javascript:`` URL is a script context too, and needs no handler."""
    findings = []
    for path in _templates():
        text = strip_comments(path.read_text(encoding="utf-8"))
        for match in _SCRIPT_URL.finditer(text):
            findings.append(f"{path.name}:{_line_of(text, match.start())}")
    assert not findings, findings


def test_templates_do_not_use_tojson_in_an_attribute():
    """``tojson`` in a double-quoted attribute is a breakout, not an escape.

    Covered by the handler scan already (it contains ``{{``), but asserted on
    its own so the *reason* is recorded where somebody reaching for ``tojson``
    will find it.
    """
    findings = []
    for path in _templates():
        text = strip_comments(path.read_text(encoding="utf-8"))
        for match in _HANDLER.finditer(text):
            body = match.group("dq") if match.group("dq") is not None else match.group("sq")
            if "tojson" in body:
                findings.append(f"{path.name}:{_line_of(text, match.start())}")
    assert not findings, findings


# --------------------------------------------------------------------------- #
# The scanner's own self-test.
#
# A source scanner that stops matching passes forever. These samples are the
# exact shapes this codebase actually shipped, so a regex edit that blinds the
# scanner fails here rather than going unnoticed until the next audit.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sample", [
    # The original: straight interpolation into a JS string literal.
    """<form onsubmit="return confirm('Delete {{ name }}?')">""",
    # Single-quoted attribute, same defect.
    """<form onsubmit='return confirm("Delete {{ name }}?")'>""",
    # onclick on a button rather than onsubmit on a form.
    """<button onclick="return confirm('Remove {{ s.cidr }}?')">x</button>""",
    # The withdrawn |tojson "fix" — breaks out of the double-quoted attribute.
    """<form onsubmit="return confirm('Rotate ' + {{ s.name|tojson }} + '?')">""",
    # A handler split across lines, which a line-based scan would miss.
    """<form\n  onsubmit="return confirm(\n     'Delete {{ name }}?')">""",
    # A statement rather than an expression.
    """<button onclick="{% if x %}f(){% endif %}">x</button>""",
    # A handler that is not one of the two this app happens to use today.
    """<img onerror="log('{{ name }}')" src="x">""",
])
def test_scanner_catches_the_shapes_it_is_meant_to_catch(sample):
    assert list(scan_handlers(sample)), f"scanner went blind on: {sample!r}"


@pytest.mark.parametrize("sample", [
    # The fix: value as data, read through the DOM.
    """<form data-name="{{ name }}" onsubmit="return confirm('Delete ' + this.dataset.name + '?')">""",
    # Jinja in a non-handler attribute is normal and correctly autoescaped.
    """<a href="/manage/clients/{{ c.id }}" class="{{ btn('primary') }}">x</a>""",
    # A constant handler carries no untrusted value at all.
    """<form onsubmit="return confirm('Replace the live database?')">""",
    # "on" appearing inside another attribute name must not trip the scan.
    """<div data-onclick-note="{{ x }}">y</div>""",
])
def test_scanner_does_not_fire_on_safe_shapes(sample):
    assert not list(scan_handlers(sample)), f"false positive on: {sample!r}"


def test_the_scan_actually_reached_the_templates():
    """Guard against the whole suite passing because the path was wrong."""
    found = _templates()
    assert len(found) > 20, f"only {len(found)} templates found under {TEMPLATES}"
    # And that the corpus really does contain inline handlers to police.
    handlers = sum(
        len(_HANDLER.findall(strip_comments(p.read_text(encoding="utf-8")))) for p in found
    )
    assert handlers > 10, f"only {handlers} inline handlers seen; the scan may be broken"


def test_comment_stripping_keeps_line_numbers_and_hides_only_comments():
    """Stripping must not shift the line numbers the failure message reports."""
    text = '<a>\n{# a comment naming onclick="{{ x }}"\n   over two lines #}\n<b onclick="{{ y }}">'
    stripped = strip_comments(text)
    assert stripped.count("\n") == text.count("\n")
    hits = list(scan_handlers(text))
    assert len(hits) == 1, hits
    assert hits[0][0] == 4, f"line number drifted: {hits[0][0]}"
