"""SOR stock ageing from the brand's dispatch date (store operations ticket 24, ST-BRD-5).

Goods on sale or return are the brand's until the brand invoices them, and the
brand must invoice at the time of supply or at 6 months, whichever is earlier
(CGST Act s.31(7)). So each SOR piece is flagged at **5 months from the brand's
dispatch date**, which leaves a month to settle it one of three ways: sell it,
return it to the brand, or get the brand's invoice.

* A piece is SOR when its brand's approved terms in force today, for the piece's
  season, say SOR (ticket 23). A brand or season with no approved model is
  **unknown**: those pieces are listed on their own and never treated as SOR (D9).
* The dispatch date is the brand's, recorded against the delivery the piece came
  on (``inbound.BrandDispatchDate``). A transfer keeps the piece's origin, so it
  keeps its delivery. With no date recorded - or opening stock, which came on no
  delivery here - the piece is listed as **no dispatch date**, never aged from
  its arrival or any other guess.
* Once the brand's invoice for the delivery is recorded (``inbound.SorBrandInvoice``)
  its pieces are settled and leave the alert.

A read: nothing here writes, and no cost, margin or other term is in any row.
"""

from __future__ import annotations

import calendar
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.utils import timezone

from masters.models import Store

OVERDUE = "overdue"
DUE = "due"
NO_DISPATCH_DATE = "no_dispatch_date"
AGEING = "ageing"
INVOICED = "invoiced"
#: In the order the page lists them: most urgent first.
GROUPS: tuple[str, ...] = (OVERDUE, DUE, NO_DISPATCH_DATE, AGEING, INVOICED)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def add_months(day: date, months: int) -> date:
    """``months`` calendar months after ``day``; a day the month lacks is its last."""
    years, month0 = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, month0 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def months_setting() -> tuple[int, int]:
    """(months to the alert, months to the brand's invoice), from the settings."""
    return int(settings.KDPS_SOR_ALERT_MONTHS), int(settings.KDPS_SOR_INVOICE_MONTHS)


@dataclass(frozen=True)
class Verdict:
    state: str
    #: The day the piece is flagged; ``None`` with no dispatch date or once invoiced.
    alert_on: date | None
    #: The day the brand must have invoiced it by.
    invoice_by: date | None
    #: Days from today to ``invoice_by``: 0 on the day, negative once past it.
    days_left: int | None


def judge(
    dispatch_date: date | None,
    *,
    invoiced: bool,
    today: date,
    alert_months: int | None = None,
    invoice_months: int | None = None,
) -> Verdict:
    """Where one SOR piece stands on ``today``."""
    if invoiced:
        return Verdict(INVOICED, None, None, None)
    if dispatch_date is None:
        return Verdict(NO_DISPATCH_DATE, None, None, None)
    set_alert, set_invoice = months_setting()
    alert_on = add_months(dispatch_date, set_alert if alert_months is None else alert_months)
    invoice_by = add_months(
        dispatch_date, set_invoice if invoice_months is None else invoice_months
    )
    days_left = (invoice_by - today).days
    if today >= invoice_by:
        state = OVERDUE
    elif today >= alert_on:
        state = DUE
    else:
        state = AGEING
    return Verdict(state, alert_on, invoice_by, days_left)


# ---------------------------------------------------------------------------
# What is recorded against a delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrandInvoice:
    number: str
    invoice_date: date

    def as_json(self) -> dict[str, Any]:
        return {"number": self.number, "invoice_date": self.invoice_date.isoformat()}


def dispatch_dates(arrival_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, date]:
    """The brand's dispatch date standing for each delivery: its newest record."""
    from inbound.sor_models import BrandDispatchDate

    out: dict[uuid.UUID, date] = {}
    for arrival_id, day in (
        BrandDispatchDate.objects.filter(arrival_id__in=sorted(set(arrival_ids), key=str))
        .order_by("recorded_at", "event_at")
        .values_list("arrival_id", "dispatch_date")
    ):
        out[arrival_id] = day
    return out


