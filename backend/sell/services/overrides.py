"""The manager's own PIN on every counter override (store operations ticket 06).

A counter override is an exception a cashier cannot make alone: today exactly
one exists, taking a piece back after the store's return window (`late_return`).
The manager types their own PIN at the till, which checks it on the device
against the hash the dataset sent (`accounts.till_pin`, `till/pin.ts`), so the
check works with the line down. The bill then carries who approved it
(`Sale.override_by`, `override_kind`, `override_at`), who billed it
(`Sale.created_by`) and on which till (`Sale.till_number`).

Where the store's `manager-pin-overrides` switch is on, three things are added,
and this module owns the two that happen after the bill is written:

* the approver must be somebody the till could have held a PIN for - see
  `resolve.manager_for_override(own_pin_rules=True)`, applied in the pipeline;
* the approver must be a different person from the cashier (overall PRD
  §10.2). The till refuses that before the bill is made. A bill that arrives
  self-approved anyway (an old till, or the switch turned on while it sat in an
  offline queue) is already printed, so it is kept and flagged rather than
  refused;
* each override is written to the audit log with the approver, cashier, till,
  time and what was approved, by person id and never by name (the audit page
  shows names only to readers allowed to see them, baseline B23).

Off, nothing here runs and the till and server behave exactly as before (B3).
"""

from __future__ import annotations

import uuid
from typing import Any

from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from masters.models import Store
from masters.store_feature_registry import MANAGER_PIN_OVERRIDES
from masters.store_features import is_feature_on
from sell.models import ContinuityFlag, Sale, SaleLine

#: The audit action every recorded override is filed under.
OVERRIDE_ACTION = "sell.override.approve"


def pin_rules_on(store: Store) -> bool:
    """Whether ticket 06's rules apply at this store."""
    return is_feature_on(store, MANAGER_PIN_OVERRIDES)


def override_facts(sale: Sale) -> dict[str, Any]:
    """What an override on this bill was, as the audit log and reports read it.

    Everything is derived from the written bill, not from what the till said it
    was asking: the original bill is the one the pipeline resolved, and the value
    is what the return lines actually gave back.
    """
    returned = sum(
        line.net_paise for line in sale.lines.filter(direction=SaleLine.Direction.RETURN)
    )
    return {
        "kind": sale.override_kind,
        "doc_number": sale.doc_number,
        "original_bill": sale.exchange_of.doc_number if sale.exchange_of else None,
        "value_paise": abs(int(returned)),
        "approver_user_id": sale.override_by_id,
        "cashier_user_id": sale.created_by_id,
        "till_number": sale.till_number,
        "approved_at": sale.override_at.isoformat() if sale.override_at else None,
        "self_approved": sale.override_by_id == sale.created_by_id,
    }


def record_override(sale: Sale, store: Store, actor: Any, *, rules_on: bool) -> list[str]:
    """Flag a self-approval and audit the override, where the switch is on.

    Returns the flags raised, as the pipeline collects them.
    """
    if not rules_on or sale.override_by_id is None:
        return []
    facts = override_facts(sale)
    flags: list[str] = []
    if facts["self_approved"]:
        ContinuityFlag.objects.create(
            kind=ContinuityFlag.Kind.OVERRIDE_SELF_APPROVED,
            store=store,
            sale=sale,
            details={
                "override_kind": facts["kind"],
                "user_id": sale.override_by_id,
            },
        )
        flags.append(ContinuityFlag.Kind.OVERRIDE_SELF_APPROVED)
    _audit(sale, store, actor, facts)
    return flags


def can_sign(actor: Any) -> bool:
    """Whether this login is a person who can sign an audit record.

    Every login that can sign in is (goods-v1 sessions refuse anything else), so
    the pipeline refuses an override it could not record rather than keep one
    with no record - the same rule as `goods_sale._principal`.
    """
    return (
        getattr(actor, "human_id", None) is not None
        and getattr(actor, "tenant_id", None) is not None
    )


def _audit(sale: Sale, store: Store, actor: Any, facts: dict[str, Any]) -> None:
    principal = Principal(tenant_id=actor.tenant_id, human_id=actor.human_id, user_id=actor.pk)

    def handler(run: CommandRun) -> CommandResult:
        run.audit_subject_key = f"sale_override:{sale.pk}"
        run.audit_site_id = store.pk
        run.audit_before = {"kind": facts["kind"], "approved": False}
        run.audit_after = {**facts, "approved": True}
        return CommandResult(resource_type="sale", resource_id=str(sale.pk), status_code=201)

    execute_command(
        principal,
        CommandSpec(
            action=OVERRIDE_ACTION,
            # Derived from the bill's own key: a replay is the same record.
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"sale-override:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=f"sale_override:{sale.pk}",
        ),
        handler,
    )
