"""What a goods-v1 store's counter may sell, and how it is described (OPS-07, PRD §8).

The legacy till reads one projection table: a barcode, a quantity, and the
quantity is whatever the ledger last netted to. A goods-v1 store has no such
table and is not going to get one - the whole point of the contract is that a
balance is never copied anywhere. So this module answers the counter's two
questions from the goods records themselves:

* **What may I sell?** Accepted, good, officially valued pieces standing in a
  selling location at this store, with nothing held over them and nothing
  reserved out from under them. That is `engine.eligible_portions(..., "sell")`,
  which is the same sentence the Stock screen's availability column and a
  transfer's own allocation use. It is deliberately not re-stated here: a second
  spelling of "sellable" is a second thing that can drift, and the drift would be
  a counter selling a quarantined piece.
* **What is this piece?** Its SKU's own identity, and the season, MRP and HSN
  frozen on the origin that valued it - never today's master, which may have
  moved since the goods came in.

**No cost leaves this module.** The origin carries `unit_cost` beside the `mrp`
this reads, so every field that travels is named one at a time and the cost is
simply never one of them (H2, PRD §8's "cost and margin never reach the till").
The test walks the finished payload for a cost-shaped key.

**A barcode is an alias, not an identity.** The till scans a code and the code
resolves to a SKU; two SKUs behind one code is an ambiguity a counter cannot
settle standing at a customer, so such a code is simply not offered here.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from masters.goods_identity_models import GovernanceState, SkuAlias
from masters.goods_models import SiteGuard
from masters.models import Store
from stockledger import goods_engine as engine
from stockledger import ranges
from stockledger.goods_models import Origin, Position
from stockledger.goods_reads import origin_seasons

#: The alias type a counter's scanner produces. A vendor code or a generated
#: internal code is not what is printed on the tag at the till.
BARCODE = SkuAlias.AliasType.BARCODE
EFFECTIVE = GovernanceState.EFFECTIVE


def is_goods_site(store: Store) -> bool:
    """Does this store keep its stock under the goods-v1 contract?"""
    return SiteGuard.objects.filter(
        site_id=store.pk, stock_contract=SiteGuard.StockContract.GOODS_V1
    ).exists()


@dataclass(frozen=True)
class Piece:
    """One (barcode, season) a counter can scan, with what the books say about it."""

    barcode: str
    sku_id: uuid.UUID
    season: str
    season_unknown_historical: bool
    #: The SKU's identity as the masters describe it now: design, brand, size, colour.
    dims: dict[str, str]
    #: Frozen on the newest accepted origin of this (barcode, season) at this store.
    hsn: str
    mrp_paise: int | None
    #: The newest accepted origin the HSN and MRP were read from (ticket 12 names
    #: its PT when the HSN is missing, so the PT can be corrected).
    origin_id: uuid.UUID | None = None
    brand_id: int | None = None
    no_discount: bool = False


def barcode_aliases(store: Store, at: datetime) -> dict[uuid.UUID, str]:
    """The scannable code for each SKU at this store, where exactly one code answers.

    Site-scoped aliases and unscoped ones together, plus exact labels of goods
    this store physically accepted. A transfer does not rewrite the source
    origin or turn a source-only alias into a global alias: the immutable origin
    line binds that accepted piece's label to its SKU. Its source alias must
    still be governed and effective. A SKU under two live codes, or a code over
    two SKUs, is left out rather than guessed at.
    """
    governed = (
        SkuAlias.objects.filter(tenant_id=store.tenant_id, sku__tenant_id=store.tenant_id, sku__style__tenant_id=store.tenant_id, sku__style__brand__tenant_id=store.tenant_id, alias_type=BARCODE, effective_from__lte=at)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))
        # Generally effective identity only: a proposal still waiting for its
        # approver, and a code somebody has retired, are both things a counter
        # must not be billing under.
        .filter(
            governance_state=EFFECTIVE,
            sku__governance_state=EFFECTIVE,
            sku__style__governance_state=EFFECTIVE,
        )
    )
    rows = list(governed.filter(Q(site__isnull=True) | Q(site_id=store.pk)).values_list("sku_id", "value"))
    received = _received_label_bindings(store)
    if received:
        # An origin cannot revive an expired/retired alias or borrow an unrelated
        # label from another site. Compare all three exact source cells.
        trusted: set[tuple[uuid.UUID, str, int | None]] = set(
            governed.filter(sku_id__in={sku for sku, _value, _site in received})
            .values_list("sku_id", "value", "site_id"))
        rows.extend((sku, value) for sku, value, site in received
                    if (sku, value, site) in trusted or (sku, value, None) in trusted)
    by_sku: dict[uuid.UUID, set[str]] = defaultdict(set)
    by_value: dict[str, set[uuid.UUID]] = defaultdict(set)
    for sku_id, value in rows:
        by_sku[sku_id].add(value)
        by_value[value].add(sku_id)
    return {
        sku_id: next(iter(values))
        for sku_id, values in by_sku.items()
        if len(values) == 1 and len(by_value[next(iter(values))]) == 1
    }


def _received_label_bindings(store: Store) -> set[tuple[uuid.UUID, str, int]]:
    """Labels of exact valued goods accepted here; no tenant-wide alias search.

    Current positions require the destination's own acceptance. Exhausted sold
    pieces retain their originating identity for a later exchange, using only
    this store's immutable sale allocations. Values never leave this reader.
    """
    from sell.models import SaleLine

    origins = set(Position.objects.filter(
        tenant_id=store.tenant_id, site=store, site__tenant_id=store.tenant_id,
        boundary="physical", origin__tenant_id=store.tenant_id,
        accepted_event__tenant_id=store.tenant_id, accepted_event__site=store,
        accepted_event__outcome="accepted_good",
    ).values_list("origin_id", flat=True))
    for allocations in SaleLine.objects.filter(
        sale__store__tenant_id=store.tenant_id, sale__store=store,
    ).values_list("goods_allocations", flat=True):
        for allocated in allocations or []:
            if not isinstance(allocated, dict) or not allocated.get("origin_id"):
                continue
            try:
                origins.add(uuid.UUID(str(allocated["origin_id"])))
            except (TypeError, ValueError):
                continue
    if not origins:
        return set()
    received: set[tuple[uuid.UUID, str, int]] = set()
    for sku_id, source_site, payload in Origin.objects.filter(
        tenant_id=store.tenant_id, pk__in=origins, site__tenant_id=store.tenant_id,
        official_line__tenant_id=store.tenant_id,
        official_line__version__tenant_id=store.tenant_id,
        official_line__version__document__tenant_id=store.tenant_id,
        sku__tenant_id=store.tenant_id,
        sku__style__tenant_id=store.tenant_id,
        sku__style__brand__tenant_id=store.tenant_id,
    ).values_list("sku_id", "site_id", "official_line__payload"):
        body = payload if isinstance(payload, dict) else {}
        label = body.get("alias_as_used")
        if (str(body.get("sku_id")) == str(sku_id) and isinstance(label, str)
                and 1 <= len(label) <= 128):
            received.add((sku_id, label, source_site))
    return received


def sku_for_barcode(store: Store, barcode: str, at: datetime | None = None) -> uuid.UUID | None:
    """The one SKU this code names at this store, or nothing when it names none or several."""
    moment = at or timezone.now()
    found = [
        sku_id
        for sku_id, value in barcode_aliases(store, moment).items()
        if value == barcode.strip()
    ]
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class Shelf:
    """One reading of a goods store's shelf: what is sellable, and what it all is."""

    pieces: list[Piece]
    #: Sellable quantity per (barcode, season).
    quantities: dict[tuple[str, str], int]


