"""Global search (issue #86) — one endpoint, grouped results, scope-enforced.

Every other screen is shaped around *creating* documents, but the store person's
single most frequent action is a lookup: scan a tag to check price and stock, or
find a voucher to reprint. So search is the one surface that cuts across all of
them — `GET /api/search?q=` answers with grouped results: items (and their
buying cohorts) with stock context, brands, and documents found by voucher
number across every document type.

This app is a **composition root**: it imports downward from the domain modules
and nothing imports it, so search can never become a back door around a module's
own scoping. Two gates apply to every result, both fail-closed:

* **store scope** (ADR-0003, `masters.scoping`) — stock counts and documents come
  only from the caller's own stores; an out-of-scope document must look exactly
  like one that does not exist;
* **section capability** (issue #85, `accounts.permissions`) — a document type is
  searchable only by someone who may see the section that owns it, so search
  hides precisely what the sidebar hides.

Customers and bills are named in the requirement but there is no customer or
sale model yet (POS is unbuilt); rather than pretend, a no-match answer says so.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from django.db.models import Q, QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import user_can
from accounts.principal import access_for_user, resolve_access
from accounts.sections import CAP_VIEW
from core.documents import DocStatus
from core.textsearch import search_term
from core.tenancy import require_tenant_id
from masters.models import Brand
from masters.scoping import (
    scope_by_store_and_brand,
)
from outbound.models import ReturnToVendor, StockAdjustment, StoreTransfer, VFlip, WriteOff
from outbound.scoping import scope_transfers, transfer_at_stores
from stockledger.models import StockOnHand
from vendors.models import Booking
from vendors.scoping import booking_at_stores, scope_bookings

#: Shortest query we will run. One character matches half the catalogue and the
#: panel would be noise, not an answer.
MIN_QUERY_LEN = 2
#: Rows per group. The panel is a shortcut, not a report — a long list means the
#: query was too vague, and the group says so with `truncated`.
MAX_PER_GROUP = 8
#: Cohorts shown per matched barcode, so one much-repeated SKU cannot flood the
#: item group with its seasons.
MAX_COHORTS_PER_SKU = 3

_NO_CUSTOMERS_NOTE = "Customers and bills are not searchable yet — they arrive with billing (POS)."


# --------------------------------------------------------------------------
# Document registry
# --------------------------------------------------------------------------
# Search finds a document by its human voucher number, whichever type it is.
# Each entry says how to narrow that type to the caller's stores, which section
# owns it (the capability gate), and where the client opens it.


def _scope_store(qs: QuerySet[Any], ids: list[int]) -> QuerySet[Any]:
    return qs.filter(store_id__in=ids)


def _scope_transfer(qs: QuerySet[Any], ids: list[int]) -> QuerySet[Any]:
    # A transfer is visible to both ends of the movement. The predicate itself
    # belongs to `outbound`, which applies the same one to the transfer list and
    # detail endpoints (#141) — written twice it would drift, and search would
    # answer with rows the screen it links to refuses to open.
    return qs.filter(transfer_at_stores(ids))


def _scope_booking(qs: QuerySet[Any], ids: list[int]) -> QuerySet[Any]:
    # The predicate belongs to `vendors` (#234) — the same shape the Booking
    # list, detail and pending-receipt endpoints answer with, written once so
    # search cannot drift from the screens its results link to.
    return qs.filter(booking_at_stores(ids)).distinct()


def _docstatus(doc: Any) -> str:
    # The kernel's labels are lowercase ("submitted"); capitalise so a document
    # row reads like the Booking row beside it ("Booked").
    return str(DocStatus(doc.docstatus).label).capitalize()


def _store_code(doc: Any) -> str:
    store = getattr(doc, "store", None)
    return str(store.code) if store else ""


@dataclass(frozen=True)
class DocType:
    """One searchable document type: how to find it, gate it, and open it."""

    label: str
    model: type[Any]
    number_field: str
    #: Section whose capability gates this type (issue #85).
    section: str
    #: Client route template; `{pk}` is the document's id.
    route: str
    scope: Callable[[QuerySet[Any], list[int]], QuerySet[Any]]
    related: tuple[str, ...]
    #: Where the document happened, for the result's second line.
    context: Callable[[Any], str]
    status: Callable[[Any], str] = _docstatus
    brand_field: str | None = None
    #: Every ownership link must belong to the bound tenant. Legacy documents
    #: have no tenant column of their own, so an all-sites assignment alone is
    #: not evidence that a row belongs to this deployment.
    tenant_paths: tuple[str, ...] = ()
    nullable_tenant_paths: tuple[str, ...] = ()


DOC_TYPES: list[DocType] = [
    DocType(
        label="Booking",
        model=Booking,
        number_field="number",
        section="booking",
        route="/booking/{pk}",
        scope=_scope_booking,
        related=("brand", "vendor", "destination_store"),
        context=lambda d: d.brand.name,
        status=lambda d: str(d.get_status_display()),
        brand_field="brand_id",
        tenant_paths=("brand", "vendor"),
        nullable_tenant_paths=("destination_store",),
    ),
    DocType(
        label="Transfer",
        model=StoreTransfer,
        number_field="doc_number",
        section="transfer",
        route="/transfer/{pk}",
        scope=_scope_transfer,
        related=("source_store", "destination_store"),
        context=lambda d: f"{d.source_store.code} → {d.destination_store.code}",
        tenant_paths=("source_store", "destination_store"),
    ),
    DocType(
        label="Return to brand",
        model=ReturnToVendor,
        number_field="doc_number",
        section="return_to_brand",
        route="/return-to-brand/{pk}",
        scope=_scope_store,
        related=("store", "brand"),
        context=lambda d: f"{_store_code(d)} · {d.brand.name}" if d.brand else _store_code(d),
        brand_field="brand_id",
        tenant_paths=("store", "vendor"),
        nullable_tenant_paths=("brand",),
    ),
    DocType(
        label="Adjustment",
        model=StockAdjustment,
        number_field="doc_number",
        section="stock_count",
        route="/stock-count/adjustments/{pk}",
        scope=_scope_store,
        related=("store",),
        context=_store_code,
        tenant_paths=("store",),
    ),
    DocType(
        label="Write-off",
        model=WriteOff,
        number_field="doc_number",
        section="stock_count",
        route="/stock-count/writeoffs/{pk}",
        scope=_scope_store,
        related=("store",),
        context=_store_code,
        tenant_paths=("store",),
    ),
    DocType(
        label="V-flip",
        model=VFlip,
        number_field="doc_number",
        section="stock",
        route="/stock/vflips/{pk}",
        scope=_scope_store,
        related=("store", "original_brand"),
        context=_store_code,
        brand_field="original_brand_id",
        tenant_paths=("store", "original_brand"),
    ),
]


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------


def _section_assignments(user: Any, section: str) -> list[Any]:
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or getattr(user, "tenant_id", None) != require_tenant_id()
    ):
        return []
    human_id = getattr(user, "human_id", None)
    if human_id is None:
        return []
    return access_for_user(user).section_grants(section, CAP_VIEW)


def _stock_brands(user: Any) -> tuple[bool, set[int]]:
    rows = _section_assignments(user, "stock")
    return any(row.all_brands for row in rows), {
        int(brand_id) for row in rows for brand_id in row.brand_ids
    }


def _search_documents(user: Any, q: str) -> tuple[list[dict[str, Any]], bool]:
    """Documents whose voucher number contains `q`, across every type the caller
    may see. Returns the capped rows and whether anything was dropped."""
    per_type: list[list[dict[str, Any]]] = []
    tenant_id = require_tenant_id()
    for spec in DOC_TYPES:
        assignments = _section_assignments(user, spec.section)
        if not assignments:
            continue
        qs = spec.model.objects.filter(**{f"{spec.number_field}__icontains": q})
        for path in spec.tenant_paths:
            qs = qs.filter(**{f"{path}__tenant_id": tenant_id})
        for path in spec.nullable_tenant_paths:
            qs = qs.filter(
                Q(**{f"{path}__isnull": True}) | Q(**{f"{path}__tenant_id": tenant_id})
            )
        if spec.model is StoreTransfer:
            # The transfer service checks snapshot brands across every line;
            # treating this document as brandless would hide selected-brand
            # assignments that its list/detail/file endpoints now accept.
            qs = scope_transfers(qs, user)
        elif spec.model is Booking:
            # A booking can span several lines/sites. Its owning service checks
            # every affected cell; matching just one line here would disclose a
            # voucher its detail endpoint correctly refuses to show.
            qs = scope_bookings(qs, user, minimum="view")
        else:
            scopes = Q(pk__in=[])
            for assignment in assignments:
                # A brandless document can only be proved in-scope by an
                # all-brands assignment. Keep each assignment's site and brand
                # halves together for branded documents.
                if spec.brand_field is None and not assignment.all_brands:
                    continue
                part = qs if assignment.all_sites else spec.scope(qs, list(assignment.site_ids))
                if spec.brand_field is not None and not assignment.all_brands:
                    part = part.filter(**{f"{spec.brand_field}__in": assignment.brand_ids})
                scopes |= Q(pk__in=part.values("pk"))
            qs = qs.filter(scopes)
        found: list[dict[str, Any]] = []
        for doc in qs.select_related(*spec.related).order_by("-id")[: MAX_PER_GROUP + 1]:
            number = str(getattr(doc, spec.number_field) or "")
            found.append(
                {
                    "kind": "document",
                    "title": number,
                    "subtitle": f"{spec.label} · {spec.context(doc)}".rstrip(" ·"),
                    "meta": spec.status(doc),
                    "to": spec.route.format(pk=doc.pk),
                    "exact": number.lower() == q.lower(),
                }
            )
        if found:
            per_type.append(found)

    # Every voucher number starts with the financial year, so a short query
    # matches every type at once. Interleave the types rather than listing them
    # in registry order — otherwise the first type fills the group and the rest
    # are invisible behind a "more matches" line.
    rows: list[dict[str, Any]] = []
    for rank in range(max((len(f) for f in per_type), default=0)):
        for found in per_type:
            if rank < len(found):
                rows.append(found[rank])
    total = sum(len(f) for f in per_type)
    # An exact voucher number is what the person typed; float it to the top.
    rows.sort(key=lambda r: not r["exact"])
    return rows[:MAX_PER_GROUP], total > MAX_PER_GROUP


def _stock_context(user: Any, barcodes: list[str]) -> dict[tuple[str, str], tuple[int, set[str]]]:
    """`(barcode, season) → (qty, store codes)` within the caller's scope only.

    Search identity and quantity both come from tenant-owned stock projections:
    the legacy SKU/cohort tables have no tenant key and cannot establish which
    tenant owns a barcode or season.
    """
    rows = scope_by_store_and_brand(
        StockOnHand.objects.filter(sku_code__in=barcodes, net_qty__gt=0),
        user,
        "store_id",
        section="stock",
    ).select_related("store")
    context: dict[tuple[str, str], tuple[int, set[str]]] = defaultdict(lambda: (0, set()))
    for row in rows:
        # `""` is the all-seasons roll-up; a season-less row must not be counted
        # twice into it.
        for key in {(row.sku_code, row.season), (row.sku_code, "")}:
            qty, stores = context[key]
            context[key] = (qty + row.net_qty, stores | {row.store.code})
    return context


def _stock_meta(context: dict[tuple[str, str], tuple[int, set[str]]], key: tuple[str, str]) -> str:
    qty, stores = context.get(key, (0, set()))
    if qty <= 0:
        return "No stock in your locations"
    if len(stores) == 1:
        return f"{qty} pcs at {next(iter(stores))}"
    return f"{qty} pcs across {len(stores)} locations"


def _search_items(user: Any, q: str, *, request: Request | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Items by scanned barcode or free text, each cohort carrying its own stock.

    A barcode is a scan-alias, not a unique key for stock. Until SO-04 moves
    legacy SKU/cohort history into tenant-owned masters, only a stock row at a
    scoped site and brand proves that this tenant may see its item dimensions
    and season. A global SKU row must never supply another tenant's metadata.
    """
    if not user_can(user, "stock"):
        return [], False
    if request is not None:
        from stockledger.on_hand_projection import canonical_sites, filtered_rows, projected_rows

        access = resolve_access(request)
        if canonical_sites(access.tenant_id):
            visible = filtered_rows(projected_rows(request, values=False), {"q": q})
            grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
            for stock_row in visible:
                grouped[(stock_row["sku_id"] or stock_row["sku_code"], stock_row["brand_id"], stock_row["season"])].append(stock_row)
            found = []
            for parts in grouped.values():
                preview = parts[0]
                barcode = preview["sku_code"]
                if not barcode:
                    continue
                context = {(barcode, preview["season"]): (sum(row["net_qty"] for row in parts),
                                                        {row["store_code"] for row in parts})}
                subtitle = " · ".join(str(preview[field]) for field in ("brand", "design", "color", "size", "season") if preview[field])
                found.append({"kind": "item", "title": barcode, "subtitle": subtitle,
                              "meta": _stock_meta(context, (barcode, preview["season"])),
                              "to": f"/stock?sku={quote(barcode)}", "exact": barcode.casefold() == q.casefold(),
                              "mrp_paise": None})
            return found[:MAX_PER_GROUP], len(found) > MAX_PER_GROUP
    stock = scope_by_store_and_brand(StockOnHand.objects.all(), user, section="stock")
    exact = list(stock.filter(sku_code__iexact=q).order_by("sku_code").values_list(
        "sku_code", flat=True
    ).distinct()[:1])
    barcodes = exact or list(
        stock.filter(
            Q(sku_code__icontains=q)
            | Q(design__icontains=q)
            | Q(item__icontains=q)
            | Q(brand__icontains=q)
        ).order_by("sku_code").values_list("sku_code", flat=True).distinct()[: MAX_PER_GROUP + 1]
    )
    truncated = len(barcodes) > MAX_PER_GROUP
    barcodes = barcodes[:MAX_PER_GROUP]
    if not barcodes:
        return [], False

    context = _stock_context(user, barcodes)
    owned_rows: dict[str, list[StockOnHand]] = defaultdict(list)
    for stock_row in stock.filter(sku_code__in=barcodes).order_by("-updated_at", "-pk"):
        owned_rows[stock_row.sku_code].append(stock_row)

    rows: list[dict[str, Any]] = []
    for barcode in barcodes:
        projections = owned_rows[barcode]
        if not projections:
            continue
        sample = projections[0]
        identity = " · ".join(p for p in (sample.design, sample.color, sample.size) if p)
        subtitle = " · ".join(p for p in (sample.brand, identity or sample.item) if p)
        is_exact = barcode.lower() == q.lower()
        to = f"/stock?sku={quote(barcode)}"
        seasons = list(dict.fromkeys(row.season for row in projections if row.season))[
            :MAX_COHORTS_PER_SKU
        ]
        if not seasons:
            rows.append(
                {
                    "kind": "item",
                    "title": barcode,
                    "subtitle": subtitle,
                    "meta": _stock_meta(context, (barcode, "")),
                    "to": to,
                    "exact": is_exact,
                    "mrp_paise": None,
                }
            )
            continue
        for season in seasons:
            rows.append(
                {
                    "kind": "item",
                    "title": barcode,
                    "subtitle": f"{subtitle} · {season}".strip(" ·"),
                    "meta": _stock_meta(context, (barcode, season)),
                    "to": to,
                    "exact": is_exact,
                    "mrp_paise": None,
                }
            )
    return rows[:MAX_PER_GROUP], truncated or len(rows) > MAX_PER_GROUP


