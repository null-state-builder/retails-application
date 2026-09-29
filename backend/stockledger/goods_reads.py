"""Goods-v1 stock reads (design E171-E176, E181).

Every read answers inside the caller's trusted scope: the sites and brands where
``stock.view`` is granted, intersected with the tenant's goods sites. A read
names its watermark. Without ``as_of`` it reads the live projections; with a past
``as_of`` it replays the append-only journal instead of trusting today's rows:

* quantity - every ``QuantityLeg`` recorded by the watermark adds ``+1`` (a
  destination) or ``-1`` (a source) over its portion at its exact address. The
  segments of a lot whose signed sum is positive at an address are present
  there. The sum is order-independent, so chained moves inside one journal batch
  net out without knowing the order they were written in.
* holds and reservations - the same signed sum over ``EncumbranceLeg`` rows with
  ``gate="active"``, per lot and axis.
* a P03 value-damage destination leg overlays its frozen value basis on the
  exact lot portion without manufacturing a quantity movement.

Both paths hand the same ``engine.Portion`` views to ``engine.build_stock_rows``,
so live and replayed answers cannot follow different eligibility or money rules.

What a replay cannot reproduce, it declares. Site readiness, location kinds and
origin cost records keep no history, so a past read still reads them from
today's rows: every stock row and the summary carry the ``READ_LIMITATIONS``
codes saying so (R27). The journal (E171) replays its own legs faithfully but
still prices them from today's origins, so it declares ``JOURNAL_LIMITATIONS``.
A reader renders that answer; it never works the limitation out from the
timestamp it asked for.

Money is integer paise as a string and appears only for the requested basis:
``cost`` carries both values and ``ticket`` ticket value only, and both are layer
value, so both need the ``cost`` field grant on every site in scope and are
refused (never silently dropped) without it (ruled by Anand, 15 September 2026);
``quantity`` carries no money at all, and its summary no value totals.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from django.apps import apps
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.goods_api import (
    LIST_QUERY_KEYS,
    decode_cursor,
    encode_cursor,
    page_limit,
    paginate,
    parse_int_id,
    parse_uuid,
)
from accounts.principal import AccessContext
from core.goods_fields import bounds, portion
from core.refusals import Refusal, issue
from masters.goods_identity_models import ProductSku
from masters.goods_models import Location, Sbu, SiteGuard
from masters.models import Brand, Season, Store
from outbound.goods_models import GoodsTransfer
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import (
    AcceptanceEvent,
    CoverageEvent,
    CustodyLot,
    EncumbranceLeg,
    HoldEvent,
    Origin,
    Position,
    QuantityLeg,
    ReservationEvent,
    ValueLeg,
)

READ = "stock.view"
BASES = ("quantity", "cost", "ticket")
#: R27 (change PRD §14.9, design §6.1): what a past answer still reads from
#: today's rows, because those rows keep no history of their own.
#:
#: ``current_site_readiness``   ``SiteGuard`` and the site's purpose, which
#:                              decide the readiness eligibility reasons.
#: ``current_location_kind``    a location's kind, which decides whether the
#:                              address is sellable, quarantine or neither.
#: ``current_cost_projection``  an origin's unit cost and MRP, which carry the
#:                              value a portion is reported at.
#:
#: All three apply to every past *stock* read until their histories are
#: versioned; a live read declares none. "Applicable" means "still unversioned",
#: not "could have moved this particular row": every stock row's eligibility
#: consults site readiness and location kind whether or not the row shows a
#: location or any money, so the list is all-or-nothing and shrinks only when a
#: history is actually built. The list is closed and identical for every reader,
#: so it discloses nothing about what a particular caller may or may not open.
READ_LIMITATIONS = ("current_site_readiness", "current_location_kind", "current_cost_projection")
#: What the operational journal (E171) still takes from today. A journal entry
#: reports the leg exactly as it was recorded - its own addresses and quantity -
#: and consults neither site readiness nor location kind to do it, so declaring
#: either would be untrue. It does price the entry from today's ``Origin`` rows
#: (``_origin_prices``), which is the cost-record projection R27 names.
JOURNAL_LIMITATIONS = ("current_cost_projection",)
CONDITIONS = ("good", "damaged", "wrong", "unidentified")
#: Physical locations that are quarantine by kind, whatever the condition.
QUARANTINE_KINDS = frozenset({"quarantine", "excess_hold", "rtv_hold"})
STOCK_QUERY_KEYS = LIST_QUERY_KEYS | {
    "sku_id",
    "origin_id",
    "location_id",
    "condition",
    "state",
    "basis",
    "as_of",
    "season",
}
#: ``season=unknown_historical`` asks for the one explicit unknown historical
#: cohort by meaning rather than by id, so a link survives a reseeded master.
UNKNOWN_HISTORICAL = "unknown_historical"
JOURNEY_QUERY_KEYS = frozenset({"cursor", "limit"})
#: ``state`` on stock rows: which part of the row the caller is asking about.
ROW_STATES: dict[str, Callable[[dict[str, Any]], bool]] = {
    "available": lambda row: row["ats_qty"] > 0 or row["transferable_qty"] > 0,
    "accepted": lambda row: row["accepted_qty"] > 0,
    "not_accepted": lambda row: row["accepted_qty"] < row["physical_qty"],
    "held": lambda row: row["held_qty"] > 0,
    "reserved": lambda row: row["reserved_qty"] > 0,
    "unvalued": lambda row: row["valued_qty"] < row["physical_qty"],
}
#: ``state`` on journal entries is the posting kind (P01-P18).
POSTING_KIND = re.compile(r"^P\d{2}$")

Interval = ranges.Interval


def _invalid(field: str, message: str) -> Refusal:
    return Refusal("INVALID_REQUEST", message, issues=[issue("INVALID", message, field=field)])


# ---------------------------------------------------------------------------
# Query and scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    """One grant's reach: these sites, and (for a brand or SBU grant) only that brand's SKUs."""

    sites: frozenset[int]
    skus: frozenset[uuid.UUID] | None
    brand_id: int | None = None

    def covers(self, site_id: int | None, sku_id: uuid.UUID | None) -> bool:
        if site_id is None or site_id not in self.sites:
            return False
        return self.skus is None or (sku_id is not None and sku_id in self.skus)

    def leg_q(self) -> Q:
        where = Q(position__site_id__in=sorted(self.sites))
        if self.skus is not None:
            where &= Q(position__sku_id__in=sorted(str(s) for s in self.skus))
        return where


@dataclass(frozen=True)
class StockQuery:
    tenant_id: uuid.UUID
    site_ids: frozenset[int]
    scope_kind: str
    terms: tuple[Term, ...]
    sku_id: uuid.UUID | None
    origin_id: uuid.UUID | None
    location_id: uuid.UUID | None
    brand_id: int | None
    sbu_id: uuid.UUID | None
    brand_skus: frozenset[uuid.UUID] | None
    condition: str | None
    state: str | None
    basis: str
    #: The season in force on a row, as ``unknown_historical`` or a season id.
    season: str | None
    #: A past watermark to replay the journal at; ``None`` reads live projections.
    replay_at: datetime | None
    watermark: datetime
    text: str

    @property
    def money(self) -> tuple[str, ...]:
        if self.basis == "cost":
            return (engine.COST_VALUE, engine.TICKET_VALUE)
        if self.basis == "ticket":
            return (engine.TICKET_VALUE,)
        return ()

    def visible(self, site_id: int | None, sku_id: uuid.UUID | None) -> bool:
        return any(term.covers(site_id, sku_id) for term in self.terms)

    def matches(self, item: engine.Portion) -> bool:
        address = item.address
        if self.sku_id is not None and address.sku_id != self.sku_id:
            return False
        if self.origin_id is not None and self.origin_id not in (
            address.origin_id,
            address.value_basis_origin_id,
        ):
            return False
        if self.location_id is not None and address.location_id != self.location_id:
            return False
        if self.condition is not None and address.condition != self.condition:
            return False
        if self.brand_skus is not None and address.sku_id not in self.brand_skus:
            return False
        return not self.text or self.text.lower() in item.description.lower()

    def keep_row(self, row: dict[str, Any]) -> bool:
        return self.state is None or ROW_STATES[self.state](row)


def _optional_uuid(params: dict[str, str], name: str) -> uuid.UUID | None:
    return parse_uuid(params[name], name) if params.get(name) else None


def _optional_int(params: dict[str, str], name: str) -> int | None:
    return parse_int_id(params[name], name) if params.get(name) else None


def _as_of(raw: str | None, now: datetime) -> tuple[datetime | None, datetime]:
    if not raw:
        return None, now
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise _invalid("as_of", "as_of must be a timestamp with a time zone.")
    if parsed >= now:
        return None, now
    return parsed, parsed


def _grant_terms(access: AccessContext, sites: frozenset[int]) -> tuple[Term, ...]:
    brand_skus: dict[int, frozenset[uuid.UUID]] = {}
    terms: list[Term] = []
    for grant in access.grants:
        if READ not in grant.actions:
            continue
        reach = frozenset(s for s in sites if access.reaches_site(grant, s))
        if not reach:
            continue
        # One term represents one complete assignment cell. A selected-brand
        # assignment must never turn into a brand-unrestricted stock term merely
        # because it contains more than one brand.
        if grant.all_brands:
            terms.append(Term(reach, None, None))
            continue
        for brand_id in sorted(grant.brand_ids):
            if brand_id not in brand_skus:
                brand_skus[brand_id] = _skus_of_brand(access.tenant_id, brand_id)
            terms.append(Term(reach, brand_skus[brand_id], brand_id))
    return tuple(terms)


def _skus_of_brand(tenant_id: uuid.UUID, brand_id: int) -> frozenset[uuid.UUID]:
    return frozenset(
        ProductSku.objects.filter(tenant_id=tenant_id, style__brand_id=brand_id).values_list(
            "pk", flat=True
        )
    )


def require_reader(access: AccessContext) -> None:
    if READ not in access.all_actions():
        raise Refusal("ACTION_DENIED", "You do not have permission to read stock.")


def tenant_sites(access: AccessContext) -> frozenset[int]:
    return frozenset(
        SiteGuard.objects.filter(tenant_id=access.tenant_id).values_list("site_id", flat=True)
    )


@dataclass(frozen=True)
class _Input:
    basis: str
    condition: str | None
    state: str | None
    season: str | None
    text: str
    replay_at: datetime | None
    watermark: datetime
    site_id: int | None
    brand_id: int | None
    sbu_id: uuid.UUID | None
    sku_id: uuid.UUID | None
    origin_id: uuid.UUID | None
    location_id: uuid.UUID | None


def _choice(params: dict[str, str], name: str, allowed: Iterable[str]) -> str | None:
    value = params.get(name) or None
    if value is not None and value not in allowed:
        raise _invalid(name, f"{name} is not a supported value for this read.")
    return value


def _parse_input(params: dict[str, str], *, entries: bool) -> _Input:
    """The closed input schema (INVALID_REQUEST before any scope lookup)."""
    decode_cursor(params.get("cursor"))
    page_limit(params)
    _choice(params, "record_contract", ("goods-v1",))
    state = params.get("state") or None
    if state is not None:
        known = POSTING_KIND.match(state) is not None if entries else state in ROW_STATES
        if len(state) > 40 or not known:
            raise _invalid("state", "state is not a supported value for this read.")
    text = params.get("q") or ""
    if len(text) > 100:
        raise _invalid("q", "q is at most 100 characters.")
    season = params.get("season") or None
    if season is not None and season != UNKNOWN_HISTORICAL:
        # Anything else must be a season id; a name would be ambiguous.
        season = str(parse_int_id(season, "season"))
    replay_at, watermark = _as_of(params.get("as_of"), timezone.now())
    return _Input(
        basis=_choice(params, "basis", BASES) or "quantity",
        condition=_choice(params, "condition", CONDITIONS),
        state=state,
        season=season,
        text=text,
        replay_at=replay_at,
        watermark=watermark,
        site_id=_optional_int(params, "site_id"),
        brand_id=_optional_int(params, "brand_id"),
        sbu_id=_optional_uuid(params, "sbu_id"),
        sku_id=_optional_uuid(params, "sku_id"),
        origin_id=_optional_uuid(params, "origin_id"),
        location_id=_optional_uuid(params, "location_id"),
    )


def _scope(access: AccessContext, given: _Input) -> tuple[frozenset[int], str, int | None]:
    """Sites, scope kind and brand of the read; a filter outside scope is NOT_FOUND."""
    everywhere = tenant_sites(access)
    # Sites any read grant reaches; each grant's own brand limit applies per row (``Term``).
    granted = access.site_reach(READ)
    sites = everywhere if granted is None else everywhere & frozenset(granted)
    scope_kind = "tenant" if granted is None else "sites"
    if given.site_id is not None:
        if given.site_id not in sites:
            raise Refusal("NOT_FOUND", "That site was not found.")
        sites, scope_kind = frozenset({given.site_id}), "sites"
    brand = None
    if given.sbu_id is not None:
        sbu = Sbu.objects.filter(tenant_id=access.tenant_id, pk=given.sbu_id).first()
        if (
            sbu is None
            or sbu.site_id not in sites
            or not access.can(READ, site_id=sbu.site_id, brand_id=sbu.brand_id)
        ):
            raise Refusal("NOT_FOUND", "That SBU was not found.")
        sites, scope_kind, brand = frozenset({sbu.site_id}), "sbus", sbu.brand_id
    if given.brand_id is not None:
        if (
            not Brand.objects.filter(pk=given.brand_id).exists()
            or not any(access.can(READ, site_id=site, brand_id=given.brand_id) for site in sites)
            or brand not in (None, given.brand_id)
        ):
            raise Refusal("NOT_FOUND", "That brand was not found.")
        brand = given.brand_id
        scope_kind = "brands" if scope_kind == "tenant" else scope_kind
    return sites, scope_kind, brand


def _check_filters(
    tenant_id: uuid.UUID, given: _Input, sites: frozenset[int], terms: tuple[Term, ...]
) -> None:
    checks = (
        (given.sku_id, ProductSku.objects.filter(tenant_id=tenant_id), "SKU"),
        (
            given.location_id,
            Location.objects.filter(tenant_id=tenant_id, site_id__in=sorted(sites)),
            "location",
        ),
    )
    for value, queryset, label in checks:
        if value is not None and not queryset.filter(pk=value).exists():
            raise Refusal("NOT_FOUND", f"That {label} was not found.")
    if given.origin_id is not None and not _origin_visible(tenant_id, given.origin_id, terms):
        raise Refusal("NOT_FOUND", "That origin was not found.")


def _origin_visible(tenant_id: uuid.UUID, origin_id: uuid.UUID, terms: tuple[Term, ...]) -> bool:
    """An origin is visible where a read term covers its receipt site or a site holding it now.

    A missing origin and one outside every term are hidden the same way.
    """
    found = (
        Origin.objects.filter(tenant_id=tenant_id, pk=origin_id)
        .values_list("site_id", "sku_id")
        .first()
    )
    if found is None:
        return False
    receipt_site, sku_id = found
    held_at = set(
        Position.objects.filter(tenant_id=tenant_id, origin_id=origin_id, boundary="physical")
        .values_list("site_id", flat=True)
        .distinct()
    )
    return any(term.covers(site, sku_id) for term in terms for site in {receipt_site, *held_at})


def _brand_limited(grant: Any) -> bool:
    """Any selected-brand assignment reaches only its own brands' goods."""
    return not grant.all_brands


