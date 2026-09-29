"""Who funds each discount: the brand and KDPS (store operations ticket 25, ST-OFR-2).

Every discounted line records the brand's share and KDPS's share of its
discount (overall PRD R-PRC-001), part by part:

* **Where the discount came from.** The bill's own offer record says it: the
  winning offer and any add-on stacked on it, each with what it saved. What no
  offer gave (a manual discount) is its own part. A bill whose offers add up to
  more than the line's discount cannot be read, so the whole line is unknown.
  The record comes from the till, the party the discount cap exists to check, so
  it is never trusted for more than the server's own reading of the offers
  (``credits``, from the accept pipeline): a till claiming more offer saving than
  that leaves the whole line unknown, never charged to a brand.
* **Who pays each part.** The offer's funder, frozen on the row. A KDPS offer and
  the part no offer gave are KDPS's alone. A brand-funded offer is shared by the
  brand terms in force on the bill date (ticket 23): the brand's share is its
  discount-funding % of the part, half up (B9), and KDPS pays the rest.
* **Unknown stays unknown (D9).** A brand-funded part is unknown when the line's
  brand or season is not on the books, the brand has no approved terms for that
  season (its model is unknown), or its terms leave the share unknown. The
  reason is kept, and the shares are left empty, never guessed.

Worked out on the server when the bill arrives - an offline bill when it syncs -
by the terms in force on the bill's date as they stand when it is worked out
(``terms_for(..., at)``, ticket 23's contract). Terms never apply from a past
date (B81), so a later change can only reach a bill it could have reached at
the time; and a result, once recorded, is never worked out again.

A piece given back against a funded line gives back each part in proportion,
counted over everything given back against that line so far, so a line
returned whole nets to nothing. That follows the sold line whatever the switch
says now: correcting a recorded split is never refused.

Only where the store's switch is on (``discount-funding-split``, off by
default, B3) does a sold line get its split. Each bill that records one leaves
one audit entry: nothing before, the parts after.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.utils import timezone

from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from masters.brand_terms import Terms, terms_for
from masters.models import Brand, Season, Store
from masters.store_feature_registry import DISCOUNT_FUNDING_SPLIT
from masters.store_features import is_feature_on
from offers.models import Offer
from offers.resolution import normalise
from sell.models import Sale, SaleLine, SaleLineFunding
from sell.services.refunds import returned_so_far
from sell.services.split_shares import bill_principal

FEATURE_KEY = DISCOUNT_FUNDING_SPLIT

#: The audit action every bill that records a split is recorded under.
FUNDING_ACTION = "sell.discount_funding.record"

BRAND = SaleLineFunding.Funder.BRAND.value
KDPS = SaleLineFunding.Funder.KDPS.value
Unknown = SaleLineFunding.Unknown


def funding_on(store: Store) -> bool:
    """Whether this store's discounted lines get their split."""
    return is_feature_on(store, FEATURE_KEY)


# -- the arithmetic ------------------------------------------------------------------


def _half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def brand_share(amount_paise: int, percent: Decimal) -> int:
    """The brand's share of ``amount_paise`` at ``percent``, half up (B9)."""
    return _half_up(Decimal(amount_paise) * percent / 100)


def portion(amount_paise: int, qty: int, of_qty: int) -> int:
    """``amount_paise`` for ``qty`` pieces of ``of_qty``, half up."""
    return _half_up(Decimal(amount_paise) * qty / of_qty)


@dataclass(frozen=True)
class Part:
    """One part of a line's discount: an offer's saving, or the rest (``offer_id`` None)."""

    offer_id: int | None
    amount_paise: int
    from_offer: bool = True


def _whole(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parts_of(disc_paise: int, offer_id: int | None, evidence: Any) -> list[Part] | None:
    """The line's discount by where it came from, or None when the bill's own offer
    record cannot be read against it.

    The evidence's ``saved_paise`` is the whole saving the offers gave; each
    add-on in ``stack`` names its own, and the winner's is what is left. A till
    that named an offer but kept no record of it gave that offer the whole
    discount. What the offers did not give is the rest, KDPS's.
    """
    if disc_paise <= 0:
        return []
    record = evidence if isinstance(evidence, dict) else {}
    claimed = _whole(record.get("saved_paise")) or 0
    winner_id = _whole(record.get("offer_id"))
    stack: list[Part] = []
    for entry in record.get("stack") or []:
        amount = _whole(entry.get("saved_paise")) if isinstance(entry, dict) else None
        if amount is None or amount < 0:
            return None
        if amount:
            stacked_id = _whole(entry.get("offer_id"))
            if stacked_id is None:
                return None
            stack.append(Part(stacked_id, amount))
    if not claimed and not stack and offer_id:
        claimed, winner_id = disc_paise, offer_id
    winner = claimed - sum(part.amount_paise for part in stack)
    rest = disc_paise - claimed
    if claimed < 0 or winner < 0 or rest < 0 or (winner and winner_id is None):
        return None
    parts = [Part(winner_id, winner)] if winner else []
    parts += stack
    if rest:
        parts.append(Part(None, rest, from_offer=False))
    return parts


# -- who pays each part --------------------------------------------------------------


@dataclass(frozen=True)
class Funded:
    """One part, with who pays it. Both shares None is unknown, with its reason."""

    offer_id: int | None
    offer_name: str
    funder: str
    discount_paise: int
    brand_paise: int | None
    kdps_paise: int | None
    unknown_reason: str = ""
    terms_version_id: uuid.UUID | None = None
    funding_percent: Decimal | None = None
    #: The brand and season the terms were read for, where the books know them.
    brand_id: int | None = None
    season_id: int | None = None

    def as_audit(self, part: int) -> dict[str, Any]:
        return {
            "part": part,
            "offer_id": self.offer_id,
            "funder": self.funder,
            "discount_paise": self.discount_paise,
            "brand_paise": self.brand_paise,
            "kdps_paise": self.kdps_paise,
            "unknown_reason": self.unknown_reason,
        }


def _known(offer: Offer | None, funder: str, amount: int, brand: int) -> Funded:
    return Funded(
        offer_id=offer.pk if offer else None,
        offer_name=offer.name if offer else "",
        funder=funder,
        discount_paise=amount,
        brand_paise=brand,
        kdps_paise=amount - brand,
    )


def _unknown(offer: Offer | None, funder: str, amount: int, reason: str) -> Funded:
    return Funded(
        offer_id=offer.pk if offer else None,
        offer_name=offer.name if offer else "",
        funder=funder,
        discount_paise=amount,
        brand_paise=None,
        kdps_paise=None,
        unknown_reason=reason,
    )


@dataclass(frozen=True)
class Place:
    """The brand and season a line's terms are read for, and the terms in force."""

    brand: Brand | None
    season: Season | None
    terms: Terms | None


def _who_pays(part: Part, offer: Offer | None, place: Place) -> Funded:
    amount = part.amount_paise
    if not part.from_offer:
        return _known(None, KDPS, amount, 0)
    if offer is None:
        # Kept by the id the bill named, which no offer on the books answers to.
        name = f"Offer {part.offer_id} (not on the books)"
        return Funded(None, name, "", amount, None, None, Unknown.OFFER.value)
    if offer.funder != BRAND:
        return _known(offer, KDPS, amount, 0)
    if place.brand is None:
        return _unknown(offer, BRAND, amount, Unknown.BRAND.value)
    if place.season is None:
        return _unknown(offer, BRAND, amount, Unknown.SEASON.value)
    terms = place.terms
    if terms is None:
        return _unknown(offer, BRAND, amount, Unknown.MODEL.value)
    percent = terms.discount_funding_percent
    # A share outside 0-100 cannot be split; it is unknown rather than a bill the
    # database refuses (the bill is already printed).
    if percent is None or not Decimal(0) <= percent <= Decimal(100):
        return replace(
            _unknown(offer, BRAND, amount, Unknown.SHARE.value), terms_version_id=terms.id
        )
    return replace(
        _known(offer, BRAND, amount, brand_share(amount, percent)),
        terms_version_id=terms.id,
        funding_percent=percent,
    )


def fund(part: Part, offer: Offer | None, place: Place) -> Funded:
    """Who pays ``part``, with the brand and season its terms were read for."""
    return replace(
        _who_pays(part, offer, place),
        brand_id=place.brand.pk if place.brand else None,
        season_id=place.season.pk if place.season else None,
    )


def given_back(row: SaleLineFunding, sold_qty: int, before_qty: int, qty: int) -> Funded | None:
    """``qty`` pieces' part of a sold line's funded part, after ``before_qty`` came back.

    Each share is given back as its portion of the running total less its portion
    of what came before, so once the whole line is back each share nets to nought.
    None when this return gives back nothing of the part.
    """
    after = before_qty + qty

    def back(amount: int) -> int:
        return portion(amount, after, sold_qty) - portion(amount, before_qty, sold_qty)

    if row.brand_paise is None or row.kdps_paise is None:
        brand, kdps = None, None
        amount = back(int(row.discount_paise))
    else:
        brand, kdps = back(int(row.brand_paise)), back(int(row.kdps_paise))
        amount = brand + kdps
    if amount <= 0:
        return None
    return Funded(
        offer_id=row.offer_id,
        offer_name=row.offer_name,
        funder=row.funder,
        discount_paise=amount,
        brand_paise=brand,
        kdps_paise=kdps,
        unknown_reason=row.unknown_reason,
        terms_version_id=row.terms_version_id,
        funding_percent=row.funding_percent,
        brand_id=row.brand_ref_id,
        season_id=row.season_ref_id,
    )


# -- the bill ------------------------------------------------------------------------


class Books:
    """What a bill's lines are read against: brands, seasons, offers and terms, once each.

    Ticket 27's margin share reads its lines' terms through the same books, so a
    line's brand, season and terms are found the same way for both."""

    def __init__(self, store: Store, billed_at: datetime) -> None:
        self.tenant_id = store.tenant_id
        self.day: date = timezone.localdate(billed_at)
        # The terms as they stand now, when the split is worked out (ticket 23).
        self.at = timezone.now()
        self._brands: dict[str, list[Brand]] | None = None
        self._seasons: dict[str, Season | None] = {}
        self._offers: dict[int, Offer | None] = {}
        self._terms: dict[tuple[int, int], Terms | None] = {}

    def brand(self, name: str) -> Brand | None:
        if self._brands is None:
            self._brands = defaultdict(list)
            for brand in Brand.objects.all():
                self._brands[normalise(brand.name)].append(brand)
        found = self._brands.get(normalise(name), []) if normalise(name) else []
        return found[0] if len(found) == 1 else None

    def season(self, text: str) -> Season | None:
        """The one season a line names, by code or by name (as the counter's own
        piece lookup reads it); none when it names none or two."""
        if text not in self._seasons:
            found = (
                list((Season.objects.filter(code=text) | Season.objects.filter(name=text))[:2])
                if text
                else []
            )
            self._seasons[text] = found[0] if len(found) == 1 else None
        return self._seasons[text]

    def offer(self, offer_id: int | None) -> Offer | None:
        if offer_id is None:
            return None
        if offer_id not in self._offers:
            self._offers[offer_id] = Offer.objects.filter(pk=offer_id).first()
        return self._offers[offer_id]

    def place(self, line: SaleLine) -> Place:
        brand, season = self.brand(line.brand), self.season(line.season)
        if brand is None or season is None:
            return Place(brand, season, None)
        key = (brand.pk, season.pk)
        if key not in self._terms:
            self._terms[key] = terms_for(self.tenant_id, brand.pk, season.pk, self.day, self.at)
        return Place(brand, season, self._terms[key])


def _sold_line(line: SaleLine, books: Books, credit: int | None) -> list[Funded]:
    """``credit`` is the server's own reading of what the offers gave this line, or
    None where there is none to check against."""
    place = books.place(line)
    parts = parts_of(int(line.disc_paise), line.offer_id, line.offer_evidence)
    reason = ""
    if parts is None:
        reason = Unknown.PARTS.value
    elif credit is not None and sum(p.amount_paise for p in parts if p.from_offer) > credit:
        reason = Unknown.UNVERIFIED.value
    if not reason:
        return [fund(part, books.offer(part.offer_id), place) for part in parts or []]
    offer = books.offer(line.offer_id)
    whole = _unknown(offer, offer.funder if offer else "", int(line.disc_paise), reason)
    return [
        replace(
            whole,
            brand_id=place.brand.pk if place.brand else None,
            season_id=place.season.pk if place.season else None,
        )
    ]


def _return_legs(lines: list[SaleLine]) -> dict[int, list[Funded]]:
    """Each return leg's part of the funded line it gives back, in the bill's order."""
    legs = [line for line in lines if line.direction == SaleLine.Direction.RETURN]
    originals = {line.original_line_id for line in legs if line.original_line_id}
    if not originals:
        return {}
    sold = defaultdict(list)
    for row in SaleLineFunding.objects.filter(line_id__in=originals).select_related("line"):
        sold[row.line_id].append(row)
    before: dict[int, int] = {}
    out: dict[int, list[Funded]] = {}
    for leg in legs:
        rows = sold.get(leg.original_line_id or 0)
        if not rows:
            continue
        original = rows[0].line
        if original.pk not in before:
            # What had come back before this bill: this bill's own legs are written
            # already, so they are taken back out and counted in order below.
            mine = sum(int(o.qty) for o in legs if o.original_line_id == original.pk)
            before[original.pk] = returned_so_far(original)[0] - mine
        funded = [
            back
            for row in sorted(rows, key=lambda r: r.part)
            if (back := given_back(row, int(original.qty), before[original.pk], int(leg.qty)))
        ]
        before[original.pk] += int(leg.qty)
        if funded:
            out[leg.pk] = funded
    return out


def record_funding(
    sale: Sale,
    store: Store,
    actor: Any,
    lines: list[SaleLine],
    credits: Mapping[int, int] | None = None,
) -> None:
    """Record who funds each discounted line of this bill, and audit it.

    ``credits`` is the server's own reading of what the offers gave each sold line,
    by line number; a line the till says the offers gave more is left unknown.
    """
    planned: dict[int, list[Funded]] = {}
    if funding_on(store):
        books = Books(store, sale.billed_at)
        for line in lines:
            if line.direction == SaleLine.Direction.SALE and int(line.disc_paise) > 0:
                credit = None if credits is None else credits.get(line.line_no)
                planned[line.pk] = _sold_line(line, books, credit)
    planned.update(_return_legs(lines))
    if not planned:
        return
    SaleLineFunding.objects.bulk_create(
        [
            SaleLineFunding(
                line_id=pk,
                part=number,
                offer_id=funded.offer_id,
                offer_name=funded.offer_name,
                funder=funded.funder,
                discount_paise=funded.discount_paise,
                brand_paise=funded.brand_paise,
                kdps_paise=funded.kdps_paise,
                unknown_reason=funded.unknown_reason,
                brand_ref_id=funded.brand_id,
                season_ref_id=funded.season_id,
                terms_version_id=funded.terms_version_id,
                funding_percent=funded.funding_percent,
            )
            for pk, parts in planned.items()
            for number, funded in enumerate(parts, start=1)
        ]
    )
    by_pk = {line.pk: line for line in lines}
    _audit(sale, store, actor, [(by_pk[pk], parts) for pk, parts in planned.items()])


def _audit(
    sale: Sale, store: Store, actor: Any, recorded: list[tuple[SaleLine, list[Funded]]]
) -> None:
    """One audit record for the split a bill recorded: nothing before, the parts after.

    The command id is derived from the bill's own key, so a replay is the same record.
    """
    subject = f"discount_funding:{sale.pk}"
    after = {
        "doc_number": sale.doc_number,
        "lines": [
            {
                "line_no": line.line_no,
                "direction": line.direction,
                "parts": [funded.as_audit(number) for number, funded in enumerate(parts, 1)],
            }
            for line, parts in sorted(recorded, key=lambda item: item[0].line_no)
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
            action=FUNDING_ACTION,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"discount-funding:{sale.idempotency_uuid}"),
            business_input={"idempotency_uuid": str(sale.idempotency_uuid)},
            site_id=store.pk,
            subject_key=subject,
        ),
        handler,
    )
