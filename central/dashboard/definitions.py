"""Device/model definitions: the operator surface.

This is the page that makes "we bought a printer model nobody has decoded yet"
an operator task instead of an engineering one. An operator writes the OIDs and
how to read them; agents pick it up on their next poll.

SECURITY POSTURE OF THIS FILE, EXPLICITLY
-----------------------------------------
Everything posted here is attacker-shaped input by this project's standing rule
(it becomes instructions that every agent in the fleet acts on), so:

* The spec is parsed as JSON with a byte cap applied to the RAW TEXT first,
  then handed to ``device_definitions.validate_definition``. Nothing is stored
  that did not survive that: the column holds the validator's normalised output,
  never the operator's text.
* The validator is the boundary -- closed vocabulary, numeric-only OIDs, no
  regular expressions at all, unknown keys refused, every length bounded. It is
  the same code the agent runs again on receipt, so a bad definition cannot be
  smuggled past one end by the other.
* Every route is tenant-checked (a client-scoped definition must name a client
  that exists) and audited, including the ``override_builtin`` flag, because
  that flag is the one that lets a definition displace hardware-proven code.
* No value is interpolated anywhere: the ORM parameterises, and Jinja escapes
  the spec back into the textarea.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.audit import record
from central.dashboard.manage import _flash, _manager, _pop_flash, _redirect, _tpl
from central.db import get_db
from central.device_definitions import (
    MAX_PAYLOAD_BYTES,
    DefinitionError,
    canonical_json,
    validate_definition,
)

router = APIRouter(prefix="/manage", tags=["manage"])

#: A single definition's JSON text, capped before it is parsed. The whole-feed
#: cap is what an agent enforces; one row must obviously be under it, and the
#: parse is what we are declining to spend on a hostile body.
MAX_SPEC_TEXT = MAX_PAYLOAD_BYTES // 4

#: Shown in the empty textarea. A worked example is the difference between a
#: feature an operator can use and one they file a ticket about.
EXAMPLE_SPEC = """{
  "key": "acme-9000-series",
  "match": {"enterprise": "12345", "model": "AC-9000"},
  "supplies": [
    {
      "oid": "1.3.6.1.4.1.12345.1.4.1.1",
      "type": "toner", "color": "black",
      "description": "Black Toner",
      "decode": {"kind": "percent"}
    },
    {
      "oid": "1.3.6.1.4.1.12345.1.4.2.1",
      "type": "drum",
      "decode": {"kind": "ratio", "max": 30000}
    }
  ],
  "meters": [
    {"oid": "1.3.6.1.4.1.12345.1.6.1", "meter": "mono"}
  ],
  "status_text": {"oid": "1.3.6.1.4.1.12345.1.1.3"}
}"""


def _parse_spec(text: str) -> dict:
    """JSON text -> a validated, normalised definition. Raises DefinitionError."""
    if len(text or "") > MAX_SPEC_TEXT:
        raise DefinitionError(
            f"definition is {len(text)} characters, over the {MAX_SPEC_TEXT} cap"
        )
    try:
        raw = json.loads(text or "")
    except RecursionError:
        # A JSON document nested thousands deep exhausts the parser's stack
        # before the depth check downstream ever sees a shape. Named rather
        # than swallowed, so the operator sees a reason.
        raise DefinitionError("definition is nested too deeply to parse")
    except ValueError as exc:
        raise DefinitionError(f"not valid JSON: {exc}")
    return validate_definition(raw)


@router.get("/definitions", response_class=HTMLResponse)
def definitions_page(
    request: Request,
    edit: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    rows = list(
        db.scalars(
            select(m.DeviceDefinition).order_by(
                m.DeviceDefinition.client_id.is_(None).desc(),
                m.DeviceDefinition.key,
            )
        )
    )
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    editing = db.get(m.DeviceDefinition, edit) if edit else None
    return _tpl(
        request,
        "definitions.html",
        db,
        user=user,
        definitions=rows,
        clients=clients,
        editing=editing,
        # Round-trip the stored canonical form, pretty-printed. An operator
        # editing a definition sees exactly what the agents were served, not
        # what somebody once typed.
        editing_spec=(
            json.dumps(editing.spec, indent=2, sort_keys=True) if editing else EXAMPLE_SPEC
        ),
        example_spec=EXAMPLE_SPEC,
        flash=_pop_flash(request),
    )


@router.post("/definitions/save")
def save_definition(
    request: Request,
    definition_id: str = Form(""),
    name: str = Form(""),
    client_id: str = Form(""),
    spec: str = Form(""),
    enabled: str = Form(""),
    enabled_present: str = Form("1"),
    override_builtin: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create or update one definition.

    ``enabled_present`` is the checkbox presence marker this codebase already
    uses for ``subnets.trusted``: an unchecked box posts nothing, so without it
    a handler cannot tell "the operator unticked this" from "this form did not
    carry the field" -- and an edit would silently re-enable a definition
    somebody deliberately turned off.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    row = db.get(m.DeviceDefinition, int(definition_id)) if definition_id.strip() else None
    if definition_id.strip() and row is None:
        _flash(request, "That definition no longer exists.")
        return _redirect("/manage/definitions")

    scope: Optional[int] = None
    if client_id.strip():
        try:
            scope = int(client_id)
        except (TypeError, ValueError):
            _flash(request, "Unknown client.")
            return _redirect("/manage/definitions")
        if db.get(m.Client, scope) is None:
            # A scope naming a client that does not exist would be served to
            # nobody and look, in the list, like it was working.
            _flash(request, "That client no longer exists.")
            return _redirect("/manage/definitions")

    try:
        parsed = _parse_spec(spec)
    except DefinitionError as exc:
        _flash(request, f"Refused: {exc}")
        return _redirect(f"/manage/definitions?edit={row.id}" if row else "/manage/definitions")

    # The checkbox is authoritative over whatever the JSON said, because the
    # checkbox is what the operator is looking at and is what the audit line
    # will quote.
    parsed["override_builtin"] = bool(override_builtin)

    # Uniqueness is also a DB constraint (a partial unique index for the global
    # scope, since SQL's UNIQUE treats NULLs as distinct and would let every
    # global key duplicate). Checked here too so the operator gets a sentence
    # instead of a 500.
    clash = db.scalar(
        select(m.DeviceDefinition).where(
            m.DeviceDefinition.key == parsed["key"],
            m.DeviceDefinition.client_id.is_(None) if scope is None
            else m.DeviceDefinition.client_id == scope,
        )
    )
    if clash is not None and (row is None or clash.id != row.id):
        _flash(
            request,
            f"A definition with key {parsed['key']!r} already exists in that scope.",
        )
        return _redirect("/manage/definitions")

    is_new = row is None
    if row is None:
        row = m.DeviceDefinition(created_by_user_id=user.id)
        db.add(row)

    changed = is_new or canonical_json(row.spec) != canonical_json(parsed)
    row.key = parsed["key"]
    row.name = (name or "").strip()[:200] or parsed["key"]
    row.client_id = scope
    row.spec = parsed
    row.notes = (notes or "").strip()[:1000] or None
    if enabled_present:
        row.enabled = bool(enabled)
    if changed and not is_new:
        row.revision = (row.revision or 1) + 1
    row.updated_at = m.utcnow()
    db.flush()

    record(
        db, request, user,
        "device_definition.create" if is_new else "device_definition.update",
        f"device_definition:{row.id}",
        # override_builtin is named in the audit line because it is the flag
        # that lets a definition displace a hardware-proven provider. "Never
        # silently" has to be true in the record as well as on the printer.
        f"key={row.key!r} scope={'client:%d' % scope if scope else 'global'} "
        f"enabled={row.enabled} override_builtin={parsed['override_builtin']} "
        f"revision={row.revision} supplies={len(parsed.get('supplies') or [])} "
        f"meters={len(parsed.get('meters') or [])}",
    )
    db.commit()
    _flash(
        request,
        f"Saved {row.key}. Agents pick it up on their next poll."
        if row.enabled else f"Saved {row.key} (disabled — agents will drop it).",
    )
    return _redirect("/manage/definitions")


@router.post("/definitions/{definition_id}/delete")
def delete_definition(
    definition_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    row = db.get(m.DeviceDefinition, definition_id)
    if row is None:
        _flash(request, "That definition no longer exists.")
        return _redirect("/manage/definitions")

    record(
        db, request, user, "device_definition.delete", f"device_definition:{row.id}",
        f"key={row.key!r} scope={'client:%d' % row.client_id if row.client_id else 'global'}",
    )
    db.delete(row)
    db.commit()
    # Worth saying: deletion changes the feed digest, so the next poll ships the
    # full remaining set and the withdrawn definition stops being applied. There
    # is no tombstone to miss, which is why the feed is a full set rather than a
    # delta.
    _flash(request, "Definition removed. Agents drop it on their next poll.")
    return _redirect("/manage/definitions")