def brand_invoices(arrival_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, BrandInvoice]:
    """The brand's invoice standing for each delivery: its newest record."""
    from inbound.sor_models import SorBrandInvoice

    out: dict[uuid.UUID, BrandInvoice] = {}
    for arrival_id, number, day in (
        SorBrandInvoice.objects.filter(arrival_id__in=sorted(set(arrival_ids), key=str))
        .order_by("recorded_at", "event_at")
        .values_list("arrival_id", "invoice_number", "invoice_date")
    ):
        out[arrival_id] = BrandInvoice(number, day)
    return out


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


class Models:
    """Each (brand, season)'s commercial model today, read once per request.

    The terms in force today, as approved by now (ticket 23). Terms apply from a
    date that is never in the past, so asking about the day the goods arrived
    would call every piece that came before its brand's terms were entered
    unknown; the model a brand and season are on now is the one that decides
    how their stock is settled.
    """

    def __init__(self, tenant_id: Any, today: date, at: datetime) -> None:
        from masters.brand_terms import approved_terms, in_force

        by_pair: dict[tuple[int, int], list[Any]] = defaultdict(list)
        for terms in approved_terms(tenant_id):
            by_pair[(terms.brand_id, terms.season_id)].append(terms)
        self._models = {
            pair: str(found.model)
            for pair, versions in by_pair.items()
            if (found := in_force(versions, today, at)) is not None
        }

    def of(self, brand_id: int | None, season_id: int | None) -> str | None:
        """The model's value, or ``None``: unknown, never assumed (D9)."""
        if brand_id is None or season_id is None:
            return None
        return self._models.get((brand_id, season_id))


@dataclass(frozen=True)
class SorRow:
    """SOR pieces of one origin standing at this store."""

    origin_id: uuid.UUID
    #: The delivery they came on; ``None`` for opening stock, which came on none.
    arrival_id: uuid.UUID | None
    arrival_site: str
    arrived_on: date | None
    vendor_invoice: str
    sku_id: uuid.UUID
    barcode: str
    brand: str
    item: str
    design: str
    size: str
    colour: str
    season_code: str
    season_label: str
    qty: int
    dispatch_date: date | None
    brand_invoice: BrandInvoice | None
    state: str
    alert_on: date | None
    invoice_by: date | None
    days_left: int | None

    def as_json(self) -> dict[str, Any]:
        def day(value: date | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "origin_id": str(self.origin_id),
            "arrival_id": str(self.arrival_id) if self.arrival_id else None,
            "arrival_site": self.arrival_site,
            "arrived_on": day(self.arrived_on),
            "vendor_invoice": self.vendor_invoice,
            "sku_id": str(self.sku_id),
            "barcode": self.barcode,
            "brand": self.brand,
            "item": self.item,
            "design": self.design,
            "size": self.size,
            "colour": self.colour,
            "season_code": self.season_code,
            "season_label": self.season_label,
            "qty": self.qty,
            "dispatch_date": day(self.dispatch_date),
            "brand_invoice": self.brand_invoice.as_json() if self.brand_invoice else None,
            "state": self.state,
            "alert_on": day(self.alert_on),
            "invoice_by": day(self.invoice_by),
            "days_left": self.days_left,
        }


@dataclass(frozen=True)
class UnknownModel:
    """Pieces here whose brand and season have no approved model."""

    brand: str
    season_code: str
    season_label: str
    qty: int
    items: int

    def as_json(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "season_code": self.season_code,
            "season_label": self.season_label,
            "qty": self.qty,
            "items": self.items,
        }


@dataclass(frozen=True)
class GroupTotal:
    group: str
    qty: int
    items: int
    deliveries: int

    def as_json(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "qty": self.qty,
            "items": self.items,
            "deliveries": self.deliveries,
        }


