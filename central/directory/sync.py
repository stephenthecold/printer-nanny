"""Apply a DirectorySnapshot to one client's end users and groups.

Every rule that protects data an operator or a customer already relies on lives
here, once, rather than three times over in the providers.

The load-bearing ones, and what goes wrong without each:

* **Match on ``directory_id``, never email.** Emails change; object ids do not.
  Matching on email turns a married employee into a leaver plus a stranger and
  strands their printer assignments on the deactivated row.
* **Never touch a ``manual`` row.** Operators hand-create contractors and shared
  accounts. A sync that adopts or deactivates those rows makes the manual path
  untrustworthy, and the operator finds out when somebody loses their printers.
* **Deactivate, never delete.** Assignment history has to survive a leaver, and
  a returning employee should come back to the same row.
* **A partial snapshot deactivates nobody.** Absence means "gone" only if the
  fetch was complete. Otherwise a paging failure reads as mass resignation.
* **Email collisions are reported, not resolved.** See ``_conflict`` below.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import services
from central.directory.base import DirectorySnapshot

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def sync_snapshot(
    db: Session,
    *,
    client: m.Client,
    source: m.DirectorySource,
    snapshot: DirectorySnapshot,
) -> dict:
    """Reconcile ``client``'s ``source``-owned people and groups to ``snapshot``.

    Returns a counts dict suitable for storing on the connection and showing to
    an operator. Never raises for ordinary data problems -- a duplicate email or
    an unknown group member is counted and reported, because one malformed row
    out of three thousand must not abandon the other 2,999 half-applied.
    """
    if source is m.DirectorySource.manual:
        # Would mean "sync owns the hand-made rows", which is precisely the
        # thing the rest of this module exists to prevent.
        raise ValueError("manual is not a syncable source")

    counts = {
        "created": 0, "updated": 0, "reactivated": 0, "deactivated": 0,
        "groups_created": 0, "groups_updated": 0, "members_added": 0,
        "members_removed": 0, "conflicts": 0, "skipped_members": 0,
        "complete": snapshot.complete,
    }
    conflicts: list = []

    # ---------------------------------------------------------------- users --
    existing = {
        row.directory_id: row
        for row in db.scalars(
            select(m.EndUser).where(
                m.EndUser.client_id == client.id,
                m.EndUser.directory_source == source,
                m.EndUser.directory_id.is_not(None),
            )
        )
    }
    # Emails already spoken for by rows this sync does NOT own (manual entries,
    # or another provider's). end_users is UNIQUE(client_id, email), so an
    # insert colliding here would fail the whole flush.
    foreign_emails = {
        email.lower(): src
        for email, src in db.execute(
            select(m.EndUser.email, m.EndUser.directory_source).where(
                m.EndUser.client_id == client.id,
                m.EndUser.email.is_not(None),
                m.EndUser.directory_source != source,
            )
        )
        if email
    }

    seen: set = set()
    by_directory_id: dict = {}

    for user in snapshot.users:
        did = _norm(user.directory_id)
        if not did:
            counts["conflicts"] += 1
            conflicts.append("user with no directory id")
            continue
        seen.add(did)
        email = _norm(user.email)
        row = existing.get(did)

        if row is None and email and email.lower() in foreign_emails:
            # A hand-made row (or another provider's) already owns this address.
            # Adopting it would silently transfer operator-owned data to the
            # directory -- and if the match is wrong (a shared mailbox, an
            # alias), it merges two different people, which is not recoverable.
            # Skipping is. Reported so the operator can merge deliberately.
            counts["conflicts"] += 1
            conflicts.append(
                f"{email} already exists as "
                f"{foreign_emails[email.lower()].value} -- not adopted"
            )
            continue

        if row is None:
            row = m.EndUser(
                client_id=client.id,
                directory_source=source,
                directory_id=did,
                email=email,
                upn=_norm(user.upn),
                display_name=_norm(user.display_name),
                active=user.active,
            )
            db.add(row)
            counts["created"] += 1
        else:
            changed = False
            for attr, value in (
                ("email", email),
                ("upn", _norm(user.upn)),
                ("display_name", _norm(user.display_name)),
            ):
                if getattr(row, attr) != value:
                    setattr(row, attr, value)
                    changed = True
            if row.active != user.active:
                row.active = user.active
                changed = True
                if user.active:
                    counts["reactivated"] += 1
                else:
                    counts["deactivated"] += 1
            if changed:
                counts["updated"] += 1
        by_directory_id[did] = row

    # Absent from the snapshot => gone. Only trustworthy on a complete fetch.
    if snapshot.complete:
        for did, row in existing.items():
            if did not in seen and row.active:
                row.active = False
                counts["deactivated"] += 1

    # Flush before groups so newly-created users have ids to be members with.
    db.flush()

    # --------------------------------------------------------------- groups --
    existing_groups = {
        g.directory_id: g
        for g in db.scalars(
            select(m.EndUserGroup).where(
                m.EndUserGroup.client_id == client.id,
                m.EndUserGroup.directory_source == source,
                m.EndUserGroup.directory_id.is_not(None),
            )
        )
    }
    taken_names = {
        name for (name,) in db.execute(
            select(m.EndUserGroup.name).where(
                m.EndUserGroup.client_id == client.id,
                m.EndUserGroup.directory_source != source,
            )
        )
    }
    # Members may be rows this run did not touch (unchanged people from an
    # earlier sync), so resolve against the client's whole synced population.
    all_synced = {
        row.directory_id: row
        for row in db.scalars(
            select(m.EndUser).where(
                m.EndUser.client_id == client.id,
                m.EndUser.directory_source == source,
                m.EndUser.directory_id.is_not(None),
            )
        )
    }
    all_synced.update(by_directory_id)

    for group in snapshot.groups:
        gid = _norm(group.directory_id)
        name = _norm(group.name)
        if not gid or not name:
            counts["conflicts"] += 1
            conflicts.append("group with no id or name")
            continue

        row = existing_groups.get(gid)
        if row is None:
            if name in taken_names:
                counts["conflicts"] += 1
                conflicts.append(f"group '{name}' already exists -- not adopted")
                continue
            row = m.EndUserGroup(
                client_id=client.id, directory_source=source,
                directory_id=gid, name=name,
            )
            db.add(row)
            db.flush()
            counts["groups_created"] += 1
        elif row.name != name:
            if name in taken_names:
                counts["conflicts"] += 1
                conflicts.append(f"cannot rename group to '{name}' -- name taken")
            else:
                row.name = name
                counts["groups_updated"] += 1

        members = []
        for member_did in group.member_directory_ids:
            member = all_synced.get(_norm(member_did) or "")
            if member is None:
                # Filtered out, or a nested group we did not expand. Skipping
                # one member is right; failing the group would drop everyone.
                counts["skipped_members"] += 1
                continue
            members.append(member)

        # Reuses the tenant-checked, set-difference membership sync so a
        # refresh is not a window in which everybody briefly has no printers.
        added, removed = services.sync_group_members(
            db, group=row, end_users=members
        )
        counts["members_added"] += added
        counts["members_removed"] += removed

    # Groups the directory no longer has: emptied, not deleted. The group may
    # carry printer assignments an operator made, and deleting it would silently
    # discard them; an empty group grants nobody anything and stays visible.
    if snapshot.complete:
        snapshot_gids = {_norm(g.directory_id) for g in snapshot.groups}
        for gid, row in existing_groups.items():
            if gid not in snapshot_gids:
                _, removed = services.sync_group_members(db, group=row, end_users=[])
                counts["members_removed"] += removed

    db.flush()
    if conflicts:
        counts["conflict_detail"] = conflicts[:20]
    return counts


def run_connection(db: Session, conn: m.DirectoryConnection) -> dict:
    """Fetch and apply one connection, recording the outcome on the row.

    Returns the counts dict. A provider failure is recorded and swallowed --
    a directory being unreachable must not deactivate anybody, and must not
    take down a worker cycle that also has alerts to deliver.
    """
    from central.directory.registry import build_provider
    from central.directory.base import DirectoryError

    client = db.get(m.Client, conn.client_id)
    if client is None:
        return {"error": "client missing"}

    conn.last_sync_at = _now()
    try:
        provider = build_provider(conn)
        snapshot = provider.fetch()
    except DirectoryError as exc:
        conn.last_ok = False
        conn.last_error = str(exc)[:500]
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never let one directory kill the cycle
        log.exception("directory sync failed for connection %s", conn.id)
        conn.last_ok = False
        # Class name only: transport exceptions quote URLs and can echo the
        # credential, and this string is rendered in the settings UI.
        conn.last_error = f"unexpected {type(exc).__name__}"
        return {"error": type(exc).__name__}

    counts = sync_snapshot(
        db, client=client, source=conn.provider, snapshot=snapshot
    )
    conn.last_ok = True
    conn.last_error = None
    conn.last_result = counts
    return counts