def _cost_granted(access: AccessContext, site_id: int, brand_id: int | None) -> bool:
    """A grant carrying ``cost`` covers this site for this brand (or, unbranded, every brand)."""
    return any(
        READ in grant.actions
        and "cost" in grant.fields
        and not (brand_id is None and _brand_limited(grant))
        and access.grant_covers(grant, site_id, brand_id)
        for grant in access.grants
    )


def reach_cells(terms: tuple[Term, ...], brand: int | None) -> set[tuple[int, int | None]]:
    """Every ``(site, brand)`` cell these grant terms can actually yield a row for.

    Each ``stock.view`` term contributes rows for its sites and (when brand scoped)
    its brand; a query brand narrows every term, and a term limited to a *different*
    brand can yield no row at all and contributes no cell.

    Two callers need exactly this set and must not disagree about it: the value-grant
    check below, and the durable export, which records these cells as the file's own
    scope. A cell either of them invents is one the requester cannot cover - which
    would refuse them the very file they asked for (GSA-T18).
    """
    reach: set[tuple[int, int | None]] = set()
    for term in terms:
        if brand is not None and term.brand_id not in (None, brand):
            continue  # this term can yield no row of the requested brand
        reach.update((site, brand if brand is not None else term.brand_id) for site in term.sites)
    return reach


