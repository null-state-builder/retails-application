"""Current, non-overridable gates for the first online store.

This only reads the existing identities, stock writer and configuration spine.
It returns setup decisions, never protected financial values or customer data.
Approval records these decisions over the whole store. Every new online issue
checks the store-level ones again; the whole-store item scans (physical
acceptance totals, every piece's inputs and costing) run at approval and on the
readiness screen, while the issue path checks exactly those inputs for each
piece on the bill (``sell.services.online.check_lines_before_issue``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone

from core.refusals import Refusal, issue
from masters.goods_models import SiteGuard
from masters.models import Store


def _gate(key: str, passed: bool, reason: str) -> dict[str, Any]:
    return {"key": key, "passed": bool(passed), "required": True,
            "overridable": False, "reason": None if passed else reason}


def _opening_established(site: Store) -> bool:
    """The issue-time opening gate: an approved source whose every batch posted.

    ``is_reconciled`` also replays every official line's acceptance progress,
    which approval already required and which acceptance cannot undo. A later
    staged snapshot still leaves the established opening in force.
    """
    from ptmapper.soh_models import SohImport

    return SohImport.objects.filter(tenant_id=site.tenant_id, site_id=site.pk, state="applied",
                                    approved_by__isnull=False).exists()


def selling_checks(site: Store, now: datetime, *, whole_store: bool = True) -> list[dict[str, Any]]:
    from accounts.models import User
    from accounts.principal import access_for_user
    from accounts.registration_models import InstallationRegistration
    from masters.document_series import prefix_for_site
    from masters.goods_config import ConfigTarget, resolve
    from masters.goods_services import compute_readiness_checks
    from masters.store_features import is_feature_on
    from masters.tax_settings import in_force, saved_versions
    from ptmapper.soh_services import is_reconciled
    from ptmapper.soh_models import SohImport
    from sell.services.goods_stock import read_shelf
    from sell.services.online import sale_series_ready
    from sell.services.postings import resolve_goods_cost_plan
    from sell.services.till_authority import active_till
    from stockledger.goods_engine import eligible_portions_for_skus
    from stockledger.goods_models import Position

    guard = SiteGuard.objects.filter(site=site, tenant_id=site.tenant_id).first()
    setup = compute_readiness_checks(site, now)
    live = bool(guard and guard.lifecycle == SiteGuard.Lifecycle.ACTIVE
                and guard.goods_ready and not guard.freeze_id
                and guard.stock_contract == SiteGuard.StockContract.GOODS_V1)
    claim = InstallationRegistration.objects.filter(tenant_id=site.tenant_id,
                                                     first_store_id=site.pk,
                                                     completed_at__isnull=False).first()
    declared_new = bool(claim and claim.summary.get("store", {}).get("setup_kind") == "new")
    # The exact jointly confirmed new-store declaration establishes an empty
    # start. Once a source has been uploaded it needs the same review as any
    # existing store. Received stock still uses the governed receipt writer.
    empty_start = declared_new and not SohImport.objects.filter(
        tenant_id=site.tenant_id, site_id=site.pk).exclude(state="withdrawn").exists()
    checks = [
        _gate("goods_active", live, "Activate this goods store and resolve its stock freeze."),
        _gate("current_setup", all(row["passed"] for row in setup),
              "Complete the current legal, calendar, receiving and staff setup without unresolved gaps."),
        _gate("opening_reconciled",
              empty_start or (is_reconciled(site) if whole_store else _opening_established(site)),
              "Approve the source, post every opening batch and reconcile physical acceptance."),
    ]
    policy = None
    try:
        policy = resolve(site.tenant_id, "sell_policy", ConfigTarget.of(now, site_id=site.pk),
                         match={}, code="SELL_POLICY_REQUIRED", path="selling_policy")
    except Refusal:
        pass
    checks.append(_gate("selling_policy", policy is not None,
                        "Approve an effective selling policy covering this store."))
    tax_version = in_force(saved_versions(site.tenant_id), timezone.localdate(now), at=now)
    checks.append(_gate("tax_configuration", tax_version is not None
                        and is_feature_on(site, "tax-settings"),
                        "Save the reviewed tax rules and enable Tax Settings for this store."))
    # A new shop can use the currently supported format. It must deliberately
    # own a prefix; activation does not backdate or change a historical series.
    checks.append(_gate("invoice_prefix", prefix_for_site(site) is not None,
                        "Assign this store's invoice prefix in Document Numbering."))
    checks.append(_gate("bill_number_series", sale_series_ready(site),
                        "Register or renew this store's counter with its reconciled current-year bill series."))
    till = active_till(site)
    checks.append(_gate("registered_counter", till is not None,
                        "Register the store counter, then pair its browser in Till & Sync."))
    if not whole_store:
        users = User.objects.filter(tenant_id=site.tenant_id, is_active=True,
                                    human__active=True, must_change_password=False)
        checks.append(_gate("authorised_cashier",
                            any(access_for_user(user).covers_all({"section.sell.operate"}, {(site.pk, None)}, [])
                                for user in users),
                            "Activate a store-scoped cashier and complete their first password change."))
        return checks
    shelf = read_shelf(site, now)
    positive = [piece for piece in shelf.pieces
                if shelf.quantities.get((piece.barcode, piece.season), 0) > 0]
    # Include current stock whose alias was later withdrawn or became ambiguous.
    # A narrowed shelf alone cannot prove that all eligible stock is represented.
    stocked_skus = Position.objects.filter(
        tenant_id=site.tenant_id, site_id=site.pk, boundary="physical",
        sku_id__isnull=False,
    ).values_list("sku_id", flat=True).distinct()
    eligible = eligible_portions_for_skus(site.pk, stocked_skus, purpose="sell")
    eligible_skus = {item.address.sku_id for item in eligible}
    complete = all(piece.brand_id is not None and piece.mrp_paise is not None
                   and piece.mrp_paise > 0 and piece.hsn.isdigit()
                   and len(piece.hsn) in (4, 6, 8) for piece in positive)
    complete = complete and eligible_skus <= {piece.sku_id for piece in positive}
    checks.append(_gate("sellable_item_inputs", complete,
                        "Resolve stable item/brand identities, positive MRP and reviewed HSN for sellable stock."))
    # Use the sale writer's exact costing decision on every eligible source
    # portion, including old layers with a different cost. Only the boolean may
    # leave this read; no source value is added to the till or setup response.
    from masters.models import Brand
    brand_by_sku = {piece.sku_id: piece.brand_id for piece in positive}
    brands = Brand.objects.filter(tenant_id=site.tenant_id,
                                  pk__in={brand for brand in brand_by_sku.values() if brand is not None}).in_bulk()
    postable = all(resolve_goods_cost_plan(brand_id=brand_by_sku.get(item.address.sku_id) if item.address.sku_id is not None else None,
                                          unit_cost_paise=int(item.unit_cost or 0),
                                          brands=brands).postable for item in eligible)
    checks.append(_gate("source_costing", postable,
                        "Resolve accepted stock ownership and its supported supplier/cost links before selling."))
    cells = {(site.pk, piece.brand_id) for piece in positive} or {(site.pk, None)}
    users = User.objects.filter(tenant_id=site.tenant_id, is_active=True,
                                human__active=True, must_change_password=False)
    cashier = any(access_for_user(user).covers_all({"section.sell.operate"}, cells, [])
                  for user in users)
    checks.append(_gate("authorised_cashier", cashier,
                        "Activate a store-scoped cashier and complete their first password change."))
    return checks


def require_ready(site: Store, now: datetime) -> None:
    guard = SiteGuard.objects.filter(site=site, tenant_id=site.tenant_id).first()
    if guard is None or guard.selling_mode != SiteGuard.SellingMode.ONLINE_ALPHA or not guard.sell_ready:
        raise Refusal("SELL_NOT_READY", "Approve this store's online selling readiness first.", status=409)
    failed = [row for row in selling_checks(site, now, whole_store=False) if not row["passed"]]
    if failed:
        raise Refusal("SELL_NOT_READY", "The store no longer meets its approved selling conditions.",
                      status=409, issues=[issue("SETUP_REQUIRED", row["reason"], field=row["key"])
                                          for row in failed])