@dataclass(frozen=True)
class SorAgeing:
    store: Store
    today: date
    #: False for a legacy store: it keeps no origins, so no piece can be traced.
    goods_records: bool
    rows: list[SorRow] = field(default_factory=list)
    unknown_models: list[UnknownModel] = field(default_factory=list)

    def totals(self) -> list[GroupTotal]:
        out = []
        for group in GROUPS:
            rows = [row for row in self.rows if row.state == group]
            out.append(
                GroupTotal(
                    group=group,
                    qty=sum(row.qty for row in rows),
                    items=len({row.sku_id for row in rows}),
                    deliveries=len({row.arrival_id for row in rows if row.arrival_id}),
                )
            )
        return out


def _receipt_of(origins: dict[uuid.UUID, Any]) -> dict[uuid.UUID, uuid.UUID]:
    """``{origin id: the receipt origin behind it}`` - a value-damage origin
    revalues a received piece, so its delivery is its previous origin's."""
    from stockledger.goods_models import Origin

    out: dict[uuid.UUID, uuid.UUID] = {}
    for pk, origin in origins.items():
        if origin.source_kind == Origin.SourceKind.VALUE_DAMAGE and origin.previous_origin_id:
            out[pk] = origin.previous_origin_id
        else:
            out[pk] = pk
    return out