def _check_basis(
    access: AccessContext, basis: str, terms: tuple[Term, ...], brand: int | None
) -> None:
    """Value is refused, never silently dropped, unless granted for every term that yields rows.

    Every basis but ``quantity`` asks for layer value - cost value or ticket value -
    and so needs the same ``cost`` field grant (ruled by Anand, 15 September 2026).
    Cost held for one brand therefore never prices another brand's rows reached
    through a wider, cost-less grant.
    """
    if basis == "quantity":
        return
    reach = reach_cells(terms, brand)
    granted = (
        all(_cost_granted(access, site, reach_brand) for site, reach_brand in reach)
        if reach
        else "cost" in access.field_grants(brand_id=brand, actions={READ})
    )
    if not granted:
        raise Refusal(
            "FIELD_DENIED",
            "Stock values need the cost field grant for every site in this read.",
            status=403,
            issues=[issue("FIELD_DENIED", "cost is not granted", field="basis")],
        )


def resolve_query(
    access: AccessContext, params: dict[str, str], *, entries: bool = False
) -> StockQuery:
    """Validate a stock read query and resolve it inside the caller's trusted scope."""
    given = _parse_input(params, entries=entries)
    require_reader(access)
    sites, scope_kind, brand = _scope(access, given)
    terms = _grant_terms(access, sites)
    _check_filters(access.tenant_id, given, sites, terms)
    _check_basis(access, given.basis, terms, brand)
    return StockQuery(
        tenant_id=access.tenant_id,
        site_ids=sites,
        scope_kind=scope_kind,
        terms=terms,
        sku_id=given.sku_id,
        origin_id=given.origin_id,
        location_id=given.location_id,
        brand_id=brand,
        sbu_id=given.sbu_id,
        brand_skus=_skus_of_brand(access.tenant_id, brand) if brand is not None else None,
        condition=given.condition,
        state=given.state,
        season=given.season,
        basis=given.basis,
        replay_at=given.replay_at,
        watermark=given.watermark,
        text=given.text,
    )


# ---------------------------------------------------------------------------
# Portions at a watermark
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    physical: list[engine.Portion]
    transit: list[engine.Portion]
    holds: dict[uuid.UUID, list[Interval]]
    reservations: dict[uuid.UUID, list[Interval]]
    guards: dict[int, SiteGuard]
    purposes: dict[int, str]
    #: transfer -> (source site, destination site), for transfers touching scope.
    transfers: dict[uuid.UUID, tuple[int, int]]


def load(query: StockQuery, *, transit: bool = False) -> Snapshot:
    sites = sorted(query.site_ids)
    transfers: dict[uuid.UUID, tuple[int, int]] = {}
    if transit and sites:
        transfers = {
            pk: (source, destination)
            for pk, source, destination in GoodsTransfer.objects.filter(tenant_id=query.tenant_id)
            .filter(Q(source_site_id__in=sites) | Q(destination_site_id__in=sites))
            .values_list("pk", "source_site_id", "destination_site_id")
        }
    if query.replay_at is None:
        physical = engine.portions_from(
            Position.objects.filter(
                tenant_id=query.tenant_id, boundary="physical", site_id__in=sites
            )
        )
        moving = (
            engine.portions_from(
                Position.objects.filter(
                    tenant_id=query.tenant_id, boundary="transit", transfer_id__in=list(transfers)
                )
            )
            if transfers
            else []
        )
        holds, reservations = engine.active_encumbrances(
            sorted({p.lot_id for p in [*physical, *moving]}, key=str)
        )
    else:
        physical, moving, holds, reservations = replay(
            query.tenant_id, query.site_ids, transfers, query.replay_at
        )
    return Snapshot(
        physical=physical,
        transit=moving,
        holds=dict(holds),
        reservations=dict(reservations),
        guards={
            guard.site_id: guard
            for guard in SiteGuard.objects.filter(tenant_id=query.tenant_id, site_id__in=sites)
        },
        purposes=engine.site_purposes(
            dict(Store.objects.filter(pk__in=sites).values_list("pk", "store_type"))
        ),
        transfers=transfers,
    )


