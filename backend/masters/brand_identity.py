"""Stable, tenant-proven brand identity for supported legacy records.

Names may suggest a reconciliation target; they never authorize a resource.
Bindings are evidence alongside old snapshots, not a second permission source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db.models import BigIntegerField, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from core.commands import CommandRun, LockRank
from core.refusals import Refusal
from core.tenancy import require_tenant_id
from masters.goods_models import BrandIdentityBinding
from masters.models import Brand


@dataclass(frozen=True)
class BrandResource:
    sites: tuple[str, ...]
    source_brand: str | None = None
    immutable: bool = False


RESOURCES: dict[str, BrandResource] = {
    "alerts.alert": BrandResource(("store",)),
    "approvals.approval": BrandResource(("store",)),
    "stockledger.stockledgerentry": BrandResource(("store",), "booking.brand", True),
    "stockledger.stockonhand": BrandResource(("store",)),
    "stockledger.intransitstock": BrandResource(("source_store", "destination_store")),
    "stockledger.quarantinestock": BrandResource(("store",)),
    "outbound.storetransferline": BrandResource(("transfer.source_store", "transfer.destination_store")),
    "outbound.transferreceiptexception": BrandResource(("receipt.transfer.source_store", "receipt.transfer.destination_store")),
    "outbound.stockrequestline": BrandResource(("request.requesting_store", "request.fulfilling_store")),
    "outbound.transfergapclosureline": BrandResource(("closure.store",)),
    "outbound.markdamagedline": BrandResource(("mark.store",)),
    "outbound.returntovendorline": BrandResource(("rtv.store",), "rtv.brand"),
    "outbound.stockadjustmentline": BrandResource(("adjustment.store",)),
    "outbound.writeoffline": BrandResource(("writeoff.store",)),
    "outbound.vflipline": BrandResource(("vflip.store",)),
    "outbound.countsessionline": BrandResource(("session.stocktake.store",)),
    "sell.saleline": BrandResource(("sale.store",)),
    "sell.returnline": BrandResource(("return_doc.store",), "original_line.brand_ref"),
    "sell.savedsize": BrandResource(("store",)),
    **{f"reporting.{name}": BrandResource(("store",)) for name in (
        "saleslinefact", "offersimlinefact", "giftitcfact", "discountfundingfact",
        "shrinkagelinefact", "marginsharefact", "inventorystockfact", "inventoryreceiptfact",
        "brandsalelinefact", "inventoryitemfact",
    )},
}


def _resource(label: str) -> BrandResource:
    resource = RESOURCES.get(label)
    if resource is None:
        raise Refusal("NOT_FOUND", "That reconciliation resource was not found.")
    return resource


def _at(row: Any, path: str) -> Any:
    for part in path.split("."):
        row = getattr(row, part, None)
        if row is None:
            return None
    return row


def resource_rows(label: str, tenant_id: Any) -> Any:
    resource = _resource(label)
    query = apps.get_model(label).objects.all()
    for path in resource.sites:
        query = query.filter(**{path.replace(".", "__") + "__tenant_id": tenant_id})
    return query


def fingerprint(row: Any) -> str:
    values = {field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields}
    resource = RESOURCES.get(row._meta.label_lower)
    if resource is not None and resource.source_brand:
        source = _at(row, resource.source_brand)
        values["_source_identity"] = (
            {"id": source.pk, "tenant_id": source.tenant_id} if source is not None else None
        )
    # Hash the source data without returning its protected contents to an
    # account administrator who may only review identity and scope.
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def with_brand_identity(query: Any, tenant_id: Any, *, brandless: Q | None = None) -> Any:
    """Annotate a stable brand ID, checking the target belongs to this tenant."""
    label = query.model._meta.label_lower
    resource = _resource(label)
    query = query.filter(pk__in=resource_rows(label, tenant_id).values("pk"))
    binding = BrandIdentityBinding.objects.filter(
        tenant_id=tenant_id, resource_model=label, resource_id=OuterRef("pk"),
    ).values("brand_id")[:1]
    query = query.annotate(_bound_brand_id=Subquery(binding, output_field=BigIntegerField()))
    expression: Any = F("_bound_brand_id")
    fields = {f.attname for f in query.model._meta.concrete_fields}
    if "brand_ref_id" in fields:
        # Even corrupt/foreign established links are conflicts, never an excuse
        # to fall back to another identity or silently rebind the source.
        query = query.filter(Q(brand_ref_id__isnull=True) | Q(_bound_brand_id__isnull=True) | Q(brand_ref_id=F("_bound_brand_id")))
        expression = Coalesce(F("brand_ref_id"), expression, output_field=BigIntegerField())
    if resource.source_brand:
        source = resource.source_brand.replace(".", "__")
        query = query.filter(Q(**{source + "__isnull": True}) | Q(**{source + "__tenant_id": tenant_id}))
        query = query.annotate(_record_brand_id=expression)
        query = query.filter(Q(_record_brand_id__isnull=True) | Q(**{source + "__isnull": True}) | Q(_record_brand_id=F(source + "_id")))
        expression = Coalesce(F("_record_brand_id"), F(source + "_id"), output_field=BigIntegerField())
    query = query.annotate(_access_brand_id=expression)
    known = Q(_access_brand_id__in=Brand.objects.filter(
        tenant_id=tenant_id,
    ).values("pk"))
    if brandless is not None:
        # Only an explicit caller classification can designate a record as
        # having no brand dimension. A missing identity is otherwise unresolved.
        known = (known & ~brandless) | (brandless & Q(_access_brand_id__isnull=True))
    return query.filter(known)


def identity_id(row: Any, tenant_id: Any) -> int | None:
    query = with_brand_identity(resource_rows(row._meta.label_lower, tenant_id), tenant_id)
    return query.filter(pk=row.pk).values_list("_access_brand_id", flat=True).first()  # type: ignore[no-any-return]


def reconciliation_page(label: str, *, after: int = 0, limit: int = 50) -> dict[str, Any]:
    tenant_id = require_tenant_id()
    resource = _resource(label)
    query = resource_rows(label, tenant_id).filter(pk__gt=after).order_by("pk")
    window = list(query[:limit + 1])
    items = []
    for row in window[:limit]:
        if label == "alerts.alert":
            from alerts.scope_contract import WHOLE_SITE_KINDS
            if row.kind in WHOLE_SITE_KINDS and not row.brand:
                items.append({
                    "id": row.pk, "brand_label": "", "brand_id": None,
                    "derived_brand_id": None, "suggestions": [], "fingerprint": fingerprint(row),
                    "site_ids": [site.pk for path in resource.sites if (site := _at(row, path)) is not None],
                    "state": "whole_site",
                })
                continue
        current = identity_id(row, tenant_id)
        source = _at(row, resource.source_brand) if resource.source_brand else None
        derived = source.pk if source is not None and source.tenant_id == tenant_id else None
        suggestions = list(Brand.objects.filter(
            tenant_id=tenant_id, name__iexact=getattr(row, "brand", ""),
        ).order_by("pk").values("id", "code", "name"))
        items.append({
            "id": row.pk, "brand_label": getattr(row, "brand", ""),
            "brand_id": current, "derived_brand_id": derived,
            "suggestions": suggestions, "fingerprint": fingerprint(row),
            "site_ids": [site.pk for path in resource.sites if (site := _at(row, path)) is not None],
            "state": "linked" if current is not None else "unresolved",
        })
    return {"resource": label, "items": items,
            "next_cursor": window[limit - 1].pk if len(window) > limit else None}


def bind_reviewed_rows(run: CommandRun, label: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the entire reviewed batch under lock before the first write."""
    resource = _resource(label)
    run.advisory_lock(LockRank.SECURITY, [f"brand-identity:{label}"])
    prepared = []
    ids = [item.get("id") for item in rows]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
        raise Refusal("INVALID_REQUEST", "Source IDs must be integers.")
    if len(ids) != len(set(ids)):
        raise Refusal("INVALID_REQUEST", "Each source row may appear once.")
    for item in rows:
        if set(item) != {"id", "brand_id", "fingerprint"}:
            raise Refusal("INVALID_REQUEST", "Supply id, brand_id and fingerprint for each reviewed row.")
        if any(isinstance(item[key], bool) or not isinstance(item[key], int) for key in ("id", "brand_id")):
            raise Refusal("INVALID_REQUEST", "Source and brand IDs must be integers.")
        row = resource_rows(label, run.tenant_id).select_for_update(of=("self",)).filter(pk=item["id"]).first()
        brand = Brand.objects.select_for_update().filter(tenant_id=run.tenant_id, pk=item["brand_id"]).first()
        if row is None or brand is None:
            raise Refusal("NOT_FOUND", "A source row or target brand was not found.")
        if label == "alerts.alert":
            from alerts.scope_contract import WHOLE_SITE_KINDS
            if row.kind in WHOLE_SITE_KINDS and not row.brand:
                raise Refusal("INVALID_SCOPE_ROW", "A whole-site alert cannot be assigned to one brand.")
        if fingerprint(row) != item["fingerprint"]:
            raise Refusal("STALE_RECONCILIATION", "Source data changed. Review the new report.", status=409)
        current = identity_id(row, run.tenant_id)
        source = _at(row, resource.source_brand) if resource.source_brand else None
        established = getattr(row, "brand_ref_id", None)
        binding = BrandIdentityBinding.objects.filter(
            tenant_id=run.tenant_id, resource_model=label, resource_id=row.pk,
        ).first()
        if (established is not None and established != brand.pk) or (
            binding is not None and binding.brand_id != brand.pk
        ) or (current is not None and current != brand.pk) or (
            source is not None and (source.tenant_id != run.tenant_id or source.pk != brand.pk)
        ):
            raise Refusal("IDENTITY_CONFLICT", "The selected brand conflicts with established source identity.")
        prepared.append((row, brand, current))
    outcomes = []
    for row, brand, _current in prepared:
        existing_binding = BrandIdentityBinding.objects.filter(
            tenant_id=run.tenant_id, resource_model=label, resource_id=row.pk,
        ).exists()
        if existing_binding or getattr(row, "brand_ref_id", None) is not None:
            outcomes.append({"id": row.pk, "outcome": "unchanged"})
            continue
        run.record(BrandIdentityBinding(
            resource_model=label, resource_id=row.pk, brand=brand,
            source_fingerprint=fingerprint(row),
            basis={"method": "source_relationship" if resource.source_brand and _at(row, resource.source_brand) is not None else "reviewed_rows",
                   "source_relationship": resource.source_brand,
                   "label_snapshot": getattr(row, "brand", "")},
        ))
        if not resource.immutable:
            row.brand_ref_id = brand.pk
            row.save(update_fields=["brand_ref_id"])
        outcomes.append({"id": row.pk, "outcome": "migrated"})
    run.audit_after = {"resource": label, "outcomes": outcomes}
    if any(item["outcome"] == "migrated" for item in outcomes):
        from accounts.goods_models import HumanIdentity
        from accounts.sessions import bump_security_epoch

        # Ownership changes affect every resource consumer, not just the operator.
        for human_id in HumanIdentity.objects.filter(tenant_id=run.tenant_id).order_by("pk").values_list("pk", flat=True):
            bump_security_epoch(human_id, run.tenant_id)
    return outcomes


def brand_predicate(query: Any, brand_field: str, tenant_id: Any, *, brandless: Q | None = None) -> tuple[Any, str]:
    """Use a real FK where present; otherwise use the reviewed legacy identity."""
    model = query.model
    parts = brand_field.removesuffix("_id").split("__")
    try:
        for part in parts:
            field = model._meta.get_field(part)
            if field.is_relation:
                model = field.related_model
        if field.is_relation and model is Brand:
            return query.filter(**{brand_field.removesuffix("_id") + "__tenant_id": tenant_id}), brand_field.removesuffix("_id") + "_id"
    except FieldDoesNotExist:
        pass
    return with_brand_identity(query, tenant_id, brandless=brandless), "_access_brand_id"
