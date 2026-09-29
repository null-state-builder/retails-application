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
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import user_can
from core.documents import DocStatus
from core.textsearch import search_term
from masters.models import Brand, Cohort, Sku
from masters.scoping import (
    active_store_ids,
    is_brand_scoped,
    scope_by_store_and_brand,
)
from outbound.models import ReturnToVendor, StockAdjustment, StoreTransfer, VFlip, WriteOff
from outbound.scoping import transfer_at_stores
from stockledger.models import StockOnHand
from vendors.models import Booking
from vendors.scoping import booking_at_stores

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
    ),
]


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------


def _search_documents(user: Any, q: str) -> tuple[list[dict[str, Any]], bool]:
    """Documents whose voucher number contains `q`, across every type the caller
    may see. Returns the capped rows and whether anything was dropped."""
    # A document carries no brand, so a brand-scoped caller has nothing here that
    # could be shown to be theirs. Fail closed rather than read their empty store
    # list as "unrestricted" (ADR-0003).
    if is_brand_scoped(user):
        return [], False
    store_ids = active_store_ids(user)
    per_type: list[list[dict[str, Any]]] = []
    for spec in DOC_TYPES:
        if not user_can(user, spec.section):
            continue
        qs = spec.model.objects.filter(**{f"{spec.number_field}__icontains": q})
        if store_ids is not None:
            qs = spec.scope(qs, store_ids)
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

    The item identity is master data everyone may look up; *how much is where*
    is store data, so this is the line the anti-leak test guards — on both axes,
    since a brand manager scanning another brand's barcode must be told the item
    and nothing about its stock.
    """
    rows = scope_by_store_and_brand(
        StockOnHand.objects.filter(sku_code__in=barcodes, net_qty__gt=0),
        user,
        "store_id",
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


def _search_items(user: Any, q: str) -> tuple[list[dict[str, Any]], bool]:
    """Items by scanned barcode or free text, each cohort carrying its own stock.

    A barcode is a scan-alias, not a unique key for stock: the same barcode is
    bought season after season at different locked costs, so an exact scan
    answers with the SKU's cohorts (issue #86), each with its own stock line.
    """
    if not user_can(user, "stock"):
        return [], False
    base = Sku.objects.filter(is_active=True)
    exact = list(base.filter(barcode__iexact=q)[:1])
    if exact:
        skus = exact
    else:
        skus = list(
            base.filter(
                Q(barcode__icontains=q)
                | Q(design__icontains=q)
                | Q(item__icontains=q)
                | Q(brand__icontains=q)
            ).order_by("barcode")[: MAX_PER_GROUP + 1]
        )
    truncated = len(skus) > MAX_PER_GROUP
    skus = skus[:MAX_PER_GROUP]
    if not skus:
        return [], False

    barcodes = [s.barcode for s in skus]
    context = _stock_context(user, barcodes)
    cohorts: dict[str, list[Cohort]] = defaultdict(list)
    for cohort in Cohort.objects.filter(barcode__in=barcodes).order_by("-id"):
        cohorts[cohort.barcode].append(cohort)

    rows: list[dict[str, Any]] = []
    for sku in skus:
        identity = " · ".join(p for p in (sku.design, sku.color, sku.size) if p)
        subtitle = " · ".join(p for p in (sku.brand, identity or sku.item) if p)
        is_exact = sku.barcode.lower() == q.lower()
        to = f"/stock?sku={quote(sku.barcode)}"
        found = cohorts.get(sku.barcode, [])[:MAX_COHORTS_PER_SKU]
        if not found:
            rows.append(
                {
                    "kind": "item",
                    "title": sku.barcode,
                    "subtitle": subtitle,
                    "meta": _stock_meta(context, (sku.barcode, "")),
                    "to": to,
                    "exact": is_exact,
                    "mrp_paise": sku.mrp_paise,
                }
            )
            continue
        for cohort in found:
            rows.append(
                {
                    "kind": "item",
                    "title": sku.barcode,
                    "subtitle": f"{subtitle} · {cohort.season}".strip(" ·"),
                    "meta": _stock_meta(context, (sku.barcode, cohort.season)),
                    "to": to,
                    "exact": is_exact,
                    "mrp_paise": cohort.mrp_paise
                    if cohort.mrp_paise is not None
                    else sku.mrp_paise,
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
    found = list(
        Brand.objects.filter(Q(name__icontains=q) | Q(code__icontains=q), is_active=True).order_by(
            "name"
        )[: MAX_PER_GROUP + 1]
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
            ("items", "Items & barcodes", _search_items),
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
        return Response({"query": query, "groups": groups, "total": total, "notes": notes})
