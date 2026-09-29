"""Who can be named as the salesperson on a bill: the staff list (ticket 07, ST-POS-2).

The till's picker lists the staff **active at this store now**: a staff record
marked as a salesperson, not retired, whose person is active, with a primary
assignment at this store that has started and not ended. It is the only list
the till reads, for every store, whether or not the store's "staff-list" switch
is on - the old salesperson table is gone and there is nothing else to read.

The till is sent only what the picker shows: the staff record's id and name
(overall PRD §10.4). No code, phone or anything else about the person.

A bill that arrives names its seller either way:

* a staff record (every till since the move), which must have been placed at
  this store at some time - an offline bill can land after the person moved on,
  and refusing it would lose a printed bill; or
* an old salesperson row's id (a bill queued by a till that had not heard of
  the move), which lands on that row's frozen copy, `SalespersonMatch`.

Either way the line keeps the seller's code and name as they were.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils import timezone

from accounts.goods_models import HumanIdentity, Staff, StaffAssignment
from masters.goods_models import EffectiveVersionPeriod
from masters.models import Store
from sell.models import SalespersonMatch


@dataclass(frozen=True)
class Seller:
    """The seller a line is written with."""

    staff: Staff | None
    match: SalespersonMatch | None
    code: str
    name: str


@dataclass(frozen=True)
class StaffRow:
    """One person on a store's staff list."""

    staff_id: uuid.UUID
    staff_code: str
    display_name: str
    salesperson: bool
    active: bool
    assigned_from: datetime | None
    assigned_to: datetime | None


def _periods_at(store_ids: list[int]) -> list[tuple[StaffAssignment, EffectiveVersionPeriod]]:
    """Every primary assignment at these stores that took effect, with its period."""
    rows = list(StaffAssignment.objects.filter(site_id__in=store_ids, primary=True))
    periods = {
        period.target_id: period
        for period in EffectiveVersionPeriod.objects.filter(
            target_kind="assignment", target_id__in=[row.pk for row in rows]
        )
    }
    return [(row, periods[row.pk]) for row in rows if row.pk in periods]


def _covers(period: EffectiveVersionPeriod, at: datetime) -> bool:
    return period.effective_from <= at and (period.effective_to is None or period.effective_to > at)


def _is_active(staff: Staff, human: HumanIdentity, at: datetime) -> bool:
    retired = staff.retired_at is not None and staff.retired_at <= at
    return human.active and not retired


def staff_list(store: Store, at: datetime | None = None) -> list[StaffRow]:
    """Everybody placed at this store now or before: who is here, and who is active.

    One row per person. Someone who moved on shows as not active here, with the
    date their time at this store ended.
    """
    at = at or timezone.now()
    placed = _periods_at([store.pk])
    staff = {
        row.pk: row
        for row in Staff.objects.select_related("human").filter(
            pk__in={assignment.staff_id for assignment, _ in placed}
        )
    }
    latest: dict[uuid.UUID, tuple[StaffAssignment, EffectiveVersionPeriod]] = {}
    for assignment, period in placed:
        if period.effective_from > at:
            continue
        seen = latest.get(assignment.staff_id)
        if seen is None or period.effective_from > seen[1].effective_from:
            latest[assignment.staff_id] = (assignment, period)
    rows: list[StaffRow] = []
    for staff_id, (_, period) in latest.items():
        person = staff[staff_id]
        here = _covers(period, at)
        rows.append(
            StaffRow(
                staff_id=staff_id,
                staff_code=person.human.staff_code,
                display_name=person.human.display_name,
                salesperson=person.salesperson,
                active=here and _is_active(person, person.human, at),
                assigned_from=period.effective_from,
                assigned_to=period.effective_to,
            )
        )
    rows.sort(key=lambda row: (not row.active, row.display_name.casefold(), row.staff_code))
    return rows


def active_salespeople(store: Store, at: datetime | None = None) -> list[StaffRow]:
    """The till's picker: salespeople active at this store now, by name."""
    return [row for row in staff_list(store, at) if row.active and row.salesperson]