def present(deltas: Iterable[tuple[int, int, int]]) -> list[Interval]:
    """Segments whose signed coverage is positive, from ``(lower, upper, +1|-1)`` legs."""
    points: dict[int, int] = defaultdict(int)
    for lower, upper, sign in deltas:
        points[lower] += sign
        points[upper] -= sign
    ordered = sorted(points)
    running = 0
    segments: list[Interval] = []
    for index, point in enumerate(ordered[:-1]):
        running += points[point]
        if running > 0:
            segments.append((point, ordered[index + 1]))
    return ranges.normalise(segments)


def _uid(value: Any) -> uuid.UUID | None:
    return uuid.UUID(str(value)) if value else None


def address_from_json(data: dict[str, Any]) -> engine.Address:
    site = data.get("site_id")
    return engine.Address(
        boundary=str(data.get("boundary")),
        site_id=int(site) if site is not None else None,
        location_id=_uid(data.get("location_id")),
        transfer_id=_uid(data.get("transfer_id")),
        condition=str(data.get("condition") or "good"),
        sku_id=_uid(data.get("sku_id")),
        origin_id=_uid(data.get("origin_id")),
        value_basis_origin_id=_uid(data.get("value_basis_origin_id")),
        accepted_event_id=_uid(data.get("accepted_event_id")),
        reason=data.get("reason"),
    )


def _value_basis_at(
    tenant_id: uuid.UUID,
    at: datetime,
    rows: list[tuple[uuid.UUID, Interval, engine.Address]],
) -> list[tuple[uuid.UUID, Interval, engine.Address]]:
    """Overlay P03 value legs without pretending they were quantity legs."""
    lot_ids = {lot_id for lot_id, _interval, _address in rows}
    valued: dict[uuid.UUID, list[tuple[Interval, uuid.UUID]]] = defaultdict(list)
    for lot_id, stored, origin_id in ValueLeg.objects.filter(
        tenant_id=tenant_id,
        batch__posting_kind="P03",
        side="destination",
        bucket="stock",
        recorded_at__lte=at,
        lot_id__in=lot_ids,
    ).values_list("lot_id", "portion", "origin_id"):
        if stored is not None:
            valued[lot_id].append((bounds(stored), origin_id))

    out: list[tuple[uuid.UUID, Interval, engine.Address]] = []
    for lot_id, interval, address in rows:
        matches = [
            (piece, origin_id)
            for assigned, origin_id in valued.get(lot_id, [])
            for piece in ranges.intersect([interval], [assigned])
        ]
        out.extend(
            (lot_id, piece, replace(address, value_basis_origin_id=origin_id))
            for piece, origin_id in matches
        )
        out.extend(
            (lot_id, piece, address)
            for piece in ranges.subtract([interval], [piece for piece, _origin_id in matches])
        )
    return out


def replay(
    tenant_id: uuid.UUID,
    sites: Iterable[int],
    transfers: dict[uuid.UUID, tuple[int, int]],
    at: datetime,
) -> tuple[
    list[engine.Portion],
    list[engine.Portion],
    dict[uuid.UUID, list[Interval]],
    dict[uuid.UUID, list[Interval]],
]:
    """Physical and transit portions, holds and reservations as recorded by ``at``."""
    site_set = frozenset(sites)
    legs = QuantityLeg.objects.filter(tenant_id=tenant_id, recorded_at__lte=at)
    touching = Q(position__site_id__in=sorted(site_set))
    if transfers:
        touching |= Q(position__transfer_id__in=sorted(str(t) for t in transfers))
    marks: dict[tuple[uuid.UUID, str], list[tuple[int, int, int]]] = defaultdict(list)
    addresses: dict[str, dict[str, Any]] = {}
    for lot_id, stored, position, qty in legs.filter(
        lot_id__in=legs.filter(touching).values("lot_id")
    ).values_list("lot_id", "portion", "position", "qty"):
        key = json.dumps(position, sort_keys=True, separators=(",", ":"))
        addresses[key] = position
        lower, upper = bounds(stored)
        marks[(lot_id, key)].append((lower, upper, 1 if qty > 0 else -1))

    here: list[tuple[uuid.UUID, Interval, engine.Address]] = []
    for (lot_id, key), deltas in marks.items():
        address = address_from_json(addresses[key])
        at_site = address.boundary == "physical" and address.site_id in site_set
        moving = address.boundary == "transit" and address.transfer_id in transfers
        if not (at_site or moving):
            continue
        here.extend((lot_id, interval, address) for interval in present(deltas))

    here = _value_basis_at(tenant_id, at, here)
    lot_ids = sorted({lot_id for lot_id, _i, _a in here}, key=str)
    lots = {
        pk: (source_time, identity)
        for pk, source_time, identity in CustodyLot.objects.filter(pk__in=lot_ids).values_list(
            "pk", "source_time", "initial_identity"
        )
    }
    origin_ids = {a.origin_id or a.value_basis_origin_id for _l, _i, a in here} - {None}
    origins = {o.pk: o for o in Origin.objects.filter(pk__in=list(origin_ids))}
    kinds = engine.location_kinds({a.location_id for _l, _i, a in here if a.location_id})
    physical: list[engine.Portion] = []
    moving_out: list[engine.Portion] = []
    for lot_id, interval, address in here:
        source_time, identity = lots[lot_id]
        origin_pk = address.origin_id or address.value_basis_origin_id
        basis = origins.get(origin_pk) if origin_pk else None
        own = origins.get(address.origin_id) if address.origin_id else None
        if basis is not None and address.sku_id is None:
            address = replace(address, sku_id=basis.sku_id)
        item = engine.Portion(
            lot_id=lot_id,
            interval=interval,
            address=address,
            location_kind=kinds.get(address.location_id) if address.location_id else None,
            source_time=own.source_time if own else source_time,
            lineage_key=str(own.lineage_key) if own else str(lot_id),
            unit_cost=int(basis.unit_cost) if basis else None,
            mrp=int(basis.mrp) if basis else None,
            description=str((identity or {}).get("description") or "")[:240],
            source_kind=basis.source_kind if basis else None,
        )
        (physical if address.boundary == "physical" else moving_out).append(item)

    encumbered: dict[tuple[uuid.UUID, str], list[tuple[int, int, int]]] = defaultdict(list)
    for lot_id, axis, stored, qty in EncumbranceLeg.objects.filter(
        tenant_id=tenant_id, recorded_at__lte=at, gate="active", lot_id__in=lot_ids
    ).values_list("lot_id", "axis", "portion", "qty"):
        lower, upper = bounds(stored)
        encumbered[(lot_id, axis)].append((lower, upper, 1 if qty > 0 else -1))
    holds: dict[uuid.UUID, list[Interval]] = {}
    reservations: dict[uuid.UUID, list[Interval]] = {}
    for (lot_id, axis), deltas in encumbered.items():
        target = holds if axis == EncumbranceLeg.Axis.HOLD else reservations
        target[lot_id] = present(deltas)
    return physical, moving_out, holds, reservations


# ---------------------------------------------------------------------------
# Stock rows (E173, E175, E176), in transit (E174) and summary (E172)
# ---------------------------------------------------------------------------


