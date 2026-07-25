"""The directory provider contract.

Three sources -- Entra ID, Google Workspace, on-prem AD -- with three wildly
different wire protocols and one job: hand back the customer's people and
groups. Everything provider-specific stops at this boundary; ``sync.py`` below
it never learns which directory it is talking to.

The contract is a **pull returning a plain snapshot**, not a stream of DB
mutations, and that shape is the point. A provider that wrote to the database
directly would make every sync rule (never clobber a manual row, deactivate
rather than delete, match on immutable id) something each of the three had to
re-implement identically. As a snapshot, those rules live once, and the entire
sync engine is testable with a hand-built snapshot and no network at all --
which matters here, because no CI runner has an Entra tenant to call.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


class DirectoryError(Exception):
    """A provider could not produce a snapshot.

    Deliberately coarse. Callers do not branch on why a directory is
    unreachable -- they record the failure and leave existing people alone.
    What matters is that the message is safe to store and render: providers
    raise this with a sanitised string, never the raw transport error, because
    those quote request URLs and occasionally echo the credential back.
    """


@dataclass
class DirectoryUser:
    """One person as the directory sees them.

    ``directory_id`` is the immutable object id (Entra objectId, Google id, AD
    objectGUID) and is the only identity the sync engine matches on. Email is
    deliberately not an identity: people get married, change names and get new
    addresses, and a sync keyed on a mutable attribute quietly turns one person
    into two -- the old row deactivated as a leaver, a new row created as a
    stranger, and every printer assignment stranded on the corpse.
    """

    directory_id: str
    email: Optional[str] = None
    upn: Optional[str] = None
    display_name: Optional[str] = None
    # Directories express "this account is disabled" rather than removing it.
    # Honoured as a deactivation, same as absence from the snapshot.
    active: bool = True


@dataclass
class DirectoryGroup:
    """One group, with membership by member ``directory_id``.

    Membership is carried as directory ids rather than resolved users so a
    provider never has to hold the whole user set in memory to describe a group,
    and so a member the snapshot did not include (filtered out, or a nested
    group we did not expand) is simply skipped rather than crashing the sync.
    """

    directory_id: str
    name: str
    member_directory_ids: list = field(default_factory=list)


@dataclass
class DirectorySnapshot:
    """Everything one fetch returned.

    ``complete`` is the safety interlock. Deactivation is inferred from
    *absence*, so a partial fetch -- a paging failure halfway through, a
    provider that gave up after N pages -- would read as "everyone after page 3
    has left the company" and deprovision them. A provider that cannot promise
    it enumerated the whole directory sets this False, and the sync engine
    applies additions and updates but performs **no deactivations**.
    """

    users: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    complete: bool = True


class DirectoryProvider(abc.ABC):
    """Built from a DirectoryConnection row; fetches one snapshot."""

    #: Matches DirectorySource, so a provider and the provenance it stamps on
    #: the rows it creates cannot drift apart.
    key: str = ""

    def __init__(self, config: Optional[dict] = None, secret: Optional[str] = None):
        self.config = config or {}
        self.secret = secret or ""

    @abc.abstractmethod
    def fetch(self) -> DirectorySnapshot:
        """Return the directory's current users and groups.

        Raises DirectoryError with a message safe to persist and render.
        """

    def _require(self, *keys: str) -> None:
        """Fail fast on missing config, before any credential leaves the box."""
        missing = [k for k in keys if not str(self.config.get(k) or "").strip()]
        if missing:
            raise DirectoryError(f"missing required setting(s): {', '.join(missing)}")