def _search_brands(user: Any, q: str) -> tuple[list[dict[str, Any]], bool]:
    """Brands by name or code.

    Gated on `stock`, not `setup`, and landing on that brand's stock rather than
    the brand master: "search brand" on the store person's own sketch means
    *show me this brand's stock*, a daily merchandising lookup — not an
    administrative one they have no business in.
    """
    if not user_can(user, "stock"):
        return [], False
    all_brands, brand_ids = _stock_brands(user)
    found = list(
        Brand.objects.filter(
            Q(name__icontains=q) | Q(code__icontains=q),
            tenant_id=require_tenant_id(), is_active=True,
        )
        .filter(Q() if all_brands else Q(pk__in=brand_ids))
        .order_by("name")[: MAX_PER_GROUP + 1]
    )
    rows = [
        {
            "kind": "brand",
            "title": brand.name,
            "subtitle": brand.code,
            "meta": brand.commercial_label,
            "to": f"/stock?brand={quote(brand.name)}",
            "exact": brand.name.lower() == q.lower() or brand.code.lower() == q.lower(),
        }
        for brand in found[:MAX_PER_GROUP]
    ]
    return rows, len(found) > MAX_PER_GROUP


class GlobalSearchView(APIView):
    """`GET /api/search?q=` — one grouped, scope-enforced answer for the top bar."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "required": ["query", "groups", "total", "notes"],
                "properties": {
                    "query": {"type": "string"},
                    "groups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["key", "label", "results", "truncated"],
                            "properties": {
                                "key": {"type": "string"},
                                "label": {"type": "string"},
                                "truncated": {"type": "boolean"},
                                "results": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["kind", "title", "subtitle", "meta", "to", "exact"],
                                        "properties": {
                                            "kind": {"type": "string"},
                                            "title": {"type": "string"},
                                            "subtitle": {"type": "string"},
                                            "meta": {"type": "string"},
                                            "to": {"type": "string"},
                                            "exact": {"type": "boolean"},
                                            "mrp_paise": {"type": "integer", "nullable": True},
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "total": {"type": "integer"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            }
        }
    )
    def get(self, request: Request) -> Response:
        # The same `?q=` the in-page search boxes send (#102) — read through the
        # one helper so the two halves of search cannot drift into two contracts.
        query = search_term(request)
        if len(query) < MIN_QUERY_LEN:
            return Response(
                {
                    "query": query,
                    "groups": [],
                    "total": 0,
                    "notes": [f"Type at least {MIN_QUERY_LEN} characters, or scan a tag."],
                }
            )

        groups: list[dict[str, Any]] = []
        for key, label, finder in (
            ("items", "Items & barcodes", lambda user, term: _search_items(user, term, request=request)),
            ("brands", "Brands", _search_brands),
            ("documents", "Documents & vouchers", _search_documents),
        ):
            results, truncated = finder(request.user, query)
            if results:
                groups.append(
                    {"key": key, "label": label, "results": results, "truncated": truncated}
                )

        total = sum(len(g["results"]) for g in groups)
        # An empty answer says what it means, and why the one thing we cannot
        # search yet is missing — never a silent blank panel.
        notes = [] if total else ["Nothing found for this search.", _NO_CUSTOMERS_NOTE]
        resolve_access(request).revalidate_delivery()
        response = Response({"query": query, "groups": groups, "total": total, "notes": notes})
        response["Cache-Control"] = "no-store, private"
        return response