def limitations(query: StockQuery, codes: tuple[str, ...] = READ_LIMITATIONS) -> list[str]:
    """The R27 codes this read must declare: all of ``codes`` for a replay, none live.

    Judged from the query the server resolved, never from the timestamp a
    caller typed: ``_as_of`` clamps a future or equal-to-now value back to a
    live read, and that read has no limitation to declare.

    ``codes`` is the read's own fixed set - what *that endpoint* still takes
    from today - and never varies row by row or basis by basis within one read.
    """
    return list(codes) if query.replay_at is not None else []


def _visible_physical(
    query: StockQuery, portions: Iterable[engine.Portion]
) -> list[engine.Portion]:
    return [
        p
        for p in portions
        if query.visible(p.address.site_id, p.address.sku_id) and query.matches(p)
    ]


def _rows(
    query: StockQuery, snap: Snapshot, portions: list[engine.Portion], values: Iterable[str]
) -> list[dict[str, Any]]:
    rows = engine.build_stock_rows(
        portions,
        holds=snap.holds,
        reservations=snap.reservations,
        guards=snap.guards,
        purposes=snap.purposes,
        values=values,
        as_of=query.watermark,
    )
    declared = limitations(query)
    out = [
        {**_unbranded_limit(query, row), "limitations": list(declared)}
        for row in rows
        if query.keep_row(row)
    ]
    return _with_season(query, out)


