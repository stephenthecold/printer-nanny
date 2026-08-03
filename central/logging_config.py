"""The one place that decides how this project's log records reach an operator.

Both long-running processes call ``configure_logging()``: ``central.main`` (which
``uvicorn central.main:app`` imports) and ``central.worker.run``. One helper
rather than two ``logging.basicConfig`` calls, because two copies drift -- and
they already had: the worker configured logging and the **api container did
not**, so every ``log.info()`` in ``central/`` was discarded there and every
warning reached stderr through ``logging.lastResort``, whose format is a bare
``%(message)s`` -- no timestamp, no level, no logger name, nothing to correlate
against a request.

Three decisions here are load-bearing rather than stylistic:

**The verbosity knob moves this project's loggers, never the root logger.**
``LOG_LEVEL`` sets the level of ``central`` and ``printer_nanny``; the root
logger keeps its WARNING default, so third-party libraries are still *heard*
when they warn and are silent below that. That is a security boundary, not
tidiness. ``httpx`` logs ``'HTTP Request: %s %s ...'`` with the full request URL
at **INFO**, and a Slack / Teams / generic-webhook URL *is* the credential -- so
a root-level INFO would print operator secrets into the container log on every
alert. One notch further down is worse: SQLAlchemy emits every statement *with
its bound parameters* at DEBUG, which is password hashes, API-key hashes and
Fernet ciphertext. Scoping the knob makes both impossible instead of filtered,
and a dependency added later cannot reopen it.

**A record propagates to an ancestor's handlers regardless of that ancestor's
level** (``Logger.callHandlers`` consults handler levels only). That is what
makes the split above work: ``central.db`` at INFO still reaches the root
handler installed here while root itself stays at WARNING. Verified, not
assumed.

**It does not fight uvicorn.** ``uvicorn.Config.__init__`` applies its own
``dictConfig`` *before* ``load()`` imports the app, and that config names only
the ``uvicorn*`` loggers -- no ``root`` key -- so installing a root handler at
import time neither clobbers uvicorn's handlers nor is clobbered by them. No
duplicate lines either: uvicorn marks ``uvicorn`` and ``uvicorn.access``
``propagate: False``, so its records stop before reaching us.

Output goes to **stderr**, matching ``basicConfig``'s and uvicorn's defaults and
keeping stdout clean -- ``python -m central.worker.run --once`` prints its cycle
summary there, and interleaved log lines would corrupt anything parsing it.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional, TextIO, Union

from central.config import settings

# Timestamp, level and logger name on every record. The name is what tells an
# operator whether a line came from the alert path, the audit writer or the
# installer, and it is the field the worker's old format left out.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# ISO-8601 with the UTC offset, so a line is unambiguous when the container's
# clock and the reader's are not in the same zone.
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# The logger namespaces this project owns. `central.*` is most of the server;
# `printer_nanny.*` is what the worker, the MSI builder and the installer route
# picked. Deliberately NOT `printer_nanny_agent` -- that is a separate top-level
# name (logger hierarchy is dot-separated, so it is not a child of these), it
# ships as its own package, and it configures its own logging in its CLI.
PROJECT_LOGGERS = ("central", "printer_nanny")

# Named so a second call finds the handler it already installed instead of
# stacking another one and doubling every line.
HANDLER_NAME = "printer-nanny"

DEFAULT_LEVEL = logging.INFO


def resolve_level(
    value: Union[int, str, None],
    default: Optional[int] = DEFAULT_LEVEL,
) -> Optional[int]:
    """Turn an operator-supplied level into a logging constant, or ``default``.

    Accepts a name (``INFO``, case-insensitive, including the ``WARN`` alias) or
    a number. Returns ``default`` for anything else so a typo in ``LOG_LEVEL``
    degrades to the normal level rather than taking the container down on boot --
    losing the ability to start is a far worse outcome than losing a preference.

    ``NOTSET``/0 is rejected on purpose: on these loggers it does not mean
    "verbose", it means "inherit from root", which would silently switch INFO
    back off and look exactly like the bug this module exists to fix.
    """
    # bool is a subclass of int, and True would otherwise resolve to level 1.
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    text = str(value or "").strip()
    if not text:
        return default
    if text.lstrip("+").isdigit():
        number = int(text)
        return number if number > 0 else default
    named = logging.getLevelName(text.upper())
    if isinstance(named, int) and named > 0:
        return named
    return default


def configure_logging(
    level: Union[int, str, None] = None,
    *,
    stream: Optional[TextIO] = None,
) -> logging.Handler:
    """Install this project's log handler and set its loggers' level. Idempotent.

    ``level`` defaults to the ``LOG_LEVEL`` environment setting. Returns the
    handler so a caller (a test, mainly) can read back what was installed.
    """
    raw = settings.log_level if level is None else level
    resolved = resolve_level(raw, default=None)
    unusable = resolved is None
    if resolved is None:
        resolved = DEFAULT_LEVEL

    root = logging.getLogger()
    handler = next((h for h in root.handlers if h.get_name() == HANDLER_NAME), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr if stream is None else stream)
        handler.set_name(HANDLER_NAME)
        root.addHandler(handler)
    elif stream is not None:
        handler.setStream(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    for name in PROJECT_LOGGERS:
        logging.getLogger(name).setLevel(resolved)

    if unusable:
        # Now that a handler exists, say so. Truncated because the value is
        # operator-supplied env; it is a level name, never a credential, but an
        # unbounded string does not belong in a log line either.
        logging.getLogger("central.logging").warning(
            "LOG_LEVEL %r is not a log level; using %s",
            str(raw)[:40],
            logging.getLevelName(DEFAULT_LEVEL),
        )
    return handler