def ever_placed_at(store: Store, staff_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these staff records have held an assignment at this store."""
    if not staff_ids:
        return set()
    return {
        assignment.staff_id
        for assignment, _ in _periods_at([store.pk])
        if assignment.staff_id in staff_ids
    }


class SellerBook:
    """The sellers a bill's lines may name, looked up once per bill."""

    def __init__(self, store: Store, lines: list[dict[str, Any]]) -> None:
        self.store = store
        wanted = {line["salesperson"] for line in lines if line.get("salesperson")}
        # Ticket 08: the people a split line is shared between.
        wanted |= {
            share["salesperson"]
            for line in lines
            for share in (line.get("shares") or [])
            if share.get("salesperson")
        }
        known = ever_placed_at(store, wanted)
        self._staff = {
            row.pk: row for row in Staff.objects.select_related("human").filter(pk__in=known)
        }
        old_ids = {line["salesman"] for line in lines if line.get("salesman")}
        self._old = {
            row.pk: row
            for row in SalespersonMatch.objects.select_related("staff__human").filter(
                pk__in=old_ids, store=store
            )
        }

    def seller_for(self, line: dict[str, Any]) -> Seller | None:
        """The line's seller, or None when it names nobody at this store."""
        staff_id = line.get("salesperson")
        if staff_id:
            staff = self._staff.get(staff_id)
            if staff is None:
                return None
            return Seller(
                staff=staff,
                match=None,
                code=staff.human.staff_code,
                name=staff.human.display_name,
            )
        old = self._old.get(line.get("salesman") or 0)
        if old is None:
            return None
        return Seller(staff=None, match=old, code=old.code, name=old.name)


def placed_at_any(store_ids: list[int]) -> dict[int, list[uuid.UUID]]:
    """For each store, the staff records ever placed there."""
    found: dict[int, list[uuid.UUID]] = {}
    for assignment, _ in _periods_at(store_ids):
        found.setdefault(assignment.site_id, [])
        if assignment.staff_id not in found[assignment.site_id]:
            found[assignment.site_id].append(assignment.staff_id)
    return found


def people_placed_at(store_ids: list[int]) -> dict[int, list[Staff]]:
    """Staff records (with their person) ever placed at each store."""
    placed = placed_at_any(store_ids)
    staff = {
        row.pk: row
        for row in Staff.objects.select_related("human").filter(
            pk__in={pk for ids in placed.values() for pk in ids}
        )
    }
    return {
        store_id: sorted((staff[pk] for pk in ids), key=lambda s: s.human.display_name.casefold())
        for store_id, ids in placed.items()
    }


def ensure_salesperson(
    store: Store, staff_code: str, display_name: str, *, service_code: str = "seed"
) -> Staff:
    """A salesperson on this store's staff list, created once (seed and test data).

    Goes through the same staff service People and access uses, as a service
    principal, so the person, their staff record and their assignment here are
    written exactly as a real one would be. Idempotent on the staff code.
    """
    from accounts.goods_admin_services import StaffFields, create_staff
    from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command

    existing = (
        Staff.objects.select_related("human")
        .filter(tenant_id=store.tenant_id, human__staff_code__iexact=staff_code)
        .first()
    )
    if existing is not None:
        return existing
    now = timezone.now()
    fields = StaffFields(
        present=frozenset(
            {"staff_code", "display_name", "salesperson", "site_id", "effective_from"}
        ),
        staff_code=staff_code,
        display_name=display_name,
        salesperson=True,
        site_id=store.pk,
        effective_from=now,
    )

    def handler(run: CommandRun) -> CommandResult:
        staff = create_staff(run, fields=fields)
        return CommandResult(resource_type="staff", resource_id=str(staff.pk))

    result = execute_command(
        Principal(tenant_id=store.tenant_id, service_code=service_code),
        CommandSpec(
            action="staff.create",
            command_id=uuid.uuid4(),
            business_input=fields.as_json(),
            site_id=store.pk,
        ),
        handler,
    )
    return Staff.objects.select_related("human").get(pk=str(result.resource_id))


def rule_outcomes(rows: Sequence[SalespersonMatch]) -> dict[int, Any]:
    """What the matching rule says about each of these old rows, today.

    Read live rather than stored, so the reason Admin sees for an unmatched row
    is always the current one and saving it is never a write of its own.
    """
    from sell.services.salesperson_move import OldSeller, StaffAtStore, match_old_sellers

    placed = people_placed_at(sorted({row.store_id for row in rows}))
    staff = [
        StaffAtStore(
            staff_id=person.pk,
            store_id=store_id,
            staff_code=person.human.staff_code,
            display_name=person.human.display_name,
        )
        for store_id, people in placed.items()
        for person in people
    ]
    return match_old_sellers(
        [OldSeller(row.pk, row.store_id, row.code, row.name) for row in rows], staff
    )


def rematch_unmatched(tenant_id: uuid.UUID, *, service_code: str = "salesperson-move") -> int:
    """Run the matching rule again over the rows still unmatched, and audit each match.

    For after the move: once the missing staff records exist (created in People
    and access), the same rule that ran in the migration places the rows it now
    can. Each match is its own command with the values before and after. A row
    the rule still cannot place waits for Admin. Returns how many rows were
    matched.
    """
    from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command

    rows = list(
        SalespersonMatch.objects.select_related("store").filter(
            staff__isnull=True, store__tenant_id=tenant_id
        )
    )
    outcomes = rule_outcomes(rows)
    matched = 0
    for row in rows:
        outcome = outcomes[row.pk]
        if outcome.staff_id is None:
            continue

        def handler(
            run: CommandRun, row: SalespersonMatch = row, found: Any = outcome
        ) -> CommandResult:
            locked = SalespersonMatch.objects.select_for_update().get(pk=row.pk)
            if locked.staff_id is not None:
                return CommandResult(resource_type="salesperson_match", resource_id=str(row.pk))
            person = Staff.objects.select_related("human").get(pk=found.staff_id)
            run.audit_before = {
                "store": row.store.code,
                "code": locked.code,
                "name": locked.name,
                "staff_id": None,
                "rule": "",
                "revision": locked.revision,
            }
            locked.staff = person
            locked.rule = found.rule
            locked.matched_at = run.now
            locked.revision += 1
            locked.save(update_fields=["staff", "rule", "matched_at", "revision"])
            run.audit_after = {
                "store": row.store.code,
                "code": locked.code,
                "name": locked.name,
                "staff_id": str(person.pk),
                "staff_code": person.human.staff_code,
                "rule": found.rule,
                "revision": locked.revision,
            }
            return CommandResult(
                resource_type="salesperson_match",
                resource_id=str(row.pk),
                revision=locked.revision,
            )

        execute_command(
            Principal(tenant_id=tenant_id, service_code=service_code),
            CommandSpec(
                action="sell.salesperson_match.rule",
                command_id=uuid.uuid4(),
                business_input={"id": row.pk, "staff_id": str(outcome.staff_id)},
                resource_ids=[str(row.pk)],
                subject_key=f"salesperson_match:{row.pk}",
                site_id=row.store_id,
            ),
            handler,
        )
        matched += 1
    return matched