def read_shelf(store: Store, at: datetime | None = None) -> Shelf:
    """Everything the counter may sell here, plus everything it has ever held.

    Two passes over the same store for two different questions, and they are
    different on purpose. *Quantity* is only ever the sellable portions. *Identity*
    is every accepted origin this store holds a position of, at nought as well as
    at ten - a piece at nought can walk back in as an exchange, and the till still
    has to name and price it when it does.
    """
    moment = at or timezone.now()
    aliases = barcode_aliases(store, moment)
    if not aliases:
        return Shelf(pieces=[], quantities={})

    sellable = engine.eligible_portions_for_skus(store.pk, sorted(aliases, key=str), purpose="sell")
    origin_ids = {
        str(origin_id)
        for origin_id in Position.objects.filter(
            tenant_id=store.tenant_id, site_id=store.pk, boundary="physical", sku_id__in=list(aliases)
        ).values_list("origin_id", flat=True)
        if origin_id
    } | {str(item.address.origin_id) for item in sellable if item.address.origin_id}
    # An exhausted opening origin remains part of this store's item book. The
    # last sale removes its physical position, but may neither erase scan/return
    # identity nor change the commercial revision during that sale's commit.
    origin_ids.update(str(pk) for pk in Origin.objects.filter(
        tenant_id=store.tenant_id, site=store, sku_id__in=list(aliases)
    ).values_list("pk", flat=True))
    from sell.models import SaleLine
    for allocated in SaleLine.objects.filter(sale__store=store).values_list("goods_allocations", flat=True):
        origin_ids.update(str(row["origin_id"]) for row in (allocated or []) if isinstance(row, dict) and row.get("origin_id"))
    origins = {
        str(row.pk): row for row in Origin.objects.filter(tenant_id=store.tenant_id, pk__in=sorted(origin_ids)).order_by("pk")
    }
    seasons = origin_seasons(origins)

    def season_of(origin_id: str | None) -> tuple[str, bool]:
        described = (seasons.get(str(origin_id or "")) or {}).get("now") or {}
        return str(described.get("code") or ""), bool(described.get("unknown_historical"))

    quantities: dict[tuple[str, str], int] = defaultdict(int)
    for item in sellable:
        origin_id = item.address.origin_id or item.address.value_basis_origin_id
        barcode = aliases.get(item.address.sku_id) if item.address.sku_id else None
        if barcode is None:
            continue
        season, _unknown = season_of(str(origin_id) if origin_id else None)
        quantities[(barcode, season)] += ranges.length(item.interval)

    return Shelf(
        pieces=_pieces(aliases, origins, season_of),
        quantities=dict(quantities),
    )


