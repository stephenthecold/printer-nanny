"""The one-shot message an operator sees after a redirect -- and its severity.

**The severity is the whole point.** Before this module the channel was a bare
string in ``session["flash"]``, rendered by ``base.html`` into a hardcoded
emerald *success* box. Roughly 237 call sites across twelve modules wrote to it,
and a large minority of them are refusals: "That site belongs to a different
client; no changes saved.", "Rate card name is required.", "Refused: {exc}".
Every one of those was delivered to the operator in the colour the interface
uses for "that worked". The customer portal had its own copy of the same bug, so
a customer whose problem report *failed to send* was told so in green, with the
text they had typed discarded.

That is the same failure the notification channels are forbidden to make -- see
``ChannelResult.sent`` versus ``ok``, and the rule that anything reporting
success without transmitting must say so. The UI was doing exactly what the
delivery layer is not allowed to do.

WHY A DICT AND NOT TWO SESSION KEYS
-----------------------------------
A second key (``flash_level``) can desynchronise from the first: one handler
sets both, the next sets only the text, and the stale level colours the new
message. One value carrying both cannot drift, and it pops atomically.

WHY THE LEVEL IS VALIDATED WHEN IT IS WRITTEN
---------------------------------------------
``note()`` resolves an unknown tone through ``tones.get(tone, tones['info'])``,
so a typo renders info-blue and looks deliberate -- the silent-fallback pattern
this codebase keeps being bitten by. Writing is our own code, reached by tests,
so an unknown level raises there. *Reading* is permissive: the value arrives
from a signed cookie that may have been minted by an older build, and a page
that 500s because of a stale cookie is worse than one that shows a neutral box.

The same asymmetry covers the upgrade: a session written by the previous version
holds a plain string. It is read as ``info`` rather than dropped, because
discarding it would silently eat the one message an operator was waiting for
across the deploy.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Session key. One key, holding both fields.
FLASH_KEY = "flash"

#: Levels, in the order an operator would rank them. ``success`` is separate
#: from ``info`` because "it worked" and "here is something you should know"
#: are different claims and the whole defect was conflating them.
LEVELS = ("success", "info", "warning", "error")

#: What a level renders as. Maps onto ``_components.note()`` tones, which is why
#: ``note`` needed a ``success`` tone added -- its absence is precisely why
#: ``base.html`` hand-rolled an emerald box instead of using the component layer.
_TONE_FOR_LEVEL = {
    "success": "success",
    "info": "info",
    "warning": "warning",
    "error": "error",
}

#: Level a value of unknown shape is shown as. Neutral on purpose: guessing
#: "success" is how the original defect read, and guessing "error" would alarm
#: an operator about a message that may well have been good news.
FALLBACK_LEVEL = "info"


class FlashLevelError(ValueError):
    """A level this module does not know how to render.

    Raised at write time only. Its own type so a test can assert the refusal
    without matching on a message.
    """


def flash(request: Any, text: str, level: str = "success") -> None:
    """Queue one message for the next page this session renders.

    ``level`` defaults to ``success`` because the majority of these are
    confirmations ("Printer updated.") and defaulting to the majority keeps the
    migration honest: a call site that has not been classified yet renders
    exactly as it did before, so this refactor cannot quietly restyle a message
    nobody has looked at. Every refusal must pass its level explicitly.
    """
    if level not in LEVELS:
        raise FlashLevelError(
            "%r is not a flash level; use one of %s" % (level, ", ".join(LEVELS))
        )
    request.session[FLASH_KEY] = {"level": level, "text": str(text)}


def error(request: Any, text: str) -> None:
    """Shorthand for the case the old channel could not express at all."""
    flash(request, text, level="error")


def pop(request: Any) -> Optional[Dict[str, str]]:
    """Take the queued message, or None. Never raises.

    Returns ``{"level": ..., "text": ...}``. Jinja resolves ``flash.level`` on a
    dict via item lookup, so templates read it as an attribute either way.
    """
    try:
        raw = request.session.pop(FLASH_KEY, None)
    except Exception:  # noqa: BLE001 - a session store that will not answer is not fatal
        return None
    if raw is None:
        return None
    # Written by a previous build, still in an unexpired cookie.
    if isinstance(raw, str):
        return {"level": FALLBACK_LEVEL, "text": raw} if raw else None
    if isinstance(raw, dict):
        text = raw.get("text")
        if not text:
            return None
        level = raw.get("level")
        if level not in LEVELS:
            level = FALLBACK_LEVEL
        return {"level": level, "text": str(text)}
    return {"level": FALLBACK_LEVEL, "text": str(raw)}


def tone_for(level: Optional[str]) -> str:
    """The ``note()`` tone for a level. Templates call this, not a dict lookup."""
    return _TONE_FOR_LEVEL.get(level or "", "info")
