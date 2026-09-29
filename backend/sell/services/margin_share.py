"""Each sale's split between the brand and KDPS (store operations ticket 27, ST-BRD-4).

For a brand sold on SOR or concession, every sold line records how its sale
value divides between the brand and KDPS (overall PRD R-FIN-023):

* **The value split** is what the line sold for after discount, without GST
  (``net_paise - gst_paise``): the GST is the government's, neither party's.
* **KDPS's share** is the brand's margin % of that value, from the brand terms
  in force on the bill date (ticket 23), half up to the paisa (B9). **The
  brand's share** is the rest.
* **Only SOR and concession are split.** An outright or consignment line records
  nothing: that stock is bought, and its margin is not a share of the sale.
* **Unknown stays unknown (D9).** A line whose brand or season is not on the
  books, whose brand has no approved terms for that season (its model is
  unknown), or whose SOR or concession terms leave the margin blank, records the
  reason and no shares, so the statement lists it and never splits it by a guess.

Worked out on the server when the bill arrives - an offline bill when it syncs -
by the terms in force on the bill's date as they stand when it is worked out
(``terms_for(..., at)``, ticket 23's contract; the same rule as ticket 25, B96).
A result, once recorded, is never worked out again.

A piece given back against a split line gives back its part by pieces, counted
over everything given back against that line so far, so a line returned whole
nets to nothing. That follows the sold line whatever the switch says now:
correcting a recorded result is never refused.

Every figure is an **estimate** until OQ-50 settles the profitability formula;
nothing here posts to the books or creates an amount owed (SOR payables wait on
OQ-26, ticket 28).

Only where the store's switch is on (``margin-share``, off by default, B3) does
a sold line get its split. Each bill that records one leaves one audit entry:
nothing before, the lines after.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from masters.models import Store
from masters.store_feature_registry import MARGIN_SHARE
from masters.store_features import is_feature_on
from sell.models import Sale, SaleLine, SaleLineMarginShare
from sell.services.discount_funding import Books, Place, portion
from sell.services.refunds import returned_so_far
from sell.services.split_shares import bill_principal

FEATURE_KEY = MARGIN_SHARE

#: The audit action every bill that records a split is recorded under.
SHARE_ACTION = "sell.margin_share.record"

#: The models whose sales are split between the brand and KDPS.
SPLIT_MODELS = frozenset(choice.value for choice in SaleLineMarginShare.Model)

Unknown = SaleLineMarginShare.Unknown


def margin_share_on(store: Store) -> bool:
    """Whether this store's sold lines get their split."""
    return is_feature_on(store, FEATURE_KEY)


# -- the arithmetic ------------------------------------------------------------------


def kdps_share(value_paise: int, margin_percent: Decimal) -> int:
    """KDPS's margin % of ``value_paise``, half up to the paisa (B9)."""
    exact = Decimal(value_paise) * margin_percent / 100
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def value_of(line: SaleLine) -> int:
    """What the line sold for after discount, without GST."""
    return max(0, int(line.net_paise) - int(line.gst_paise))


@dataclass(frozen=True)
class Shared:
    """One line's split. Both shares None is unknown, with its reason."""

    value_paise: int
    model: str
    margin_percent: Decimal | None
    kdps_paise: int | None
    brand_paise: int | None
    unknown_reason: str = ""
    terms_version_id: uuid.UUID | None = None
    brand_id: int | None = None
    season_id: int | None = None

    def as_audit(self) -> dict[str, Any]:
        return {
            "value_paise": self.value_paise,
            "model": self.model,
            "margin_percent": None if self.margin_percent is None else str(self.margin_percent),
            "kdps_paise": self.kdps_paise,
            "brand_paise": self.brand_paise,
            "unknown_reason": self.unknown_reason,
        }


def share(value_paise: int, place: Place) -> Shared | None:
    """The split of ``value_paise`` under the terms ``place`` found, or None where
    the brand's model is one that is not split (outright, consignment)."""
    brand_id = place.brand.pk if place.brand else None
    season_id = place.season.pk if place.season else None

    def unknown(reason: str, model: str = "", terms_id: uuid.UUID | None = None) -> Shared:
        return Shared(value_paise, model, None, None, None, reason, terms_id, brand_id, season_id)

    if place.brand is None:
        return unknown(Unknown.BRAND.value)
    if place.season is None:
        return unknown(Unknown.SEASON.value)
    terms = place.terms
    if terms is None:
        return unknown(Unknown.MODEL.value)
    if terms.model not in SPLIT_MODELS:
        return None
    percent = terms.margin_percent
    # A margin outside 0-100 cannot be split; it is unknown rather than a bill the
    # database refuses (the bill is already printed).
    if percent is None or not Decimal(0) <= percent <= Decimal(100):
        return unknown(Unknown.MARGIN.value, terms.model, terms.id)
    kdps = kdps_share(value_paise, percent)
    return Shared(
        value_paise=value_paise,
        model=terms.model,
        margin_percent=percent,
        kdps_paise=kdps,
        brand_paise=value_paise - kdps,
        terms_version_id=terms.id,
        brand_id=brand_id,
        season_id=season_id,
    )


