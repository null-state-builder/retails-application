"""One server-derived session description for navigation and context choices.

The display hints are never used by the server as authority.  Every resource
request is checked against the current role assignment and policy again.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Max
from django.utils import timezone

from accounts.permissions import visible_sections
from accounts.principal import effective_grants
from accounts.role_assignments import effective_assignments
from accounts.sessions import step_up_valid_until
from core.canonical import sha256_hex


def session_payload(user: Any, session: Any, *, csrf_token: str | None = None) -> dict[str, Any]:
    from masters.goods_models import MasterVersion, SiteGuard
    from masters.models import Brand, Store
    from masters.store_features import features_on_by_site
    from accounts.unified_policy import WORKFLOW_TARGET_KEY
    from accounts.sessions import IDLE_LIFE
    from accounts.till_pin import may_hold_till_pin, may_set_personal_till_pin

    assignments = effective_assignments(user.human_id) if user.human_id else []
    grants = effective_grants(user.human_id) if user.human_id else []
    all_sites = any(row.all_sites for row in assignments)
    all_brands = any(row.all_brands for row in assignments)
    all_units = any(row.all_sites and row.all_brands for row in assignments)
    site_ids = {int(pk) for row in assignments for pk in row.site_ids}
    brand_ids = {int(pk) for row in assignments for pk in row.brand_ids}
    tenant_stores = Store.objects.filter(tenant_id=user.tenant_id, is_active=True)
    tenant_brands = Brand.objects.filter(tenant_id=user.tenant_id, is_active=True)
    stores = list((tenant_stores if all_sites else tenant_stores.filter(pk__in=site_ids)).order_by("code"))
    brands = list((tenant_brands if all_brands else tenant_brands.filter(pk__in=brand_ids)).order_by("code"))
    contracts = dict(SiteGuard.objects.filter(site_id__in=[s.pk for s in stores]).values_list("site_id", "stock_contract"))
    store_choices = [
        {
            "id": store.pk,
            "code": store.code,
            "name": store.name,
            "store_type": store.store_type,
            "state_name": store.gstin.state_name,
            "state_code": store.gstin.state_code,
            "gstin_number": store.gstin.gstin,
        }
        for store in stores
    ]
    brand_choices = [{"id": brand.pk, "code": brand.code, "name": brand.name} for brand in brands]
    sections = visible_sections(user)
    capabilities = {str(entry["code"]): str(entry["capability"]) for entry in sections}
    display_actions = {action for grant in grants for action in grant.actions}
    # A policy change updates Role.updated_at; an assignment change changes its
    # membership or effective period.  The digest is a cache validator, never
    # a credential or authorisation token.
    workflow_revision = (
        MasterVersion.objects.filter(
            tenant_id=user.tenant_id, kind="tenant", target_key=WORKFLOW_TARGET_KEY
        ).aggregate(latest=Max("revision"))["latest"] or 0
    )
    version_material = f"workflow:{workflow_revision}|" + "|".join(
        sorted(
            f"{row.pk}:{row.role.updated_at}:{row.effective_from}:{row.effective_to}:"
            f"{row.all_sites}:{sorted(row.site_ids)}:{row.all_brands}:{sorted(row.brand_ids)}"
            for row in assignments
        )
    )
    human = user.human
    payload: dict[str, Any] = {
        "contract_version": "access-v2",
        "policy_version": sha256_hex(version_material),
        "user": {
            "id": str(user.pk),
            "human_id": str(user.human_id) if user.human_id else None,
            "display_name": human.display_name if human else (user.full_name or user.username),
            "email": user.email,
            "must_change_password": bool(getattr(user, "must_change_password", False)),
            "has_till_pin": bool(user.till_pin_hash),
            "may_hold_till_pin": may_hold_till_pin(user),
            "may_set_till_pin": may_set_personal_till_pin(user),
        },
        "assignments": [
            {
                "id": str(row.pk),
                "role_code": row.role.code,
                "all_sites": row.all_sites,
                "site_ids": sorted(int(pk) for pk in row.site_ids),
                "all_brands": row.all_brands,
                "brand_ids": sorted(int(pk) for pk in row.brand_ids),
                "effective_from": row.effective_from.isoformat(),
                "effective_to": row.effective_to.isoformat() if row.effective_to else None,
            }
            for row in assignments
        ],
        "navigation": [str(entry["code"]) for entry in sections],
        "sections": sections,
        "capabilities": capabilities,
        "display_actions": sorted(display_actions),
        "context_choices": {
            "mode": "brands" if assignments and all(row.all_sites and not row.all_brands for row in assignments) else "units",
            "all_units": all_units,
            "sites": store_choices,
            "brands": brand_choices,
        },
        "sites": [
            {
                "id": str(store.pk), "code": store.code, "name": store.name,
                "type": store.store_type,
                "stock_contract": contracts.get(store.pk, SiteGuard.StockContract.LEGACY),
            }
            for store in stores
        ],
        "store_features": features_on_by_site(store.pk for store in stores),
        "expires_at": min(session.expires_at, session.last_seen_at + IDLE_LIFE).isoformat() if session is not None else None,
        "step_up_valid_until": (
            until.isoformat() if (until := step_up_valid_until(session, timezone.now())) else None
        ),
    }
    if csrf_token is not None:
        payload["csrf_token"] = csrf_token
    return payload