def _pieces(
    aliases: dict[uuid.UUID, str],
    origins: dict[str, Origin],
    season_of: Any,
) -> list[Piece]:
    """One row per (barcode, season) this store holds goods of, priced from the newest origin.

    Newest, not oldest: the ticket price on the tag is the one the most recent
    buying of that piece carried, and that is what a customer is holding. The
    *cost* of an older lot is untouched by this - it lives on its own origin and
    is what the sale consumes at, which is why nothing here goes near it.
    """
    from core.tenancy import require_tenant_id
    from masters.goods_identity_services import candidates_for

    grouped: dict[tuple[str, str], list[Origin]] = defaultdict(list)
    unknown_season: dict[tuple[str, str], bool] = {}
    for origin in origins.values():
        barcode = aliases.get(origin.sku_id)
        if barcode is None:
            continue
        season, unknown = season_of(str(origin.pk))
        key = (barcode, season)
        grouped[key].append(origin)
        unknown_season[key] = unknown
    if not grouped:
        return []
    described = {
        row["sku_id"]: row
        for row in candidates_for(
            require_tenant_id(), {str(o.sku_id) for group in grouped.values() for o in group}
        )
    }
    from masters.goods_identity_models import ProductSku
    from stockledger.goods_descriptions import origin_item_names
    no_discount = set(ProductSku.objects.filter(tenant_id=require_tenant_id(), pk__in=[o.sku_id for group in grouped.values() for o in group], no_discount=True).values_list("pk", flat=True))
    item_names = origin_item_names(require_tenant_id(), origins)
    out: list[Piece] = []
    for (barcode, season), group in sorted(grouped.items()):
        newest = max(group, key=lambda o: (o.source_time, str(o.pk)))
        identity = described.get(str(newest.sku_id), {})
        out.append(
            Piece(
                barcode=barcode,
                sku_id=newest.sku_id,
                brand_id=identity.get("brand_id"),
                season=season,
                season_unknown_historical=unknown_season[(barcode, season)],
                dims={
                    "design": str(identity.get("style") or ""),
                    "brand": str(identity.get("brand") or ""),
                    "item": item_names.get(str(newest.pk)) or str(identity.get("grade") or ""),
                    "size": str(identity.get("size") or ""),
                    "color": str(identity.get("colour") or ""),
                },
                hsn=str((newest.frozen_evidence or {}).get("hsn") or ""),
                # Never nought: an origin with no MRP is a piece a human must
                # price, and a zero here would bill a garment at nothing.
                mrp_paise=int(newest.mrp) or None,
                origin_id=newest.pk,
                no_discount=newest.sku_id in no_discount,
            )
        )
    return out
