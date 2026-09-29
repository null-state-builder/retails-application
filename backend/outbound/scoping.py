"""Read scope for outbound documents (issue #141) — one predicate per type.

Writing an outbound document has always been gated (`enforce_store_scope`);
reading one was not, so the Deoghar manager's Transfers screen listed a move
between two other stores and any voucher id opened. The gates live here, above
the views, for one reason: the top-bar search registry (`search.views`) narrows
exactly the same documents, and a predicate written twice is a predicate that
drifts. The `#101` rewrite found the booking predicate hand-copied three times.

Transfer lines carry snapshot brand names while the document carries both sites.
Its read boundary therefore composes complete assignment scopes across those
lines; stock requests still use the store-only `masters.scoping` helper.

The store-owned documents (return to brand, adjustment, write-off, V-flip, mark
damaged) need nothing here at all: they carry `store_id`, so their views call
`masters.scoping.scope_by_store` directly.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Exists, Model, OuterRef, Q, QuerySet
from rest_framework.exceptions import PermissionDenied

from accounts.principal import access_for_user
from core.tenancy import require_tenant_id
from masters.models import Brand, Store
from masters.brand_identity import with_brand_identity
from masters.scoping import scope_by_store_predicate
from masters.unit_context import active_brand_name, active_unit_code
from outbound.models import StoreTransferLine


def transfer_at_stores(store_ids: list[int]) -> Q:
    """A transfer belongs to **both ends of the move**.

    The sender is answerable for the pieces and the receiver is expecting them,
    so either end may read it — matching the top-bar search, and the reason this
    document cannot go through the plain `store_id` gate.
    """
    return Q(source_store_id__in=store_ids) | Q(destination_store_id__in=store_ids)


def scope_transfers[M: Model](qs: QuerySet[M], user: Any) -> QuerySet[M]:
    """Read a transfer only when one complete assignment covers each line.

    Either end may read a transfer, but every line must be covered at an end;
    matching one permitted brand must not reveal other brands on the document.
    A draft with no lines needs an all-brand assignment at one end. We keep
    tenant and scope checks as SQL predicates so
    list pagination, detail reads, files and search agree without materializing
    every transfer in Python.
    """
    tenant_id = require_tenant_id()
    qs = qs.filter(
        source_store__tenant_id=tenant_id,
        destination_store__tenant_id=tenant_id,
    )
    human_id = getattr(user, "human_id", None)
    if not (getattr(user, "is_authenticated", False) and human_id
            and getattr(user, "tenant_id", None) == tenant_id):
        return qs.none()
    assignments = access_for_user(user).section_grants("transfer", "view")
    if not assignments:
        return qs.none()
    line_allowed = Q(pk__in=[])
    draft_allowed = Q(pk__in=[])
    unrestricted = False
    for row in assignments:
        if not (row.all_sites or row.site_ids) or not (row.all_brands or row.brand_ids):
            continue
        if row.all_sites and row.all_brands:
            unrestricted = True
            break
        site_match = (
            Q(transfer__source_store_id__in=row.site_ids)
            | Q(transfer__destination_store_id__in=row.site_ids)
        ) if not row.all_sites else None
        document_site_match = transfer_at_stores(list(row.site_ids)) if not row.all_sites else Q()
        if row.all_brands:
            line_allowed |= site_match if site_match is not None else Q()
            draft_allowed |= document_site_match
            continue
        part = Q(_access_brand_id__in=row.brand_ids)
        if site_match is not None:
            part &= site_match
        line_allowed |= part

    # Unresolved lines remain forbidden, including to an all/all assignment.
    # Excluding them from the allowed subquery must not make a whole document
    # appear to have fewer affected scope cells.
    known = with_brand_identity(StoreTransferLine.objects.all(), tenant_id)
    allowed_lines = known if unrestricted else known.filter(line_allowed)
    lines = StoreTransferLine.objects.filter(transfer_id=OuterRef("pk"))
    qs = qs.alias(
        _so03_has_line=Exists(lines),
        _so03_forbidden_line=Exists(lines.exclude(pk__in=allowed_lines.values("pk"))),
    ).filter(_so03_forbidden_line=False)
    if not unrestricted:
        qs = qs.filter(Q(_so03_has_line=True) | draft_allowed)

    # The top-bar context narrows reading; it never combines a site chosen from
    # one assignment with a brand chosen from another to grant a new cell.
    chosen_site = active_unit_code()
    selected_site_id: int | None = None
    if chosen_site:
        stores = Store.objects.filter(tenant_id=tenant_id, is_active=True)
        store = stores.filter(pk=int(chosen_site)).first() if chosen_site.isdecimal() else None
        if store is None or not any(row.all_sites or store.pk in row.site_ids for row in assignments):
            raise PermissionDenied("You may not work in this business unit.")
        selected_site_id = store.pk
        qs = qs.filter(transfer_at_stores([store.pk]))
    chosen_brand = active_brand_name()
    selected_brand_id: int | None = None
    if chosen_brand:
        brands = Brand.objects.filter(tenant_id=tenant_id, is_active=True)
        brand = brands.filter(pk=int(chosen_brand)).first() if chosen_brand.isdecimal() else None
        if brand is None or not any(row.all_brands or brand.pk in row.brand_ids for row in assignments):
            raise PermissionDenied("You may not work in this brand.")
        selected_brand_id = brand.pk
        qs = qs.filter(pk__in=known.filter(_access_brand_id=brand.pk).values("transfer_id"))
    if selected_site_id is not None and selected_brand_id is not None and not any(
        (row.all_sites or selected_site_id in row.site_ids)
        and (row.all_brands or selected_brand_id in row.brand_ids)
        for row in assignments
    ):
        raise PermissionDenied("You may not work in this site and brand context.")
    return qs


def stock_request_at_stores(store_ids: list[int]) -> Q:
    """A stock request belongs to **both ends of the ask** — the store that
    raised it and the store answering it — same shape as a transfer, and for
    the same reason (#74)."""
    return Q(requesting_store_id__in=store_ids) | Q(fulfilling_store_id__in=store_ids)


def scope_stock_requests[M: Model](qs: QuerySet[M], user: Any) -> QuerySet[M]:
    """`scope_transfers`'s rule, for a stock request's two ends."""
    scoped: QuerySet[M] = scope_by_store_predicate(qs, user, stock_request_at_stores, section="transfer", minimum="view")
    return scoped
