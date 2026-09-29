"""Each offer's tax settings (store operations PRD §16 and §6; ticket 11).

"Each offer also carries the tax settings from section 6: whether a bank instant
discount reduces value (default No), and the allocation method for buy 2 get 1
(default by MRP)." Both live in the offer's own ``reward_config`` - the smallest
place the offer model has for them, with no migration - and a live offer is
frozen, so a bill is always read against the settings it was priced under:

* ``allocation`` on a buy-X-get-Y (``item_free``) offer: ``by_mrp`` (default)
  or ``free_piece`` (the free piece carries the whole discount, as before);
* ``reduces_value`` on a bank-layer offer: ``false`` (default) - the discount is
  a "bank offer" payment and the taxable value stays - or ``true``.

They change a bill only where the store's ``gst-after-discount`` switch is on.
Each save that sets or changes them leaves an audit record, before and after.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from core.tenancy import current_tenant_id
from masters.models import Store
from offers.models import Offer
from offers.resolution import ALLOCATIONS, REDUCES_VALUE, allocation_of, reduces_value

ALLOCATION = "allocation"
AUDIT_ACTION = "offers.offer.tax_settings"


def check(layer: str, reward: str, reward_config: dict[str, Any]) -> str | None:
    """Why these settings are wrong for this offer, or None."""
    if ALLOCATION in reward_config:
        if reward != Offer.Reward.ITEM_FREE:
            return "'allocation' is only for a buy-X-get-Y offer that gives a piece free."
        if reward_config[ALLOCATION] not in ALLOCATIONS:
            return "'allocation' is 'by_mrp' (spread the price by MRP) or 'free_piece'."
    if REDUCES_VALUE in reward_config:
        if layer != Offer.Layer.BANK:
            return "'reduces_value' is only for a bank offer."
        if not isinstance(reward_config[REDUCES_VALUE], bool):
            return "'reduces_value' is true or false."
    return None


def settings_of(offer: Offer | None) -> dict[str, Any]:
    """The offer's tax settings as they apply, defaults filled in; {} for none."""
    if offer is None:
        return {}
    rule = offer.as_rule()
    out: dict[str, Any] = {}
    if offer.reward_type == Offer.Reward.ITEM_FREE:
        out[ALLOCATION] = allocation_of(rule)
    if offer.layer == Offer.Layer.BANK:
        out[REDUCES_VALUE] = reduces_value(rule)
    return out


def audit(offer: Offer, before: dict[str, Any], actor: Any) -> None:
    """One audit record when a save sets or changes the offer's tax settings."""
    after = settings_of(offer)
    if not after or after == before:
        return
    tenant_id = getattr(actor, "tenant_id", None) or current_tenant_id() or _tenant_of(offer)
    if tenant_id is None:
        raise ValueError(f"offer {offer.pk}: no tenant to audit its tax settings under")
    human_id = getattr(actor, "human_id", None)
    principal = (
        Principal(tenant_id=tenant_id, human_id=human_id, user_id=getattr(actor, "pk", None))
        if human_id is not None
        else Principal(
            tenant_id=tenant_id, service_code="offer-author", user_id=getattr(actor, "pk", None)
        )
    )
    subject = f"offer:{offer.pk}"

    def handler(run: CommandRun) -> CommandResult:
        run.audit_subject_key = subject
        run.audit_before = {"offer": offer.name, **before}
        run.audit_after = {"offer": offer.name, **after}
        return CommandResult(resource_type="offer", resource_id=str(offer.pk), status_code=200)

    execute_command(
        principal,
        CommandSpec(
            action=AUDIT_ACTION,
            command_id=uuid.uuid4(),
            business_input={"offer_id": offer.pk, **after},
            subject_key=subject,
        ),
        handler,
    )


def _tenant_of(offer: Offer) -> Any:
    """The tenant of the stores the offer runs in (one company per deployment)."""
    codes = list((offer.store_scope or {}).get("stores") or [])
    return (
        Store.objects.filter(code__in=codes).values_list("tenant_id", flat=True).first()
        if codes
        else None
    )
