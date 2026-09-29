"""Stock ageing measured against the season (store operations ticket 33, ST-INV-2).

Age is not flat days. What makes a piece old depends on its season:

* **In season:** it is flagged once the item has had no sale at this store for
  the policy's days (``AlertPolicy`` for ``stock_ageing``, 90 to start). The count
  runs from the later of the item's last sale here and the day this piece arrived
  here - a piece that came in last week is not idle because of an old sale.
* **Season ended:** it counts as aged from the day its season ended
  (``Season.ended_on``, a recorded fact), whatever has sold since.
* **Unknown historical season:** its own group. Nobody knows its season, so
  nothing is guessed about how old that makes it.
* **Season unclear:** a closed season whose end date nobody has recorded, or a
  piece with no season at all. Also never guessed; the page asks for the date.

Each piece also shows the days since it first arrived in the company and the
days at this store. The first comes from its origin: a receipt's actual arrival,
or, for opening stock, the older arrival the manifest recorded - and when the
manifest recorded none, it is unknown, never the cutover date passed off as one.
The second is when this store accepted it (a transfer keeps the piece's origin
and gets a new acceptance at the destination).

Only goods-v1 stores have origins and acceptance, so only they can be aged; a
legacy store is reported as such. A read: nothing here writes, and no cost or
margin is in any row.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from masters.models import Season, Store

IN_SEASON = "in_season"
SEASON_ENDED = "season_ended"
UNKNOWN_SEASON = "unknown_season"
SEASON_UNCLEAR = "season_unclear"
#: In the order the page lists them.
GROUPS: tuple[str, ...] = (IN_SEASON, SEASON_ENDED, UNKNOWN_SEASON, SEASON_UNCLEAR)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeasonFacts:
    """What the season master says about one season, read once per request."""

    code: str
    label: str
    status: str
    ended_on: date | None
    unknown_historical: bool = False


@dataclass(frozen=True)
class Verdict:
    group: str
    #: The day the age is counted from; ``None`` where no age can be known.
    since: date | None
    age_days: int | None
    aged: bool


def classify(
    season: SeasonFacts | None,
    *,
    today: date,
    arrived_here: date,
    last_sale: date | None,
    idle_days: int | None,
) -> Verdict:
    """Which group a piece is in, how old that makes it, and whether it is aged."""
    if season is None:
        return Verdict(SEASON_UNCLEAR, None, None, False)
    if season.unknown_historical:
        return Verdict(UNKNOWN_SEASON, None, None, False)
    if season.ended_on is not None and season.ended_on <= today:
        return Verdict(SEASON_ENDED, season.ended_on, (today - season.ended_on).days, True)
    if season.status == Season.Status.CLOSED:
        # Closed, but nobody recorded when: the day it ended is not guessed.
        return Verdict(SEASON_UNCLEAR, None, None, False)
    since = max(arrived_here, last_sale) if last_sale else arrived_here
    age = max(0, (today - since).days)
    return Verdict(IN_SEASON, since, age, idle_days is not None and age >= idle_days)


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def idle_days_policy() -> int | None:
    """The in-season days with no sale that flag a piece, or ``None`` when unset.

    The alert policy's number, so the page and the alert can never disagree.
    """
    from alerts.models import AlertKind, AlertPolicy

    policy = AlertPolicy.objects.filter(kind=AlertKind.STOCK_AGEING).first()
    return min(policy.thresholds_days) if policy and policy.thresholds_days else None


@dataclass(frozen=True)
class AgeingRow:
    """Pieces of one origin that arrived at this store on one day."""

    #: The receipt or opening line the pieces came in on; with ``arrived_here_on``
    #: it names the row.
    origin_id: uuid.UUID
    sku_id: uuid.UUID
    barcode: str
    brand: str
    brand_id: int | None
    item: str
    design: str
    size: str
    colour: str
    season_code: str
    season_label: str
    group: str
    qty: int
    #: When the piece first arrived in the company, if known.
    first_arrived_on: date | None
    days_in_company: int | None
    #: Opening stock whose older arrival is unknown: it was here before this day.
    in_company_before: date | None
    arrived_here_on: date
    days_here: int
    last_sale_on: date | None
    since: date | None
    age_days: int | None
    aged: bool

    def as_json(self) -> dict[str, Any]:
        def day(value: date | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "origin_id": str(self.origin_id),
            "sku_id": str(self.sku_id),
            "barcode": self.barcode,
            "brand": self.brand,
            "brand_id": self.brand_id,
            "item": self.item,
            "design": self.design,
            "size": self.size,
            "colour": self.colour,
            "season_code": self.season_code,
            "season_label": self.season_label,
            "group": self.group,
            "qty": self.qty,
            "first_arrived_on": day(self.first_arrived_on),
            "days_in_company": self.days_in_company,
            "in_company_before": day(self.in_company_before),
            "arrived_here_on": day(self.arrived_here_on),
            "days_here": self.days_here,
            "last_sale_on": day(self.last_sale_on),
            "since": day(self.since),
            "age_days": self.age_days,
            "aged": self.aged,
        }


@dataclass(frozen=True)
class GroupTotal:
    group: str
    qty: int
    aged_qty: int
    items: int
    #: The oldest age in the group, where ages are known.
    oldest_days: int | None

    def as_json(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "qty": self.qty,
            "aged_qty": self.aged_qty,
            "items": self.items,
            "oldest_days": self.oldest_days,
        }


@dataclass(frozen=True)
class StoreAgeing:
    store: Store
    today: date
    idle_days: int | None
    #: False for a legacy store: it keeps no origins, so nothing can be aged.
    goods_records: bool
    rows: list[AgeingRow] = field(default_factory=list)

    def totals(self) -> list[GroupTotal]:
        out = []
        for group in GROUPS:
            rows = [row for row in self.rows if row.group == group]
            ages = [row.age_days for row in rows if row.age_days is not None]
            out.append(
                GroupTotal(
                    group=group,
                    qty=sum(row.qty for row in rows),
                    aged_qty=sum(row.qty for row in rows if row.aged),
                    items=len({row.sku_id for row in rows}),
                    oldest_days=max(ages) if ages else None,
                )
            )
        return out


def _local_day(moment: datetime) -> date:
    return timezone.localtime(moment).date()


def _older_arrival(evidence: dict[str, Any]) -> date | None:
    """The older arrival an opening row recorded, or ``None`` - never invented."""
    raw = evidence.get("older_origin_at")
    if not raw:
        return None
    text = str(raw)
    try:
        moment = parse_datetime(text)
        if moment is not None:
            return _local_day(moment) if timezone.is_aware(moment) else moment.date()
        return parse_date(text)
    except ValueError:
        # Well-formed but not a real date ("2026-02-30"): unknown, never a guess -
        # and one bad row must not stop the store's page or the daily alert job.
        return None


def season_facts(season_ids: set[str]) -> dict[str, SeasonFacts]:
    return {
        str(row.pk): SeasonFacts(
            code=row.code,
            label=row.name,
            status=row.status,
            ended_on=row.ended_on,
            unknown_historical=row.historical_unknown,
        )
        for row in Season.objects.filter(pk__in=sorted(season_ids))
    }


@dataclass(frozen=True)
class ItemBarcodes:
    """The barcodes of the items a store holds, for naming them and finding their sales."""

    #: Every code each item has ever had here (site-scoped or unscoped, retired
    #: too), so a sale billed under an older code still counts. A code that has
    #: ever named two items is left out: a bill under it cannot say which sold.
    item_of: dict[str, uuid.UUID]
    #: The one code on the tag now, where exactly one is live.
    shown: dict[uuid.UUID, str]


def item_barcodes(store: Store, sku_ids: set[uuid.UUID], at: datetime) -> ItemBarcodes:
    from django.db.models import Q

    from masters.goods_identity_models import GovernanceState, SkuAlias

    here = SkuAlias.objects.filter(alias_type=SkuAlias.AliasType.BARCODE).filter(
        Q(site__isnull=True) | Q(site_id=store.pk)
    )
    held = list(
        here.filter(sku_id__in=sorted(sku_ids, key=str)).values_list(
            "sku_id", "value", "effective_from", "effective_to", "governance_state"
        )
    )
    owners: dict[str, set[uuid.UUID]] = defaultdict(set)
    for sku_id, value in here.filter(value__in=sorted({row[1] for row in held})).values_list(
        "sku_id", "value"
    ):
        owners[value].add(sku_id)
    item_of = {value: next(iter(skus)) for value, skus in owners.items() if len(skus) == 1}
    live: dict[uuid.UUID, set[str]] = defaultdict(set)
    for sku_id, value, starts, ends, state in held:
        if (
            value in item_of
            and state == GovernanceState.EFFECTIVE
            and starts <= at
            and (ends is None or ends > at)
        ):
            live[sku_id].add(value)
    shown = {sku_id: next(iter(codes)) for sku_id, codes in live.items() if len(codes) == 1}
    return ItemBarcodes(item_of=item_of, shown=shown)


def last_sales(store: Store, item_of: dict[str, uuid.UUID], today: date) -> dict[uuid.UUID, date]:
    """The day each item last sold at this store, up to and including ``today``.

    A bill line names the barcode the counter scanned; ``item_of`` says which item
    that code names. A cancelled bill sold nothing.
    """
    from django.db.models import Max

    from core.documents import DocStatus
    from sell.models import SaleLine

    day_after = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min))
    rows = (
        SaleLine.objects.filter(
            sale__store=store,
            direction=SaleLine.Direction.SALE,
            barcode__in=sorted(item_of),
            sale__billed_at__lt=day_after,
        )
        .exclude(sale__docstatus=DocStatus.CANCELLED)
        .values("barcode")
        .annotate(last=Max("sale__billed_at"))
    )
    out: dict[uuid.UUID, date] = {}
    for row in rows:
        sku_id, sold = item_of[row["barcode"]], _local_day(row["last"])
        out[sku_id] = max(sold, out.get(sku_id, sold))
    return out


def store_ageing(
    store: Store, today: date | None = None, idle_days: int | None = None
) -> StoreAgeing:
    """Every good, accepted piece standing at this store, aged against its season."""
    from core.goods_fields import bounds
    from core.tenancy import require_tenant_id
    from masters.goods_identity_services import candidates_for
    from sell.services.goods_stock import is_goods_site
    from stockledger.goods_models import Origin, Position
    from stockledger.goods_reads import origin_seasons

    day = today or timezone.localdate()
    if not is_goods_site(store):
        return StoreAgeing(store=store, today=day, idle_days=idle_days, goods_records=False)

    positions = Position.objects.filter(
        site_id=store.pk,
        boundary="physical",
        condition="good",
        accepted_event__isnull=False,
        origin__isnull=False,
    ).values_list("portion", "origin_id", "accepted_event__event_at")
    pieces: dict[tuple[uuid.UUID, date], int] = defaultdict(int)
    for portion, origin_id, accepted_at in positions:
        lower, upper = bounds(portion)
        pieces[(origin_id, _local_day(accepted_at))] += upper - lower
    if not pieces:
        return StoreAgeing(store=store, today=day, idle_days=idle_days, goods_records=True)

    origins = {
        row.pk: row for row in Origin.objects.filter(pk__in=sorted({o for o, _ in pieces}, key=str))
    }
    seasons = origin_seasons([str(pk) for pk in origins])
    facts = season_facts(
        {
            str(described["now"]["season_id"])
            for described in seasons.values()
            if described["now"]["season_id"]
        }
    )
    barcodes = item_barcodes(store, {o.sku_id for o in origins.values()}, timezone.now())
    sold = last_sales(store, barcodes.item_of, day)
    identity = {
        row["sku_id"]: row
        for row in candidates_for(require_tenant_id(), {str(o.sku_id) for o in origins.values()})
    }

    rows: list[AgeingRow] = []
    for (origin_id, arrived_here), qty in pieces.items():
        origin = origins[origin_id]
        season_id = (seasons.get(str(origin_id)) or {}).get("now", {}).get("season_id")
        season = facts.get(str(season_id)) if season_id else None
        last_sale = sold.get(origin.sku_id)
        verdict = classify(
            season, today=day, arrived_here=arrived_here, last_sale=last_sale, idle_days=idle_days
        )
        if origin.source_kind == Origin.SourceKind.OPENING:
            first = _older_arrival(origin.frozen_evidence or {})
            before = None if first else _local_day(origin.source_time)
        else:
            first, before = _local_day(origin.source_time), None
        described = identity.get(str(origin.sku_id), {})
        rows.append(
            AgeingRow(
                origin_id=origin_id,
                sku_id=origin.sku_id,
                barcode=barcodes.shown.get(origin.sku_id, ""),
                brand=str(described.get("brand") or ""),
                brand_id=described.get("brand_id"),
                item=str(described.get("grade") or ""),
                design=str(described.get("style") or ""),
                size=str(described.get("size") or ""),
                colour=str(described.get("colour") or ""),
                season_code=season.code if season else "",
                season_label=season.label if season else "",
                group=verdict.group,
                qty=qty,
                first_arrived_on=first,
                days_in_company=max(0, (day - first).days) if first else None,
                in_company_before=before,
                arrived_here_on=arrived_here,
                days_here=max(0, (day - arrived_here).days),
                last_sale_on=last_sale,
                since=verdict.since,
                age_days=verdict.age_days,
                aged=verdict.aged,
            )
        )
    rows.sort(
        key=lambda r: (
            GROUPS.index(r.group),
            not r.aged,
            -(r.age_days or 0),
            r.barcode,
            r.arrived_here_on,
        )
    )
    return StoreAgeing(store=store, today=day, idle_days=idle_days, goods_records=True, rows=rows)