def _deliveries(origin_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """``{receipt origin id: arrival id}``: the covering PT, its GRN, that GRN's arrival."""
    from ptmapper.goods_models import GoodsPt
    from stockledger.goods_models import Origin

    documents = dict(
        Origin.objects.filter(
            pk__in=sorted(set(origin_ids), key=str), source_kind=Origin.SourceKind.RECEIPT
        ).values_list("pk", "official_line__version__document_id")
    )
    arrivals = dict(
        GoodsPt.objects.filter(
            document_id__in=sorted(set(documents.values()), key=str), grn__isnull=False
        ).values_list("document_id", "grn__arrival_id")
    )
    return {
        origin_id: arrivals[document_id]
        for origin_id, document_id in documents.items()
        if document_id in arrivals
    }


def store_sor_ageing(store: Store, today: date | None = None) -> SorAgeing:
    """Every SOR piece at this site, aged from its brand's dispatch date; and the
    pieces whose model is unknown.

    Every piece the brand has not invoiced is counted, whatever its condition: a
    damaged piece waiting to go back to the brand ages like any other, and so does
    one received but not yet accepted. A piece on the road is counted at the site
    it is travelling to, so it is never out of every list while it moves."""
    from django.db.models import Q

    from core.goods_fields import bounds
    from core.tenancy import require_tenant_id
    from inbound.goods_models import Arrival
    from masters.brand_terms_models import CommercialModel
    from masters.goods_identity_models import ProductSku
    from masters.goods_identity_services import candidates_for
    from masters.models import Brand
    from sell.services.goods_stock import is_goods_site
    from stockledger.goods_ageing import item_barcodes
    from stockledger.goods_models import Origin, Position
    from stockledger.goods_reads import origin_seasons

    day = today or timezone.localdate()
    if not is_goods_site(store):
        return SorAgeing(store=store, today=day, goods_records=False)

    here = Q(site_id=store.pk, boundary="physical") | Q(
        boundary="transit", transfer__destination_site_id=store.pk
    )
    pieces: dict[uuid.UUID, int] = defaultdict(int)
    for portion, origin_id in Position.objects.filter(here, origin__isnull=False).values_list(
        "portion", "origin_id"
    ):
        lower, upper = bounds(portion)
        pieces[origin_id] += upper - lower
    if not pieces:
        return SorAgeing(store=store, today=day, goods_records=True)

    origins = {row.pk: row for row in Origin.objects.filter(pk__in=sorted(pieces, key=str))}
    seasons = origin_seasons([str(pk) for pk in origins])
    item_brands = dict(
        ProductSku.objects.filter(
            pk__in=sorted({o.sku_id for o in origins.values()}, key=str)
        ).values_list("pk", "style__brand_id")
    )
    models = Models(require_tenant_id(), day, timezone.now())

    receipt = _receipt_of(origins)
    delivery = _deliveries(receipt.values())
    arrivals = {
        row.pk: row
        for row in Arrival.objects.select_related("site").filter(
            pk__in=sorted(set(delivery.values()), key=str)
        )
    }
    dated = dispatch_dates(arrivals)
    invoiced = brand_invoices(arrivals)

    sor: list[tuple[Any, int, dict[str, Any]]] = []
    unknown: dict[tuple[str, str, str], dict[str, Any]] = {}
    for origin_id, qty in pieces.items():
        origin = origins[origin_id]
        season = (seasons.get(str(origin_id)) or {}).get("now") or {}
        season_id = None if season.get("unknown_historical") else season.get("season_id")
        arrival = arrivals.get(delivery.get(receipt[origin_id], uuid.UUID(int=0)))
        # The brand the goods were received as; the item's own brand otherwise.
        brand_id = arrival.brand_id if arrival is not None else item_brands.get(origin.sku_id)
        model = models.of(brand_id, int(season_id) if season_id else None)
        if model is None:
            key = (
                str(brand_id or ""),
                str(season.get("code") or ""),
                str(season.get("label") or ""),
            )
            entry = unknown.setdefault(key, {"brand_id": brand_id, "qty": 0, "skus": set()})
            entry["qty"] += qty
            entry["skus"].add(origin.sku_id)
            continue
        if model != CommercialModel.SOR.value:
            continue
        sor.append((origin, qty, {"season": season, "arrival": arrival}))

    used = {a.brand_id for _o, _q, extra in sor if (a := extra["arrival"]) is not None} | {
        entry["brand_id"] for entry in unknown.values() if entry["brand_id"]
    }
    names = dict(Brand.objects.filter(pk__in=sorted(used)).values_list("pk", "name"))
    identity = {
        row["sku_id"]: row
        for row in candidates_for(require_tenant_id(), {str(o.sku_id) for o, _q, _x in sor})
    }
    barcodes = item_barcodes(store, {o.sku_id for o, _q, _x in sor}, timezone.now())

    rows: list[SorRow] = []
    for origin, qty, extra in sor:
        arrival = extra["arrival"]
        season = extra["season"]
        dispatch = dated.get(arrival.pk) if arrival else None
        invoice = invoiced.get(arrival.pk) if arrival else None
        verdict = judge(dispatch, invoiced=invoice is not None, today=day)
        described = identity.get(str(origin.sku_id), {})
        rows.append(
            SorRow(
                origin_id=origin.pk,
                arrival_id=arrival.pk if arrival else None,
                arrival_site=arrival.site.code if arrival else "",
                arrived_on=timezone.localtime(arrival.actual_arrival_at).date()
                if arrival
                else None,
                vendor_invoice=(arrival.invoice_number or "") if arrival else "",
                sku_id=origin.sku_id,
                barcode=barcodes.shown.get(origin.sku_id, ""),
                brand=str(
                    described.get("brand") or names.get(arrival.brand_id if arrival else 0, "")
                ),
                item=str(described.get("grade") or ""),
                design=str(described.get("style") or ""),
                size=str(described.get("size") or ""),
                colour=str(described.get("colour") or ""),
                season_code=str(season.get("code") or ""),
                season_label=str(season.get("label") or ""),
                qty=qty,
                dispatch_date=dispatch,
                brand_invoice=invoice,
                state=verdict.state,
                alert_on=verdict.alert_on,
                invoice_by=verdict.invoice_by,
                days_left=verdict.days_left,
            )
        )
    rows.sort(
        key=lambda r: (
            GROUPS.index(r.state),
            r.days_left if r.days_left is not None else 0,
            r.brand,
            r.barcode,
            str(r.origin_id),
        )
    )
    listed = [
        UnknownModel(
            brand=names.get(entry["brand_id"], "") if entry["brand_id"] else "",
            season_code=code,
            season_label=label,
            qty=entry["qty"],
            items=len(entry["skus"]),
        )
        for (_brand, code, label), entry in unknown.items()
    ]
    listed.sort(key=lambda u: (u.brand, u.season_code))
    return SorAgeing(store=store, today=day, goods_records=True, rows=rows, unknown_models=listed)
