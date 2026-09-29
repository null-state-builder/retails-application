"""``SessionDTO`` (design §6.1) plus the legacy shell profile.

The goods screens read ``user``/``roles``/``sites``/``actions``/``field_grants``;
the existing shell still draws its sidebar from the profile the old login
returned, so that rides along under ``profile``. No bearer token is ever part of
this payload.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from accounts.principal import effective_grants
from accounts.serializers import UserProfileSerializer
from accounts.sessions import step_up_valid_until


def session_payload(user: Any, session: Any, *, csrf_token: str | None = None) -> dict[str, Any]:
    from masters.brand_terms import FEATURE_KEY as BRAND_TERMS
    from masters.brand_terms import brand_terms_sites
    from masters.goods_models import Sbu, SiteGuard
    from masters.models import Store
    from masters.scoping import actionable_stores
    from masters.store_features import features_on_by_site

    grants = effective_grants(user.human_id) if user.human_id else []
    actions: set[str] = set()
    fields: set[str] = set()
    roles: set[str] = set()
    site_ids: set[int] = set()
    all_sites = False
    sbu_ids: set[str] = set()
    for grant in grants:
        actions |= grant.actions
        fields |= grant.fields
        roles.add(grant.role_code)
        if grant.scope_kind in ("tenant", "brand"):
            all_sites = True
        elif grant.scope_kind == "entity" and grant.entity_id is not None:
            site_ids |= set(
                Store.objects.filter(gstin__legal_entity_id=grant.entity_id).values_list(
                    "id", flat=True
                )
            )
        elif grant.scope_kind == "site" and grant.site_id is not None:
            site_ids.add(grant.site_id)
        elif grant.scope_kind == "sbu" and grant.sbu_id is not None:
            sbu_ids.add(str(grant.sbu_id))
            if grant.sbu_site_id is not None:
                site_ids.add(grant.sbu_site_id)
    stores = Store.objects.all() if all_sites else Store.objects.filter(id__in=site_ids)
    if all_sites:
        sbu_ids |= {str(pk) for pk in Sbu.objects.values_list("id", flat=True)}
    stores = stores.order_by("code")
    # Which stock system each site runs on, so a form offers only the sites one
    # booking can reach. It describes the sites already listed, nothing more.
    contracts = dict(
        SiteGuard.objects.filter(site_id__in=[store.id for store in stores]).values_list(
            "site_id", "stock_contract"
        )
    )
    human = user.human
    payload: dict[str, Any] = {
        "user": {
            "id": str(user.pk),
            "human_id": str(user.human_id) if user.human_id else None,
            "display_name": human.display_name if human else (user.full_name or user.username),
            "email": user.email,
            # GSA-T03/ticket 03A: true until this login's own change-password
            # succeeds. A restricted session's `ServerSessionAuthentication`
            # gate answers every other write with `PASSWORD_CHANGE_REQUIRED`.
            "must_change_password": bool(getattr(user, "must_change_password", False)),
        },
        "roles": sorted(roles),
        "sites": [
            # GSA-T07: a store's own kind, so a stock reader can tell a
            # warehouse row from a store row (R28: a warehouse reports
            # transferable, never sellable) without guessing it back from
            # which quantity a row happens to carry — a fully-held warehouse
            # row carries neither, and guessing from quantities alone reads
            # that as a store.
            {
                "id": str(store.id),
                "code": store.code,
                "name": store.name,
                "type": store.store_type,
                "stock_contract": contracts.get(store.id, SiteGuard.StockContract.LEGACY),
            }
            for store in stores
        ],
        "sbus": sorted(sbu_ids),
        "actions": sorted(actions),
        "field_grants": sorted(fields),
        "expires_at": session.expires_at.isoformat() if session is not None else None,
        "step_up_valid_until": (
            until.isoformat() if (until := step_up_valid_until(session, timezone.now())) else None
        ),
        # ST-OPS-6: which switchable features are on where this person works,
        # so the menus hide a feature that is off. The server still refuses it.
        # Over the stores the server's own check reaches (`actionable_stores`),
        # so a menu can never disagree with the refusal behind it.
        "store_features": features_on_by_site(s.id for s in actionable_stores(user)),
        "profile": UserProfileSerializer(user).data,
    }
    # Ticket 23: brand terms are a brand manager's work across every store, so the
    # Brands menu reads the stores the server's own check counts for this person
    # (`masters.brand_terms.switch_stores`), not only the stores they act at.
    brand_sites = brand_terms_sites(user)
    if brand_sites:
        payload["store_features"][BRAND_TERMS] = brand_sites
    else:
        payload["store_features"].pop(BRAND_TERMS, None)
    if csrf_token is not None:
        payload["csrf_token"] = csrf_token
    return payload
