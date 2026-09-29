"""Moving the till's salesperson off the old table onto the staff list (ticket 07, §29).

Every old salesperson row is matched to a staff record at the same store by a
rule that cannot guess:

* the one staff record at that store whose staff code is the old row's code, or
* when no code matches at all, the one staff record at that store whose name is
  the old row's name.

"At that store" means the person held an assignment there at some time, so a
seller who has since moved or left can still be found. Case and spacing are
ignored; nothing else is. Two candidates, or a code and a name pointing at
different people, is not a match: the row is left for Admin with the reason.

The rule is pure: ``match_salespeople`` and the tests feed it plain rows.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

RULE_CODE = "code"
RULE_NAME = "name"
RULE_ADMIN = "admin"


@dataclass(frozen=True)
class OldSeller:
    """One row of the old salesperson table, frozen as it was."""

    id: int
    store_id: int
    code: str
    name: str


@dataclass(frozen=True)
class StaffAtStore:
    """A staff record that has held an assignment at a store."""

    staff_id: uuid.UUID
    store_id: int
    staff_code: str
    display_name: str


@dataclass(frozen=True)
class MatchOutcome:
    staff_id: uuid.UUID | None
    rule: str
    reason: str


def normalise(text: str) -> str:
    """Case and spacing only: "Ravi  Kumar" is "ravi kumar", "Ravi K." is not."""
    return " ".join((text or "").split()).casefold()


def _only(ids: set[uuid.UUID]) -> uuid.UUID | None:
    return next(iter(ids)) if len(ids) == 1 else None


def _outcome(old: OldSeller, staff: list[StaffAtStore]) -> MatchOutcome:
    code, name = normalise(old.code), normalise(old.name)
    by_code = {s.staff_id for s in staff if code and normalise(s.staff_code) == code}
    by_name = {s.staff_id for s in staff if name and normalise(s.display_name) == name}
    if len(by_code) > 1:
        return MatchOutcome(
            None, "", f"{len(by_code)} staff records at this store share this code."
        )
    code_match = _only(by_code)
    if code_match is not None:
        if by_name and by_name != {code_match}:
            return MatchOutcome(
                None, "", "The code and the name point to different staff records at this store."
            )
        return MatchOutcome(code_match, RULE_CODE, "")
    if len(by_name) > 1:
        return MatchOutcome(
            None, "", f"{len(by_name)} staff records at this store share this name."
        )
    name_match = _only(by_name)
    if name_match is not None:
        return MatchOutcome(name_match, RULE_NAME, "")
    return MatchOutcome(None, "", "No staff record at this store has this code or name.")


def match_old_sellers(
    old_rows: list[OldSeller], staff: list[StaffAtStore]
) -> dict[int, MatchOutcome]:
    """An outcome for every old row: a certain match, or the reason there is none."""
    at_store: dict[int, list[StaffAtStore]] = defaultdict(list)
    for row in staff:
        at_store[row.store_id].append(row)
    return {old.id: _outcome(old, at_store.get(old.store_id, [])) for old in old_rows}


# -- the old table (plain SQL: the model is gone once 0029 has run) ------------------


def unmatched_count(cursor: Any) -> int:
    cursor.execute("SELECT count(*) FROM sell_salesperson_match WHERE staff_id IS NULL")
    return int(cursor.fetchone()[0])


def old_table_exists(cursor: Any) -> bool:
    cursor.execute("SELECT to_regclass('public.sell_salesman') IS NOT NULL")
    return bool(cursor.fetchone()[0])


class OldTableStillNeeded(Exception):  # noqa: N818 - a refusal, named for what it says
    """The old table is dropped only once every row is matched."""


def drop_old_table(cursor: Any) -> bool:
    """Drop the old salesperson table, only once no row is unmatched.

    Returns False when it is already gone. Raises `OldTableStillNeeded` while any
    row waits for Admin: the frozen copy holds everything, but the ticket keeps
    the original until the move is complete, so nothing about an unresolved row
    rests on the copy alone.
    """
    left = unmatched_count(cursor)
    if left:
        raise OldTableStillNeeded(
            f"{left} old salesperson row(s) still have no staff record. Admin resolves them "
            "on Setup, Salesperson Matches; then run this again."
        )
    if not old_table_exists(cursor):
        return False
    cursor.execute("DROP TABLE sell_salesman")
    return True