def given_back(row: SaleLineMarginShare, sold_qty: int, before_qty: int, qty: int) -> Shared | None:
    """``qty`` pieces' part of a sold line's split, after ``before_qty`` came back.

    Each figure is given back as its portion of the running total less its portion
    of what came before, so once the whole line is back each nets to nought. None
    when nothing is left to give back (more pieces than the line sold).
    """
    after = min(before_qty + qty, sold_qty)
    if after <= before_qty:
        return None

    def back(amount: int) -> int:
        return portion(amount, after, sold_qty) - portion(amount, before_qty, sold_qty)

    if row.kdps_paise is None or row.brand_paise is None:
        kdps, brand = None, None
        value = back(int(row.value_paise))
    else:
        kdps, brand = back(int(row.kdps_paise)), back(int(row.brand_paise))
        value = kdps + brand
    return Shared(
        value_paise=value,
        model=row.model,
        margin_percent=row.margin_percent,
        kdps_paise=kdps,
        brand_paise=brand,
        unknown_reason=row.unknown_reason,
        terms_version_id=row.terms_version_id,
        brand_id=row.brand_ref_id,
        season_id=row.season_ref_id,
    )


# -- the bill ------------------------------------------------------------------------


def _return_legs(lines: list[SaleLine]) -> dict[int, Shared]:
    """Each return leg's part of the split line it gives back, in the bill's order."""
    legs = [line for line in lines if line.direction == SaleLine.Direction.RETURN]
    originals = {line.original_line_id for line in legs if line.original_line_id}
    if not originals:
        return {}
    sold = {
        row.line_id: row
        for row in SaleLineMarginShare.objects.filter(line_id__in=originals).select_related("line")
    }
    before: dict[int, int] = {}
    out: dict[int, Shared] = {}
    for leg in legs:
        row = sold.get(leg.original_line_id or 0)
        if row is None:
            continue
        original = row.line
        if original.pk not in before:
            # What had come back before this bill: this bill's own legs are written
            # already, so they are taken back out and counted in order below.
            mine = sum(int(o.qty) for o in legs if o.original_line_id == original.pk)
            before[original.pk] = returned_so_far(original)[0] - mine
        back = given_back(row, int(original.qty), before[original.pk], int(leg.qty))
        before[original.pk] += int(leg.qty)
        if back is not None:
            out[leg.pk] = back
    return out


def record_margin_share(sale: Sale, store: Store, actor: Any, lines: list[SaleLine]) -> None:
    """Record each sold line's split between the brand and KDPS, and audit it."""
    planned: dict[int, Shared] = {}
    if margin_share_on(store):
        books = Books(store, sale.billed_at)
        for line in lines:
            if line.direction == SaleLine.Direction.SALE:
                shared = share(value_of(line), books.place(line))
                if shared is not None:
                    planned[line.pk] = shared
    planned.update(_return_legs(lines))
    if not planned:
        return
    SaleLineMarginShare.objects.bulk_create(
        [
            SaleLineMarginShare(
                line_id=pk,
                model=shared.model,
                value_paise=shared.value_paise,
                margin_percent=shared.margin_percent,
                kdps_paise=shared.kdps_paise,
                brand_paise=shared.brand_paise,
                unknown_reason=shared.unknown_reason,
                brand_ref_id=shared.brand_id,
                season_ref_id=shared.season_id,
                terms_version_id=shared.terms_version_id,
            )
            for pk, shared in planned.items()
        ]
    )
    by_pk = {line.pk: line for line in lines}
    _audit(sale, store, actor, [(by_pk[pk], shared) for pk, shared in planned.items()])


def _audit(sale: Sale, store: Store, actor: Any, recorded: list[tuple[SaleLine, Shared]]) -> None:
    """One audit record for the split a bill recorded: nothing before, the lines after.

    The command id is derived from the bill's own key, so a replay is the same record.
    """
    subject = f"margin_share:{sale.pk}"
    after = {
        "doc_number": sale.doc_number,
        "lines": [
            {"line_no": line.line_no, "direction": line.direction, **shared.as_audit()}
            for line, shared in sorted(recorded, key=lambda item: item[0].line_no)
        ],
    }

    def handler(run: CommandRun) -> CommandResult:
        run.audit_subject_key = subject
        run.audit_site_id = store.pk
        run.audit_before = {"doc_number": sale.doc_number, "lines": []}
        run.audit_after = after
        return CommandResult(resource_type="sale", resource_id=str(sale.pk), status_code=201)

    execute_command(
        bill_principal(store, actor),
        CommandSpec(
            action=SHARE_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"margin-share:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )
