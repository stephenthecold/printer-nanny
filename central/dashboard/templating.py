"""The one Jinja environment the dashboard renders through.

Four modules used to each construct their own ``Jinja2Templates`` over the same
directory. That was merely wasteful until globals had to be registered on it:
with four environments, a global added to three of them is a page that renders
without a CSRF token, which submits and 403s. One environment makes that
impossible rather than merely unlikely -- the same reasoning as
``_components.html`` owning the class strings.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from central.csrf import install_jinja_globals

TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# ``csrf_token()`` / ``csrf_field()`` -- see central/csrf.py. Registered here so
# every template in the app has them regardless of which router rendered it.
install_jinja_globals(templates.env)


def render_csrf_error(request: Request, message: str):
    """The 403 page a refused form post lands on.

    Deliberately standalone rather than extending base.html: base.html needs
    branding, the nav's pending count and the worker banner, all of which come
    from the database, and this handler runs after the request's DB dependency
    has been torn down. An error page that can itself 500 is worse than a plain
    one, so this needs nothing but the vendored stylesheet.
    """
    return templates.TemplateResponse(
        request, "csrf_error.html", {"message": message}, status_code=403
    )
