"""Gift stock: pieces given away free, tagged when they leave stock (ticket 14).

Store operations PRD ST-CMP-7 (baseline, CA to confirm). Stock given away with
no payment, as a different item - a gift with purchase from the offers engine is
the example - is tagged as a gift when its bill is accepted, so Accounts can
reverse the input tax credit on it each month (CGST Act s.17(5)(h)).

**What is a gift piece** (baseline B93): a sold line on an accepted bill whose
value is nought - the customer paid nothing for it - and whose rule is not a buy
X get Y. A buy 2 get 1 piece is part of the sale and is never tagged, however
the price was spread. A gift offer the bill earned for the line's barcode is
named on the tag; a piece given free with no gift offer is tagged all the same,
because it is stock given away too and leaving it out would reverse less credit
(the conservative side, §6 principle 4). A gift sold at a token price was paid
for, so it is a sale, not gift stock.

**When** (B94): at acceptance, only where the store's switch is on then. A bill
queued offline is tagged when it reaches head office; a bill accepted earlier
is never tagged afterwards.

**The credit to reverse** (B11, B95): for each receipt layer the pieces came
from, the pieces' receipt cost times the input tax rate on that layer's
price-ticket line, half up to the paisa. It is unknown - never guessed (overall
PRD §8.4) - when no layer is recorded, when a layer carries no input tax rate,
or when a layer was received under another GSTIN (the input tax of the transfer
into this one is not held). The figures are frozen on the tag.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from core.commands import CommandResult, CommandRun, CommandSpec, Principal, execute_command
from masters.models import Store
from masters.store_feature_registry import GIFT_STOCK_ITC
from masters.store_features import is_feature_on
from masters.tax_settings import DEFAULT_GIFT, LEGACY_VERSION, saved_versions
from offers.models import Offer
from offers.resolution import Entitlement
from sell.models import GiftPiece, Sale, SaleLine
from sell.services.goods_sale import Allocation
from stockledger.goods_models import Origin

#: The audit action every bill carrying gift pieces is recorded under.
GIFT_ACTION = "sell.gift_piece.tag"


@dataclass(frozen=True)
class Candidate:
    """A sold line the customer paid nothing for, and the rules that priced it."""

    row: SaleLine
    goods: Sequence[Allocation]
    #: The rule the server's own resolution chose, and the rule the till cited
    #: where the server re-ran it and found it applies to this line.
    offer_ids: tuple[Any, ...]


@dataclass(frozen=True)
class LayerTax:
    """What a receipt layer says about input tax: its rate and its GSTIN."""

    input_tax_pct: Decimal | None
    gstin: str


@dataclass(frozen=True)
class Valued:
    cost_paise: int | None
    itc_paise: int | None
    missing: str
    layers: list[dict[str, Any]]


def gifts_on(store: Store) -> bool:
    """Whether pieces given free at this store are tagged (gated: CA sign-off)."""
    return is_feature_on(store, GIFT_STOCK_ITC)


def is_part_of_sale(offers: Iterable[Offer | None]) -> bool:
    """A buy X get Y piece: its price went to the other pieces, it was not given."""
    return any(
        offer is not None
        and (
            offer.reward_type == Offer.Reward.ITEM_FREE or offer.trigger_type == Offer.Trigger.GROUP
        )
        for offer in offers
    )


def itc_on(cost_paise: int, pct: Decimal) -> int:
    """The input tax on ``cost_paise`` of receipt cost at ``pct`` %, half up (B9)."""
    exact = Decimal(cost_paise) * pct / 100
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _pct(raw: Any) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        pct = Decimal(str(raw))
    except InvalidOperation:
        return None
    return pct if pct.is_finite() and pct >= 0 else None


def layer_taxes(origin_ids: Iterable[str]) -> dict[str, LayerTax]:
    """Each receipt layer's input tax rate (its price-ticket line's) and GSTIN."""
    out: dict[str, LayerTax] = {}
    for origin in Origin.objects.filter(pk__in=set(origin_ids)).select_related(
        "official_line", "site__gstin"
    ):
        payload = origin.official_line.payload if origin.official_line is not None else {}
        calculated = (payload or {}).get("calculated") or {}
        out[str(origin.pk)] = LayerTax(
            input_tax_pct=_pct(calculated.get("input_tax_pct")),
            gstin=(origin.site.gstin.gstin or "").strip().upper(),
        )
    return out


def value(
    goods: Sequence[Allocation],
    taxes: Mapping[str, LayerTax],
    gstin: str,
    fallback_cost_paise: int | None,
) -> Valued:
    """The cost and the credit to reverse on these pieces, layer by layer (B95).

    With no layer recorded the cost is the line's cost of record, if it has one,
    and the credit is unknown. One layer that cannot be valued makes the whole
    credit unknown: a partial figure would read as the whole.
    """
    if not goods:
        return Valued(fallback_cost_paise, None, GiftPiece.Missing.NO_LAYER, [])
    # One figure per receipt layer: two lot portions of the same origin are one
    # layer, rounded once (B95).
    by_layer: dict[str, list[Allocation]] = {}
    for portion in goods:
        by_layer.setdefault(str(portion.origin_id), []).append(portion)
    rows: list[dict[str, Any]] = []
    missing = ""
    for origin_id, portions in by_layer.items():
        tax = taxes.get(origin_id)
        pct = tax.input_tax_pct if tax else None
        cost = sum(portion.cost_paise for portion in portions)
        itc: int | None = None
        if pct is None:
            missing = missing or GiftPiece.Missing.NO_RATE
        elif tax is not None and tax.gstin != gstin:
            missing = missing or GiftPiece.Missing.OTHER_GSTIN
        else:
            itc = itc_on(cost, pct)
        rows.append(
            {
                "origin_id": origin_id,
                "qty": sum(portion.qty for portion in portions),
                "cost_paise": cost,
                "input_tax_pct": None if pct is None else str(pct),
                "itc_paise": itc,
            }
        )
    cost = sum(int(row["cost_paise"]) for row in rows)
    total = None if missing else sum(int(row["itc_paise"]) for row in rows)
    return Valued(cost, total, missing, rows)


def _offer_id(raw: Any) -> int | None:
    """An offer id as the till or the resolution sent it; anything else is no id."""
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def gift_with_purchase_is_gift(sale: Sale, store: Store) -> bool:
    """The bill's own tax settings version says whether a gift with purchase is
    gift stock (ticket 14, §6 principles 1-2). Version 1 has the baseline: yes."""
    if sale.tax_setting_version <= LEGACY_VERSION:
        return DEFAULT_GIFT
    found = next(
        (v for v in saved_versions(store.tenant_id) if v.version == sale.tax_setting_version),
        None,
    )
    return found.gift_with_purchase_is_gift if found is not None else DEFAULT_GIFT


def _gstin(store: Store) -> str:
    registration = store.gstin if store.gstin_id else None
    return (registration.gstin if registration else "").strip().upper()


def tag_gifts(
    sale: Sale,
    store: Store,
    actor: Any,
    candidates: Sequence[Candidate],
    entitlements: Sequence[Entitlement],
) -> list[GiftPiece]:
    """Tag the pieces this bill gave away, and audit them. Nothing where off."""
    if not candidates or not gifts_on(store):
        return []
    offer_ids = {oid for c in candidates for oid in map(_offer_id, c.offer_ids) if oid}
    offers = Offer.objects.in_bulk(offer_ids) if offer_ids else {}
    given = [
        c
        for c in candidates
        if not is_part_of_sale(offers.get(oid) for oid in map(_offer_id, c.offer_ids) if oid)
    ]
    if not given:
        return []
    gifts_count = gift_with_purchase_is_gift(sale, store)
    gstin = _gstin(store)
    taxes = layer_taxes(str(p.origin_id) for c in given for p in c.goods)
    earned = list(entitlements)
    day = timezone.localdate(sale.billed_at)
    tags: list[GiftPiece] = []
    for candidate in given:
        row = candidate.row
        gift = next((e for e in earned if e.barcode.strip() == row.barcode.strip()), None)
        if gift is not None:
            earned.remove(gift)  # one earned gift names one line
            if not gifts_count:
                continue  # this bill's tax version: a gift with purchase is part of the sale
        known_cost = int(row.unit_cost_paise) * row.qty if row.unit_cost_paise else None
        valued = value(candidate.goods, taxes, gstin, known_cost)
        tags.append(
            GiftPiece.objects.create(
                sale=sale,
                line=row,
                store=store,
                day=day,
                gstin=gstin,
                qty=row.qty,
                source=GiftPiece.Source.GIFT_OFFER if gift else GiftPiece.Source.FREE,
                offer_id=gift.offer_id if gift else None,
                offer_name=gift.offer_name if gift else "",
                cost_paise=valued.cost_paise,
                itc_paise=valued.itc_paise,
                missing=valued.missing,
                layers=valued.layers,
                tax_setting_version=sale.tax_setting_version,
            )
        )
    if tags:
        _audit(sale, store, actor, tags)
    return tags


def _audit(sale: Sale, store: Store, actor: Any, tags: list[GiftPiece]) -> None:
    """One audit record for the pieces a bill gave away: none before, these after.

    No cost or credit in it: the audit log is read more widely than cost is.
    The command id comes from the bill's own key, so a replay is the same record.
    """
    human_id = getattr(actor, "human_id", None)
    tenant_id = getattr(actor, "tenant_id", None)
    user_id = getattr(actor, "pk", None)
    # The bill is printed; a login that is not a person is recorded as the
    # till's sync, still naming the login, exactly as split shares are.
    principal = (
        Principal(tenant_id=tenant_id, human_id=human_id, user_id=user_id)
        if human_id is not None and tenant_id is not None
        else Principal(tenant_id=store.tenant_id, service_code="till-sync", user_id=user_id)
    )
    subject = f"gift_pieces:{sale.pk}"
    after = {
        "doc_number": sale.doc_number,
        "pieces": [
            {
                "line_no": tag.line.line_no,
                "barcode": tag.line.barcode,
                "qty": tag.qty,
                "source": tag.source,
                "offer_id": tag.offer_id,
                "gstin": tag.gstin,
                "credit_known": tag.missing == "",
                "tax_setting_version": tag.tax_setting_version,
            }
            for tag in tags
        ],
    }

    def handler(run: CommandRun) -> CommandResult:
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {"doc_number": sale.doc_number, "pieces": []}
        run.audit_after = after
        return CommandResult(resource_type="sale", resource_id=str(sale.pk), status_code=201)

    execute_command(
        principal,
        CommandSpec(
            action=GIFT_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"gift-pieces:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )
