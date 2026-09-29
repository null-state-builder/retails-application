"""Size-balancing suggestions (store operations ticket 34, ST-TRF-1).

A store's broken-size alert (ticket 32) names the core sizes a style-colour
lacks there. Another store of the same company may hold that size beyond **8
weeks of its own sales**; the part beyond is what it can spare. The daily check
suggests moving the spare pieces to the store that lacks them, and makes the
suggestion only when it moves **at least 3 pieces or Rs 3,000 at MRP** (Anand,
B7). MRP, because store roles read it and never see cost. The three figures
are settings.

The rule, in order (``plan``, pure):

* **What a store can spare** of an item: the pieces it could send today (good,
  accepted, unheld, unreserved: exactly what a transfer's allocation takes), less
  what open transfer requests already ask it for, less its own sales of the item
  in the last 8 weeks - and it always keeps at least one piece, so balancing never
  takes a size away from the store that gives it.
* **What a store lacks**: each missing size of each open broken-size alert, the
  oldest alert first. It asks for as many pieces as it sold of that size in the
  last 8 weeks, and at least one.
* **Who gives**: a store already sending to it in this run first (one shipment,
  not two), then the store with the most to spare. Stores of another company never
  (that is a sale, not a transfer).
* **One suggestion per pair of stores**, carrying every item one sends the other,
  kept only if it clears the threshold.

A size somebody already approved a suggestion for is not suggested again while
its alert is open; a size rejected from one store is not suggested again from
that store while its alert is open.

Nothing here writes stock, a request or a transfer (overall PRD R-AN-007). The
daily check (``refresh``) keeps one waiting suggestion per pair of stores: the
same one while it still holds, a new one when the transfer changes, withdrawn
when it no longer holds. A person approves it into an ordinary transfer request
or rejects it with a reason (``outbound.goods_size_balancing_views``).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction

from masters.models import Store
from stockledger.broken_size import size_key

#: The rule's name and version, kept on every suggestion (R-AN-007).
RULE = "size-balance/1"
#: The advisory lock key the daily check and every decision share, so a decision
#: never lands in the middle of a check.
LOCK_KEY = "size-balancing"


@dataclass(frozen=True)
class Settings:
    weeks: int
    min_pieces: int
    min_mrp_paise: int

    def as_json(self) -> dict[str, int]:
        return {
            "weeks": self.weeks,
            "min_pieces": self.min_pieces,
            "min_mrp_paise": self.min_mrp_paise,
        }


def current_settings() -> Settings:
    from django.conf import settings

    return Settings(
        weeks=settings.KDPS_SIZE_BALANCE_WEEKS,
        min_pieces=settings.KDPS_SIZE_BALANCE_MIN_PIECES,
        min_mrp_paise=settings.KDPS_SIZE_BALANCE_MIN_MRP_PAISE,
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Need:
    """One core size one open broken-size alert says its store lacks."""

    alert_id: str
    destination_id: int
    style_id: str
    colour: str
    size: str
    brand: str = ""
    style_code: str = ""
    category: str = ""


@dataclass(frozen=True)
class Offer:
    """One item at one store that could send it."""

    source_id: int
    sku_id: str
    style_id: str
    colour: str
    size: str
    #: Pieces it could send today.
    available: int
    #: Its own sales of the item in the last 8 weeks, net of returns.
    sold: int
    mrp_paise: int | None


@dataclass(frozen=True)
class Line:
    alert_id: str
    sku_id: str
    brand: str
    style_code: str
    colour: str
    category: str
    size: str
    qty: int
    mrp_paise: int | None
    source_available: int
    source_sold: int
    destination_sold: int

    def as_json(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "sku_id": self.sku_id,
            "brand": self.brand,
            "style_code": self.style_code,
            "colour": self.colour,
            "category": self.category,
            "size": self.size,
            "qty": self.qty,
            "mrp_paise": self.mrp_paise,
            "source_available": self.source_available,
            "source_sold": self.source_sold,
            "destination_sold": self.destination_sold,
        }


@dataclass(frozen=True)
class Plan:
    """One transfer between two stores: every item one would send the other."""

    destination_id: int
    source_id: int
    lines: tuple[Line, ...]

    @property
    def pieces(self) -> int:
        return sum(line.qty for line in self.lines)

    @property
    def mrp_paise(self) -> int:
        return sum(line.qty * line.mrp_paise for line in self.lines if line.mrp_paise)

    @property
    def mrp_unknown_pieces(self) -> int:
        return sum(line.qty for line in self.lines if not line.mrp_paise)

    def clears(self, settings: Settings) -> bool:
        """At least 3 pieces, or Rs 3,000 at MRP (pieces with no MRP count for nothing)."""
        return self.pieces >= settings.min_pieces or self.mrp_paise >= settings.min_mrp_paise


def spare(offer: Offer, promised: int = 0) -> int:
    """What a store can send of an item beyond 8 weeks of its own sales, keeping one."""
    return max(0, offer.available - promised - max(offer.sold, 1))


def _item(style_id: str, colour: str, size: str) -> tuple[str, str, str]:
    return (str(style_id), size_key(colour), size_key(size))


def plan(
    needs: Sequence[Need],
    offers: Iterable[Offer],
    settings: Settings,
    *,
    entity_of: Mapping[int, Any],
    destination_sold: Mapping[tuple[int, str], int] | None = None,
    promised: Mapping[tuple[int, str], int] | None = None,
    settled: Iterable[tuple[str, str]] = (),
    refused: Iterable[tuple[str, str, int]] = (),
) -> list[Plan]:
    """The transfers that fill ``needs`` (oldest first) from the stores' spare pieces.

    ``entity_of`` names each store's company; only stores of the same one pair.
    ``destination_sold`` is each store's sales of an item in the last 8 weeks, by
    (store, item). ``promised`` is what open transfer requests already ask a
    store for, by (store, item). ``settled`` holds (alert, size) pairs a person
    approved a suggestion for; ``refused`` holds (alert, size, store) a person
    rejected from that store.
    """
    sold_at = destination_sold or {}
    asked = promised or {}
    done = {(alert, size_key(size)) for alert, size in settled}
    declined = {(alert, size_key(size), source) for alert, size, source in refused}
    by_item: dict[tuple[str, str, str], list[Offer]] = defaultdict(list)
    left: dict[tuple[int, str], int] = {}
    for offer in offers:
        key = (offer.source_id, offer.sku_id)
        if key in left:
            continue
        left[key] = spare(offer, asked.get(key, 0))
        by_item[_item(offer.style_id, offer.colour, offer.size)].append(offer)

    sending: dict[int, set[int]] = defaultdict(set)
    lines: dict[tuple[int, int], list[Line]] = defaultdict(list)
    for need in needs:
        size = size_key(need.size)
        if (need.alert_id, size) in done:
            continue
        company = entity_of.get(need.destination_id)
        candidates = [
            offer
            for offer in by_item.get(_item(need.style_id, need.colour, need.size), [])
            if offer.source_id != need.destination_id
            and company is not None
            and entity_of.get(offer.source_id) == company
            and (need.alert_id, size, offer.source_id) not in declined
            and left[(offer.source_id, offer.sku_id)] > 0
        ]
        if not candidates:
            continue
        items = sorted({offer.sku_id for offer in candidates})
        sold_here = {sku: sold_at.get((need.destination_id, sku), 0) for sku in items}
        wanted = max(1, sum(sold_here.values()))
        candidates.sort(
            key=lambda o: (
                o.source_id not in sending[need.destination_id],
                -left[(o.source_id, o.sku_id)],
                o.source_id,
                o.sku_id,
            )
        )
        for offer in candidates:
            if wanted <= 0:
                break
            key = (offer.source_id, offer.sku_id)
            take = min(wanted, left[key])
            left[key] -= take
            wanted -= take
            sending[need.destination_id].add(offer.source_id)
            lines[(need.destination_id, offer.source_id)].append(
                Line(
                    alert_id=need.alert_id,
                    sku_id=offer.sku_id,
                    brand=need.brand,
                    style_code=need.style_code,
                    colour=need.colour,
                    category=need.category,
                    size=need.size,
                    qty=take,
                    mrp_paise=offer.mrp_paise,
                    source_available=offer.available,
                    source_sold=offer.sold,
                    destination_sold=sold_here[offer.sku_id],
                )
            )
    plans = [
        Plan(destination_id=destination, source_id=source, lines=tuple(rows))
        for (destination, source), rows in lines.items()
    ]
    return [p for p in plans if p.clears(settings)]


# ---------------------------------------------------------------------------
# The readers
# ---------------------------------------------------------------------------


def balancing_stores() -> list[Store]:
    """Active selling stores with the size-balancing switch on."""
    from masters.store_feature_registry import SIZE_BALANCING
    from masters.store_features import feature, switch_states

    stores = list(
        Store.objects.select_related("gstin").filter(
            is_active=True, store_type=Store.StoreType.STORE
        )
    )
    states = switch_states(stores, [feature(SIZE_BALANCING)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def sold_since(
    store: Store, sku_ids: Iterable[Any], since: datetime, until: datetime
) -> dict[str, int]:
    """Pieces of each item this store sold in ``[since, until)``, net of returns (never below 0).

    A bill line names the barcode the counter scanned; a code that has ever named
    two items is left out (Stock Ageing's rule). A cancelled bill sold nothing.
    """
    from django.db.models import Sum

    from core.documents import DocStatus
    from sell.models import SaleLine
    from stockledger.goods_ageing import item_barcodes

    wanted = {uuid.UUID(str(sku)) for sku in sku_ids}
    if not wanted:
        return {}
    item_of = {
        code: sku
        for code, sku in item_barcodes(store, wanted, until).item_of.items()
        if sku in wanted
    }
    if not item_of:
        return {}
    rows = (
        SaleLine.objects.filter(
            sale__store=store,
            barcode__in=sorted(item_of),
            sale__billed_at__gte=since,
            sale__billed_at__lt=until,
        )
        .exclude(sale__docstatus=DocStatus.CANCELLED)
        .values("barcode", "direction")
        .annotate(pieces=Sum("qty"))
    )
    net: dict[str, int] = defaultdict(int)
    for row in rows:
        sign = -1 if row["direction"] == SaleLine.Direction.RETURN else 1
        net[str(item_of[row["barcode"]])] += sign * int(row["pieces"] or 0)
    return {sku: pieces for sku, pieces in net.items() if pieces > 0}


def _sendable(site_id: int, sku_id: uuid.UUID) -> tuple[int, int | None]:
    """Pieces a transfer could take today, and the MRP on the newest of their origins."""
    from stockledger import goods_engine as engine
    from stockledger import ranges
    from stockledger.goods_models import Origin

    portions = engine.eligible_portions(site_id, sku_id, purpose="transfer")
    pieces = sum(ranges.length(p.interval) for p in portions)
    origin_ids = {
        p.address.origin_id or p.address.value_basis_origin_id
        for p in portions
        if p.address.origin_id or p.address.value_basis_origin_id
    }
    newest = (
        Origin.objects.filter(pk__in=sorted(origin_ids, key=str))
        .order_by("-source_time", "-pk")
        .values_list("mrp", flat=True)
        .first()
    )
    return pieces, (int(newest) or None) if newest is not None else None


def _promised(site_ids: Iterable[int]) -> dict[tuple[int, str], int]:
    """What transfer requests already ask each store for, per item, until it is reserved.

    An open request, and a drafted one whose transfer is not approved yet: approval
    is what reserves the pieces, and from then on they are no longer available.
    """
    from django.db.models import Q

    from outbound.goods_models import GoodsTransfer, TransferRequest

    asked: dict[tuple[int, str], int] = defaultdict(int)
    waiting = Q(state=TransferRequest.State.OPEN) | Q(
        state=TransferRequest.State.DRAFTED,
        transfer__state__in=[GoodsTransfer.State.DRAFT, GoodsTransfer.State.SUBMITTED],
    )
    for source, lines in TransferRequest.objects.filter(
        waiting, source_site_id__in=sorted(site_ids)
    ).values_list("source_site_id", "lines"):
        for line in lines or []:
            asked[(source, str(line.get("sku_id")))] += int(line.get("qty") or 0)
    return dict(asked)


def _decided(
    alert_ids: set[str], destinations: Iterable[int]
) -> tuple[set[tuple[str, str]], set[tuple[str, str, int]]]:
    """(alert, size) already approved, and (alert, size, store) rejected, for these alerts."""
    from outbound.size_balancing_models import SizeBalanceSuggestion

    settled: set[tuple[str, str]] = set()
    refused: set[tuple[str, str, int]] = set()
    decided = SizeBalanceSuggestion.objects.filter(
        state__in=[SizeBalanceSuggestion.State.APPROVED, SizeBalanceSuggestion.State.REJECTED],
        destination_site_id__in=sorted(set(destinations)),
    ).values_list("state", "source_site_id", "lines")
    for state, source, lines in decided:
        for line in lines or []:
            alert = str(line.get("alert_id"))
            if alert not in alert_ids:
                continue
            size = size_key(line.get("size", ""))
            if state == SizeBalanceSuggestion.State.APPROVED:
                settled.add((alert, size))
            else:
                refused.add((alert, size, source))
    return settled, refused


@dataclass(frozen=True)
class Found:
    plans: list[Plan]
    #: Stores that can be a destination today: switch on here and broken sizes checked.
    destinations: set[int]
    #: Stores that can send today: switch on.
    sources: set[int]


def find(now: datetime, settings: Settings) -> Found:
    """Read today's broken sizes, stock and sales, and plan the transfers."""
    from core.tenancy import require_tenant_id
    from masters.goods_identity_models import ProductSku
    from masters.goods_identity_services import candidates_for
    from stockledger.broken_size import checked_stores, open_broken_sizes
    from stockledger.goods_models import Position

    stores = balancing_stores()
    ids = {store.pk for store in stores}
    checked = {store.pk for store in checked_stores()}
    destinations = ids & checked
    alerts = open_broken_sizes(destinations)
    if not alerts:
        return Found(plans=[], destinations=destinations, sources=ids)
    needs = [
        Need(
            alert_id=str(alert.pk),
            destination_id=alert.site_id,
            style_id=str(alert.style_id),
            colour=alert.colour,
            size=size,
            brand=alert.brand,
            style_code=alert.style_code,
            category=alert.category,
        )
        for alert in alerts
        for size in alert.missing_sizes
    ]
    wanted = {_item(n.style_id, n.colour, n.size) for n in needs}
    style_of = dict(
        ProductSku.objects.filter(style_id__in=sorted({n.style_id for n in needs})).values_list(
            "pk", "style_id"
        )
    )
    described = {
        row["sku_id"]: row
        for row in candidates_for(require_tenant_id(), {str(s) for s in style_of})
    }
    items: dict[str, tuple[str, str]] = {}
    for sku_id, style_id in style_of.items():
        row = described.get(str(sku_id), {})
        colour, size = str(row.get("colour") or ""), str(row.get("size") or "unknown")
        if _item(str(style_id), colour, size) in wanted:
            items[str(sku_id)] = (colour, size)
    if not items:
        return Found(plans=[], destinations=destinations, sources=ids)

    held = (
        Position.objects.filter(
            site_id__in=sorted(ids),
            sku_id__in=sorted(items),
            boundary="physical",
            condition="good",
            accepted_event__isnull=False,
        )
        .values_list("site_id", "sku_id")
        .distinct()
    )
    since = now - timedelta(weeks=settings.weeks)
    by_id = {store.pk: store for store in stores}
    sold: dict[tuple[int, str], int] = {}
    for store in stores:
        for sku, pieces in sold_since(store, items, since, now).items():
            sold[(store.pk, sku)] = pieces
    offers: list[Offer] = []
    for site_id, sku_id in sorted(set(held), key=lambda pair: (pair[0], str(pair[1]))):
        available, mrp = _sendable(site_id, sku_id)
        if available <= 0:
            continue
        colour, size = items[str(sku_id)]
        offers.append(
            Offer(
                source_id=site_id,
                sku_id=str(sku_id),
                style_id=str(style_of[sku_id]),
                colour=colour,
                size=size,
                available=available,
                sold=sold.get((site_id, str(sku_id)), 0),
                mrp_paise=mrp,
            )
        )
    settled, refused = _decided({n.alert_id for n in needs}, {n.destination_id for n in needs})
    plans = plan(
        needs,
        offers,
        settings,
        entity_of={pk: store.gstin.legal_entity_id for pk, store in by_id.items()},
        destination_sold=sold,
        promised=_promised(ids),
        settled=settled,
        refused=refused,
    )
    return Found(plans=plans, destinations=destinations, sources=ids)


# ---------------------------------------------------------------------------
# The daily check
# ---------------------------------------------------------------------------


def lock() -> None:
    """The lock every check and decision takes (``LOCK_KEY``), as a command's advisory lock."""
    from django.db import connection

    from core.canonical import content_hash
    from core.tenancy import require_tenant_id

    number = int(content_hash([str(require_tenant_id()), LOCK_KEY])[:15], 16)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [number])


def _signature(lines: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, int]]:
    return sorted((str(x["alert_id"]), str(x["sku_id"]), int(x["qty"])) for x in lines)


@dataclass(frozen=True)
class Refreshed:
    waiting: int
    made: int
    withdrawn: int


@transaction.atomic
def refresh(now: datetime, settings: Settings | None = None) -> Refreshed:
    """Make the waiting suggestions match what is true now.

    Per pair of stores: the same transfer still planned keeps its suggestion
    (checked again); a different one withdraws it and makes a new one; none
    withdraws it, saying why. Everything is read after taking the lock, so a
    decision is never made against a suggestion this check is replacing.
    """
    from core.tenancy import require_tenant_id
    from outbound.size_balancing_models import SizeBalanceSuggestion

    rule = settings or current_settings()
    lock()
    found = find(now, rule)
    fresh = {(p.destination_id, p.source_id): p for p in found.plans}
    made = withdrawn = 0
    for old in SizeBalanceSuggestion.objects.select_for_update().filter(
        state=SizeBalanceSuggestion.State.PENDING
    ):
        key = (old.destination_site_id, old.source_site_id)
        new = fresh.get(key)
        if new is not None and _signature(old.lines) == _signature(
            line.as_json() for line in new.lines
        ):
            old.checked_at = now
            old.lines = [line.as_json() for line in new.lines]
            old.mrp_paise, old.mrp_unknown_pieces = new.mrp_paise, new.mrp_unknown_pieces
            old.save(update_fields=["checked_at", "lines", "mrp_paise", "mrp_unknown_pieces"])
            del fresh[key]
            continue
        if new is not None:
            reason = SizeBalanceSuggestion.Withdrawn.REPLACED
        elif key[0] not in found.destinations or key[1] not in found.sources:
            reason = SizeBalanceSuggestion.Withdrawn.NOT_CHECKED
        else:
            reason = SizeBalanceSuggestion.Withdrawn.NOT_NEEDED
        old.state = SizeBalanceSuggestion.State.WITHDRAWN
        old.withdrawn_reason = reason
        old.decided_at = now
        old.save(update_fields=["state", "withdrawn_reason", "decided_at"])
        withdrawn += 1
    for new in fresh.values():
        SizeBalanceSuggestion.objects.create(
            tenant_id=require_tenant_id(),
            destination_site_id=new.destination_id,
            source_site_id=new.source_id,
            lines=[line.as_json() for line in new.lines],
            pieces=new.pieces,
            mrp_paise=new.mrp_paise,
            mrp_unknown_pieces=new.mrp_unknown_pieces,
            rule=RULE,
            rule_settings=rule.as_json(),
            made_at=now,
            checked_at=now,
        )
        made += 1
    waiting = SizeBalanceSuggestion.objects.filter(
        state=SizeBalanceSuggestion.State.PENDING
    ).count()
    return Refreshed(waiting=waiting, made=made, withdrawn=withdrawn)