def _season_labels(season_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    wanted = sorted({int(s) for s in season_ids if s})
    return {
        str(pk): {"code": code, "label": name, "unknown_historical": bool(unknown)}
        for pk, code, name, unknown in Season.objects.filter(pk__in=wanted).values_list(
            "pk", "code", "name", "historical_unknown"
        )
    }


def origin_seasons(origin_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Which season each origin's goods are in now, and which they started in.

    The one place that answer is worked out, because two screens ask it and they
    must not answer differently: the Stock screen labels a row with it, and the
    till has to send a counter the same season the shelf says (OPS-07). A season is
    frozen on the origin at PT approval, so it is read from there and never from
    whatever the master has been edited into since; an opening row whose real
    season was later established carries a correction, which is the season the
    goods are in *now*, with the one they opened under kept beside it (store and
    warehouse operations PRD §4 - a correction adds a fact, it never rewrites one).

    Per origin: ``now`` and ``original``, each ``{season_id, code, label,
    unknown_historical}``; ``original`` is ``None`` when nothing was corrected.
    """
    from ptmapper.goods_manifest_services import latest_season_corrections

    ids = sorted({str(o) for o in origin_ids if o})
    if not ids:
        return {}
    frozen = {
        str(pk): evidence or {}
        for pk, evidence in Origin.objects.filter(pk__in=ids).values_list("pk", "frozen_evidence")
    }
    row_ids = {
        uuid.UUID(str(e["manifest_row_id"])) for e in frozen.values() if e.get("manifest_row_id")
    }
    corrected = latest_season_corrections(row_ids) if row_ids else {}
    labels = _season_labels(
        [str(e.get("season_id") or "") for e in frozen.values()]
        + [str(s) for s in corrected.values()]
    )

    def described(season_id: str | None) -> dict[str, Any]:
        found = labels.get(season_id or "", {})
        return {
            "season_id": season_id,
            "code": found.get("code", ""),
            "label": found.get("label", ""),
            "unknown_historical": bool(found.get("unknown_historical")),
        }

    out: dict[str, dict[str, Any]] = {}
    for origin_id, evidence in frozen.items():
        original_id = str(evidence.get("season_id")) if evidence.get("season_id") else None
        manifest_row = evidence.get("manifest_row_id")
        now_id = original_id
        if manifest_row:
            moved = corrected.get(uuid.UUID(str(manifest_row)))
            now_id = str(moved) if moved else original_id
        out[origin_id] = {
            "now": described(now_id),
            "original": described(original_id) if now_id != original_id else None,
        }
    return out


def _with_season(query: StockQuery, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every row's season, as the goods stand now, with the original beside it.

    A row's season is frozen on its origin at PT approval, so it is read from
    there and not from anything today's master might have been edited into. An
    opening row whose cohort was later established carries an
    ``OpeningSeasonCorrection``: that is the season the goods are in *now*, and
    the season the row opened under stays visible as ``season_original`` (store
    and warehouse operations PRD §4 - a correction adds a fact, it never
    rewrites one).

    A row hidden from a brand-limited reader by R29 has no origin to read, so it
    carries no season either.

    Which season an origin is in is `origin_seasons`, shared with the till: the
    shelf and the counter must not disagree about what a piece is. This function
    is only the DTO's spelling of that answer and the season filter over it.
    """
    seasons = origin_seasons([str(row["origin_id"]) for row in rows if row.get("origin_id")])
    blank = {"season_id": None, "code": "", "label": "", "unknown_historical": False}

    def as_dto(described: dict[str, Any]) -> dict[str, Any]:
        return {
            "season_id": described["season_id"],
            "season_label": described["label"],
            "season_unknown_historical": described["unknown_historical"],
        }

    out: list[dict[str, Any]] = []
    for row in rows:
        found = seasons.get(str(row.get("origin_id") or ""), {})
        described_now = as_dto(found.get("now") or blank)
        if query.season is not None and not _season_matches(query.season, described_now):
            continue
        original = found.get("original")
        out.append(
            {
                **row,
                **described_now,
                "season_original": as_dto(original) if original else None,
            }
        )
    return out


def _season_matches(wanted: str, described: dict[str, Any]) -> bool:
    if wanted == UNKNOWN_HISTORICAL:
        return bool(described["season_unknown_historical"])
    return bool(described["season_id"] == wanted)


def _unbranded_limit(query: StockQuery, row: dict[str, Any]) -> dict[str, Any]:
    """Unidentified goods, seen only through a brand-limited grant, are just a quantity (R29).

    No brand can be assigned to them yet, so a brand reader sees them at a site its
    grant reaches - but not the typed description or origin, which may name another
    brand's goods. A grant not limited to a brand at that site sees the whole row.
    """
    if row["sku_id"] is not None or row["site_id"] is None:
        return row
    site = int(row["site_id"])
    if any(term.skus is None and site in term.sites for term in query.terms):
        return row
    return {**row, "description": "", "origin_id": None}


def on_hand_rows(query: StockQuery) -> list[dict[str, Any]]:
    """Every physical portion in scope: all conditions, holds and reservations."""
    snap = load(query)
    return _rows(query, snap, _visible_physical(query, snap.physical), query.money)


def availability_rows(query: StockQuery) -> list[dict[str, Any]]:
    """Each physical portion with its ATS/transferable quantity and every failing reason."""
    snap = load(query)
    return _rows(query, snap, _visible_physical(query, snap.physical), query.money)


def quarantine_rows(query: StockQuery) -> list[dict[str, Any]]:
    """Quarantine-kind locations, non-good condition, or the held part of anything else."""
    snap = load(query)
    selected: list[engine.Portion] = []
    for item in _visible_physical(query, snap.physical):
        if item.location_kind in QUARANTINE_KINDS or item.address.condition != "good":
            selected.append(item)
            continue
        for interval in ranges.intersect([item.interval], snap.holds.get(item.lot_id, [])):
            selected.append(replace(item, interval=interval))
    return _rows(query, snap, selected, query.money)


def transit_rows(query: StockQuery, snap: Snapshot | None = None) -> list[dict[str, Any]]:
    """Dispatched portions not yet checked in, by transfer, SKU, origin and condition."""
    snap = snap or load(query, transit=True)
    stamp = query.watermark.isoformat()
    declared = limitations(query)
    wanted = set(query.money)
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in snap.transit:
        address = item.address
        if address.transfer_id is None or query.location_id is not None:
            continue
        source, destination = snap.transfers[address.transfer_id]
        if not (
            query.visible(source, address.sku_id) or query.visible(destination, address.sku_id)
        ) or not query.matches(item):
            continue
        key = tuple(
            str(v) if v is not None else ""
            for v in (
                address.transfer_id,
                address.sku_id,
                address.origin_id or address.value_basis_origin_id,
                address.condition,
            )
        )
        row = groups.setdefault(
            key,
            {
                "site_id": str(source),
                "location_id": None,
                "sku_id": str(address.sku_id) if address.sku_id else None,
                "description": item.description,
                "origin_id": str(address.origin_id) if address.origin_id else None,
                "source_kind": engine.source_kind_label(item, address),
                "condition": address.condition,
                "physical_qty": 0,
                "valued_qty": 0,
                "accepted_qty": 0,
                "held_qty": 0,
                "reserved_qty": 0,
                "ats_qty": 0,
                "transferable_qty": 0,
                "eligibility_reasons": ["IN_TRANSIT"],
                "limitations": list(declared),
                "as_of": stamp,
                "record_contract": "goods-v1",
                "transfer_id": str(address.transfer_id),
                "source_site_id": str(source),
                "destination_site_id": str(destination),
                "_cost": 0,
                "_ticket": 0,
            },
        )
        size = ranges.length(item.interval)
        row["physical_qty"] += size
        if address.origin_id or address.value_basis_origin_id:
            row["valued_qty"] += size
        row["held_qty"] += ranges.total(
            ranges.intersect([item.interval], snap.holds.get(item.lot_id, []))
        )
        row["reserved_qty"] += ranges.total(
            ranges.intersect([item.interval], snap.reservations.get(item.lot_id, []))
        )
        # An unvalued piece adds nothing here; its row's value is reported unknown below.
        row["_cost"] += size * (item.unit_cost or 0)
        row["_ticket"] += size * (item.mrp or 0)
    out: list[dict[str, Any]] = []
    for _key, row in sorted(groups.items()):
        cost, ticket = row.pop("_cost"), row.pop("_ticket")
        known = row["valued_qty"] == row["physical_qty"]
        if engine.COST_VALUE in wanted:
            row[engine.COST_VALUE] = str(cost) if known else None
        if engine.TICKET_VALUE in wanted:
            row[engine.TICKET_VALUE] = str(ticket) if known else None
        if not query.keep_row(row):
            continue
        ends = {int(row["source_site_id"]), int(row["destination_site_id"])}
        if row["sku_id"] is None and not any(
            term.skus is None and ends & term.sites for term in query.terms
        ):
            row = {**row, "description": "", "origin_id": None}  # R29, as ``_unbranded_limit``
        out.append(row)
    return out


def summary(query: StockQuery) -> dict[str, Any]:
    """StockSummaryDTO: same-scope totals of the physical rows plus transit quantity."""
    snap = load(query, transit=True)
    rows = _rows(
        query,
        snap,
        _visible_physical(query, snap.physical),
        (engine.COST_VALUE, engine.TICKET_VALUE),
    )
    physical = sum(row["physical_qty"] for row in rows)
    valued = sum(row["valued_qty"] for row in rows)
    totals: dict[str, Any] = {
        "physical_qty": physical,
        "valued_qty": valued,
        "unvalued_qty": physical - valued,
        "ats_qty": sum(row["ats_qty"] for row in rows),
        "transferable_qty": sum(row["transferable_qty"] for row in rows),
        "transit_qty": sum(row["physical_qty"] for row in transit_rows(query, snap)),
    }
    value_key = {"cost": engine.COST_VALUE, "ticket": engine.TICKET_VALUE}.get(query.basis)
    if value_key is not None:
        # Unvalued pieces have an unknown value, not a zero one: the total exists only
        # when every piece is valued, and the valued pieces' value is kept separately.
        known = str(sum(int(row[value_key]) for row in rows if row[value_key] is not None))
        totals["value_paise"] = known if physical == valued else None
        totals["valued_value_paise"] = known
        totals["value_completeness"] = (
            "complete" if physical == valued else "partial" if valued else "unknown"
        )
    return {
        "scope": {
            "entity_id": None,
            "site_ids": [str(s) for s in sorted(query.site_ids)],
            "sbu_ids": [str(query.sbu_id)] if query.sbu_id else [],
            "brand_ids": [str(query.brand_id)] if query.brand_id else [],
            "scope_kind": query.scope_kind,
        },
        "basis": query.basis,
        "as_of": query.watermark.isoformat(),
        "limitations": limitations(query),
        "totals": totals,
    }


def rows_page(
    query: StockQuery, rows: list[dict[str, Any]], params: dict[str, str]
) -> dict[str, Any]:
    window, cursor = paginate(rows, params)
    return {"items": window, "next_cursor": cursor, "as_of": query.watermark.isoformat()}


# ---------------------------------------------------------------------------
# Journal entries (E171)
# ---------------------------------------------------------------------------


def _origin_prices(ids: Iterable[str | None]) -> dict[str, tuple[int, int]]:
    wanted = sorted({i for i in ids if i})
    return {
        str(pk): (int(cost), int(mrp))
        for pk, cost, mrp in Origin.objects.filter(pk__in=wanted).values_list(
            "pk", "unit_cost", "mrp"
        )
    }


def _basis_origin(*positions: dict[str, Any] | None) -> str | None:
    for position in positions:
        if position:
            found = position.get("origin_id") or position.get("value_basis_origin_id")
            if found:
                return str(found)
    return None


def _position_filters(query: StockQuery) -> list[Q]:
    """Leg-address conditions for the query's filters; a pair matches on either side."""
    scope = Q(pk__in=[])
    for term in query.terms:
        scope |= term.leg_q()
    wheres = [scope]
    if query.sku_id is not None:
        wheres.append(Q(position__sku_id=str(query.sku_id)))
    if query.origin_id is not None:
        origin = str(query.origin_id)
        wheres.append(Q(position__origin_id=origin) | Q(position__value_basis_origin_id=origin))
    if query.location_id is not None:
        wheres.append(Q(position__location_id=str(query.location_id)))
    if query.condition is not None:
        wheres.append(Q(position__condition=query.condition))
    if query.brand_skus is not None:
        wheres.append(Q(position__sku_id__in=sorted(str(s) for s in query.brand_skus)))
    return wheres


def _selected_pairs(query: StockQuery, legs: Any) -> Any:
    """Destination legs (one per pair) whose pair touches scope and every filter."""
    partner = legs.filter(
        side="source", batch_id=OuterRef("batch_id"), pair_key=OuterRef("pair_key")
    )
    selected = legs.filter(side="destination")
    for where in _position_filters(query):
        selected = selected.filter(where | Exists(partner.filter(where)))
    if query.replay_at is not None:
        selected = selected.filter(recorded_at__lte=query.replay_at)
    if query.state is not None:
        selected = selected.filter(batch__posting_kind=query.state)
    if query.text:
        selected = selected.filter(batch__version__document__official_number__icontains=query.text)
    return selected


def entries_page(query: StockQuery, params: dict[str, str]) -> dict[str, Any]:
    """Page<JournalDTO>: quantity pairs touching scope, newest first, paged in the database."""
    offset = decode_cursor(params.get("cursor"))
    limit = page_limit(params)
    legs = QuantityLeg.objects.filter(tenant_id=query.tenant_id)
    window = list(
        _selected_pairs(query, legs)
        .select_related("batch", "batch__version", "batch__version__document")
        .order_by("-recorded_at", "batch_id", "pair_key")[offset : offset + limit + 1]
    )
    more = len(window) > limit
    window = window[:limit]
    sources = {
        (leg.batch_id, leg.pair_key): leg
        for leg in legs.filter(
            side="source",
            batch_id__in={leg.batch_id for leg in window},
            pair_key__in={leg.pair_key for leg in window},
        )
    }
    pairs = [(leg, sources.get((leg.batch_id, leg.pair_key))) for leg in window]
    prices = _origin_prices(
        _basis_origin(leg.position, source.position if source else None) for leg, source in pairs
    )
    declared = limitations(query, JOURNAL_LIMITATIONS)
    items: list[dict[str, Any]] = []
    for leg, source in pairs:
        version = leg.batch.version
        item: dict[str, Any] = {
            "limitations": list(declared),
            "document_id": str(version.document_id),
            "number": version.document.official_number,
            "version": version.version,
            "event_id": str(leg.batch_id),
            "kind": leg.batch.posting_kind,
            "source": source.position if source else None,
            "destination": leg.position,
            "qty": int(leg.qty),
            "event_at": leg.event_at.isoformat(),
            "recorded_at": leg.recorded_at.isoformat(),
        }
        if query.basis in ("cost", "ticket"):
            price = prices.get(_basis_origin(leg.position, item["source"]) or "")
            item["value_paise"] = (
                None
                if price is None
                else str(int(leg.qty) * price[0 if query.basis == "cost" else 1])
            )
        items.append(item)
    return {
        "items": items,
        "next_cursor": encode_cursor(offset + limit) if more else None,
        "as_of": query.watermark.isoformat(),
    }


# ---------------------------------------------------------------------------
# Origin journey (E181)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    recorded_at: datetime
    rank: int
    event_id: str
    event_kind: str
    version: Any
    source_site_id: int | None
    destination_site_id: int | None
    qty: int
    source_evidence_ref: str
    event_at: datetime
    #: A reference for a step whose record is not a kernel document with a number
    #: of its own - an arrival, for instance. Ignored when ``version`` is set.
    reference: str | None = None


def _overlap(covered: dict[uuid.UUID, list[Interval]]) -> Q:
    where = Q(pk__in=[])
    for lot_id, intervals in covered.items():
        for lower, upper in intervals:
            where |= Q(lot_id=lot_id, portion__overlap=portion(lower, upper))
    return where


def _covered_qty(covered: dict[uuid.UUID, list[Interval]], lot_id: uuid.UUID, stored: Any) -> int:
    return ranges.total(ranges.intersect([bounds(stored)], covered.get(lot_id, [])))


def journey_page(
    access: AccessContext, origin_id: uuid.UUID, params: dict[str, str]
) -> dict[str, Any]:
    """Page<JourneyDTO>: the origin's evidence and every authorised event on its portions.

    The original PT quantity is reported as recorded; portion offsets are never
    shown, so nothing here reads as a serial history of an individual piece.
    """
    offset = decode_cursor(params.get("cursor"))
    limit = page_limit(params, default=50, maximum=100)
    require_reader(access)
    origin = (
        Origin.objects.select_related(
            "official_line__version__document",
            "value_damage_disposition__document",
            "sku__style",
        )
        .filter(tenant_id=access.tenant_id, pk=origin_id)
        .first()
    )
    brand_id = origin.sku.style.brand_id if origin is not None else None
    if origin is None or not access.can(READ, site_id=origin.site_id, brand_id=brand_id):
        raise Refusal("NOT_FOUND", "That origin was not found.")
    sites = tenant_sites(access)
    granted = access.site_reach(READ)
    terms = _grant_terms(access, sites if granted is None else sites & frozenset(granted))
    sku_id = origin.sku_id

    def visible(*site_ids: int | None) -> bool:
        known = [s for s in site_ids if s is not None]
        return not known or any(term.covers(s, sku_id) for term in terms for s in known)

    official_version = origin.official_line.version if origin.official_line is not None else None
    disposition = origin.value_damage_disposition
    steps: list[_Step] = [
        _Step(
            origin.recorded_at,
            0,
            str(origin.pk),
            "value_damage_approved" if disposition is not None else "origin_created",
            official_version,
            None,
            origin.site_id,
            int(origin.opening_qty),
            (
                f"disposition:{disposition.pk}"
                if disposition is not None
                else f"official_line:{origin.official_line_id}"
            ),
            origin.event_at,
            disposition.document.official_number if disposition is not None else None,
        )
    ]
    covered: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    for event in CoverageEvent.objects.filter(
        tenant_id=access.tenant_id, origin_id=origin.pk
    ).select_related("pt_version__document"):
        interval = bounds(event.portion)
        cover = event.effect == CoverageEvent.Effect.COVER
        if cover:
            covered[event.lot_id].append(interval)
        if visible(event.site_id):
            steps.append(
                _Step(
                    event.recorded_at,
                    1,
                    str(event.pk),
                    f"coverage_{event.effect}",
                    event.pt_version,
                    None if cover else event.site_id,
                    event.site_id if cover else None,
                    ranges.length(interval),
                    f"coverage_event:{event.pk}",
                    event.event_at,
                )
            )
    if covered:
        steps.extend(_acceptance_steps(access, covered, visible))
        steps.extend(_journal_steps(access, covered, visible))
        steps.extend(_encumbrance_steps(access, covered, visible))
    steps.extend(_receipt_steps(origin, visible))
    # Deterministic order, and a meaningful one when two events share a recorded
    # time: the business time they happened at, then their own stable evidence
    # reference, before the event id is ever reached (ticket 07's recorded
    # sort-tie finding).
    steps.sort(key=lambda s: (s.recorded_at, s.rank, s.event_at, s.source_evidence_ref, s.event_id))
    window = steps[offset : offset + limit]
    show_value = "cost" in access.field_grants(
        site_id=origin.site_id, brand_id=brand_id, actions={READ}
    )
    items: list[dict[str, Any]] = []
    for step in window:
        version = step.version
        item: dict[str, Any] = {
            "event_id": step.event_id,
            "event_kind": step.event_kind,
            "document_id": str(version.document_id) if version is not None else None,
            "number": version.document.official_number if version is not None else step.reference,
            "version": version.version if version is not None else None,
            "source_site_id": str(step.source_site_id) if step.source_site_id else None,
            "destination_site_id": str(step.destination_site_id)
            if step.destination_site_id
            else None,
            "qty": step.qty,
            "source_evidence_ref": step.source_evidence_ref,
            "event_at": step.event_at.isoformat(),
            "recorded_at": step.recorded_at.isoformat(),
        }
        if show_value:
            item["value_paise"] = str(step.qty * int(origin.unit_cost))
        items.append(item)
    return {
        "items": items,
        "next_cursor": encode_cursor(offset + limit) if offset + limit < len(steps) else None,
        "as_of": timezone.now().isoformat(),
    }


def _receipt_steps(origin: Any, visible: Callable[..., bool]) -> list[_Step]:
    """The booking and the arrival behind a receipt origin, when there are any.

    Change PRD §4.1 outcome 1 asks the origin journey to link the whole ordinary
    path, and it always could reach the GRN through the posting that opened the
    custody - but it stopped there, so nothing on screen said which delivery
    these pieces turned up on or which order they answered. This walks the one
    chain that exists: the covering receipt PT, its GRN, that GRN's arrival, and
    the booking the arrival names. Opening or transfer origins have no such
    chain and get no extra steps.

    Read through ``apps.get_model`` rather than an import: ``inbound`` and
    ``vendors`` both already read this package, and a read should not turn that
    into a cycle between them.
    """
    if origin.official_line is None:
        return []
    version = origin.official_line.version
    pt = (
        apps.get_model("ptmapper", "GoodsPt")
        .objects.filter(document_id=version.document_id)
        .select_related("grn__arrival")
        .first()
    )
    arrival = getattr(getattr(pt, "grn", None), "arrival", None)
    if arrival is None or not visible(arrival.site_id):
        return []
    steps: list[_Step] = []
    booking_head = (
        apps.get_model("core", "DocumentHead")
        .objects.select_related("live_version__document")
        .filter(
            document_id=apps.get_model("vendors", "GoodsBooking")
            .objects.filter(pk=arrival.booking_id)
            .values_list("document_id", flat=True)[:1]
        )
        .first()
        if arrival.booking_id
        else None
    )
    booking_version = getattr(booking_head, "live_version", None)
    if booking_version is not None:
        steps.append(
            _Step(
                booking_version.recorded_at,
                -2,
                str(booking_version.pk),
                "booking_confirmed",
                booking_version,
                None,
                arrival.site_id,
                int(origin.opening_qty),
                f"booking:{booking_version.document_id}",
                booking_version.recorded_at,
            )
        )
    steps.append(
        _Step(
            arrival.recorded_at,
            -1,
            str(arrival.pk),
            "goods_arrived",
            None,
            None,
            arrival.site_id,
            int(origin.opening_qty),
            f"arrival:{arrival.pk}",
            arrival.actual_arrival_at,
            # An arrival is not a kernel document, so it has no official number.
            # Its invoice is what a receiver recognises it by; without one, its
            # transporter reference is, and it is honestly unreferenced with
            # neither rather than carrying an invented number.
            reference=arrival.invoice_number or arrival.transporter_ref or None,
        )
    )
    return steps


def _acceptance_steps(
    access: AccessContext,
    covered: dict[uuid.UUID, list[Interval]],
    visible: Callable[..., bool],
) -> list[_Step]:
    steps: list[_Step] = []
    for event in (
        AcceptanceEvent.objects.filter(tenant_id=access.tenant_id)
        .filter(_overlap(covered))
        .select_related("session__source_version__document")
    ):
        qty = _covered_qty(covered, event.lot_id, event.portion)
        if not qty or not visible(event.site_id):
            continue
        steps.append(
            _Step(
                event.recorded_at,
                2,
                str(event.pk),
                f"acceptance_{event.outcome}",
                event.session.source_version,
                None,
                event.site_id,
                qty,
                event.label_evidence_ref or f"acceptance_event:{event.pk}",
                event.event_at,
            )
        )
    return steps


def _journal_steps(
    access: AccessContext,
    covered: dict[uuid.UUID, list[Interval]],
    visible: Callable[..., bool],
) -> list[_Step]:
    legs = QuantityLeg.objects.filter(tenant_id=access.tenant_id).filter(_overlap(covered))
    destinations = list(legs.filter(side="destination").select_related("batch__version__document"))
    sources = {(leg.batch_id, leg.pair_key): leg for leg in legs.filter(side="source")}
    grouped: dict[tuple[uuid.UUID, int | None, int | None], list[Any]] = {}
    for leg in destinations:
        source = sources.get((leg.batch_id, leg.pair_key))
        from_site = source.position.get("site_id") if source else None
        to_site = leg.position.get("site_id")
        qty = _covered_qty(covered, leg.lot_id, leg.portion)
        if not qty or not visible(from_site, to_site):
            continue
        key = (leg.batch_id, from_site, to_site)
        if key in grouped:
            grouped[key][1] += qty
        else:
            grouped[key] = [leg, qty]
    return [
        _Step(
            leg.recorded_at,
            3,
            str(leg.batch_id),
            f"posting_{leg.batch.posting_kind}",
            leg.batch.version,
            from_site,
            to_site,
            qty,
            f"journal_batch:{leg.batch_id}",
            leg.event_at,
        )
        for (_batch, from_site, to_site), (leg, qty) in grouped.items()
    ]


def _encumbrance_steps(
    access: AccessContext,
    covered: dict[uuid.UUID, list[Interval]],
    visible: Callable[..., bool],
) -> list[_Step]:
    steps: list[_Step] = []
    for hold in (
        HoldEvent.objects.filter(tenant_id=access.tenant_id)
        .filter(_overlap(covered))
        .select_related("source_version__document")
    ):
        qty = _covered_qty(covered, hold.lot_id, hold.portion)
        if not qty or not visible(hold.site_id):
            continue
        placed = hold.effect == HoldEvent.Effect.PLACE
        steps.append(
            _Step(
                hold.recorded_at,
                4,
                str(hold.pk),
                f"hold_{hold.effect}",
                hold.source_version,
                None if placed else hold.site_id,
                hold.site_id if placed else None,
                qty,
                f"hold_event:{hold.pk}",
                hold.event_at,
            )
        )
    for reservation in (
        ReservationEvent.objects.filter(tenant_id=access.tenant_id)
        .filter(_overlap(covered))
        .select_related("transfer_version__document")
    ):
        qty = _covered_qty(covered, reservation.lot_id, reservation.portion)
        if not qty or not visible(reservation.site_id):
            continue
        steps.append(
            _Step(
                reservation.recorded_at,
                5,
                str(reservation.pk),
                f"reservation_{reservation.effect}",
                reservation.transfer_version,
                reservation.site_id,
                None,
                qty,
                f"reservation_event:{reservation.pk}",
                reservation.event_at,
            )
        )
    return steps
