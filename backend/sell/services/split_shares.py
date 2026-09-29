"""A sale line shared between two salespeople (store operations ticket 08, ST-POS-2).

The till can split a sold line between two people by whole percentages
(baseline B14): two **different** people, each from 1% to 99%, adding to 100%.
The line's value (what the customer paid for it, GST inclusive) is split to the
paisa. The second person takes their percentage rounded down, and the first
person takes the rest - so the spare paisa, if any, goes to the first
salesperson and the two shares always add up to the line exactly.

A piece given back against a split line takes each person's share down in the
same proportion. Each return is split the same way, but counted over everything
given back against that line so far: the second person's part is their share of
the running total less their share of what came before. The first return of a
line is therefore split exactly as a sale is, and once the whole line has come
back each person has given back exactly what they were credited - partial
returns never leave a stray paisa on anybody.

The shares are stored as rows (`SaleLineShare`), one per person, on the sold
line and on every return leg against it, so reports and incentives read them
directly rather than working them out again. A reader skips the rows of a
cancelled bill, exactly as it skips the bill: a cancelled return's shares stay
stored, and the next return against the line is counted without them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from masters.models import Store
from masters.store_feature_registry import SPLIT_SALE
from masters.store_features import is_feature_on

#: Each person's share, in whole percent (B14).
MIN_PERCENT = 1
MAX_PERCENT = 99
#: How many people a line can be split between.
PEOPLE = 2

#: The audit action every bill carrying shares is recorded under.
SHARES_ACTION = "sell.sale_line.shares"


@dataclass(frozen=True)
class PlannedShare:
    """One person's part of a line, ready to be written."""

    position: int
    staff_id: uuid.UUID
    code: str
    name: str
    percent: int
    value_paise: int


def split_on(store: Store) -> bool:
    """Whether this store's till may split a line."""
    return is_feature_on(store, SPLIT_SALE)


def split_problem(first: Any, shares: list[dict[str, Any]]) -> str | None:
    """Why these shares cannot stand, in words a cashier reads, or None.

    `first` is the line's own salesperson; the first share must name them.
    """
    if len(shares) != PEOPLE:
        return "A line is split between exactly two salespeople."
    people = [share.get("salesperson") for share in shares]
    if any(person is None for person in people):
        return "Both salespeople must be named."
    if str(people[0]) != str(first):
        return "The first share must be the line's own salesperson."
    if str(people[0]) == str(people[1]):
        return "A line is split between two different salespeople."
    percents = [share.get("percent") for share in shares]
    for percent in percents:
        if not isinstance(percent, int) or isinstance(percent, bool):
            return "Each share is a whole percentage."
        if percent < MIN_PERCENT or percent > MAX_PERCENT:
            return f"Each share is from {MIN_PERCENT}% to {MAX_PERCENT}%."
    if sum(percents) != 100:  # type: ignore[arg-type]
        return "The two shares must add up to 100%."
    return None


def split_value(value_paise: int, percents: list[int]) -> list[int]:
    """A sold line's value split by these percentages; the spare paisa to the first."""
    return split_returned(value_paise, 0, percents)


def split_returned(value_paise: int, before_paise: int, percents: list[int]) -> list[int]:
    """A return's value split by these percentages, given what came back before it.

    The second person gives back their share of the running total less their
    share of what came before, rounded down; the first gives back the rest. With
    nothing before, this is exactly `split_value`.
    """
    second_percent = percents[1]
    after = before_paise + value_paise
    second = (after * second_percent) // 100 - (before_paise * second_percent) // 100
    return [value_paise - second, second]


def bill_principal(store: Store, actor: Any) -> Principal:
    """Who an audit record written with a bill names: the person who synced it.

    A login that is not a person cannot sign an audit record. Ticket 06 refuses
    such an override before anything is written; what a bill records alongside it
    cannot be refused that way, because the bill is already printed and refusing
    it would lose it. So it is recorded as the till's sync, still naming the login.
    """
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    user_id = getattr(actor, "pk", None)
    if human_id is not None and tenant_id is not None:
        return Principal(tenant_id=tenant_id, human_id=human_id, user_id=user_id)
    return Principal(tenant_id=store.tenant_id, service_code="till-sync", user_id=user_id)


def audit_shares(sale: Any, store: Store, actor: Any, lines: list[dict[str, Any]]) -> None:
    """One audit record for the shares a bill wrote: none before, these after.

    Recorded by staff id, percentage and paise - never by name (baseline B23).
    The command id is derived from the bill's own key, so a replay is the same
    record. A login that is not a person is recorded as the till's sync.
    """
    principal = bill_principal(store, actor)
    subject = f"sale_shares:{sale.pk}"

    def handler(run: CommandRun) -> CommandResult:
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {"doc_number": sale.doc_number, "lines": []}
        run.audit_after = {"doc_number": sale.doc_number, "lines": lines}
        return CommandResult(resource_type="sale", resource_id=str(sale.pk), status_code=201)

    execute_command(
        principal,
        CommandSpec(
            action=SHARES_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"sale-shares:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )
