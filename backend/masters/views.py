"""Master-data API: scoped reads for the foundation switcher + steward-gated
create/edit (the D8 stewardship slice). Reads stay open to any authenticated
user; writes require a master-data steward (owner / IT admin / data steward).
One read is deliberately *un*scoped - `LocationListView`, whose docstring says
why - and it pays for that by carrying identity fields and nothing else.
Records are deactivated (`is_active`), never hard-deleted - masters are referenced
by append-only ledger rows.

One master here is not a steward's: `StoreTargetView`, the store x month sales
target (#171). Choosing what a store must sell is a Money act, so it answers to
the Money section rather than to the stewardship rule above, and its docstring
carries the argument.
"""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import require_section
from accounts.principal import resolve_access
from accounts.sections import CAP_MANAGE, CAP_VIEW
from core.fiscal import financial_year_months
from core.refusals import Refusal, first_message, refusal_body
from core.textsearch import search_term, text_filter
from core.tenancy import require_tenant_id
from masters.models import Brand, Gstin, LegalEntity, Season, Store, StoreTarget

# Re-exported: the gate moved to `masters.permissions` so the vendor master —
# which lives in `vendors` because it carries bookings — is gated by the same
# rule. Imported here so `from masters.views import IsMasterSteward` still reads.
from masters.permissions import IsMasterSteward
from masters.scoping import scope_by_entitlement, scoped_brands, scoped_stores
from masters.serializers import (
    BrandSerializer,
    GstinSerializer,
    LegalEntitySerializer,
    LocationSerializer,
    SeasonSerializer,
    StoreSerializer,
    StoreTargetSerializer,
    StoreTargetWriteSerializer,
)

#: Master data lists (#106) — every one of them searches by name / code.
STORE_SEARCH_FIELDS = ("code", "name")
BRAND_SEARCH_FIELDS = ("code", "name")
SEASON_SEARCH_FIELDS = ("code", "name")
#: `Gstin` carries no name/code pair — the number itself, its state, and the
#: legal entity it's registered under are the nearest equivalent.
GSTIN_SEARCH_FIELDS = ("gstin", "state_name", "legal_entity__name", "legal_entity__code")

# --- Stores --------------------------------------------------------------


class StoreListView(generics.ListCreateAPIView[Store]):
    serializer_class = StoreSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]

    def get_queryset(self) -> Any:
        qs = scoped_stores(self.request.user, section="setup", minimum="view").select_related("gstin", "goods_guard")
        return text_filter(qs, search_term(self.request), STORE_SEARCH_FIELDS)

    def perform_create(self, serializer: BaseSerializer[Store]) -> None:
        access = resolve_access(self.request)
        access.require("org.site.manage")
        with access.guard_legacy_write(lambda fresh: fresh.can("org.site.manage")):
            serializer.save()


class LocationListView(generics.ListAPIView[Store]):
    """Every active location in the network, identity fields only — the list of
    places stock may be *sent* to.

    Deliberately unscoped, unlike `StoreListView` above. That one answers "which
    units may I operate on", which is the right question for the *source* of a
    transfer and the wrong one for its *destination*: sending a carton somewhere
    claims no rights at the place it is going. Scoping both alike left every
    store person with an empty destination picker and no way to start a transfer
    at all (#147). What guards a store sending anywhere is the e-way bill the
    screen demands across registrations, plus the Operations Head approval gate
    (PRD #104) — a picker is not a permission and must not become one.
    """

    serializer_class = LocationSerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self) -> Any:
        resolve_access(self.request).require_action("org.site.route")
        return Store.objects.filter(
            tenant_id=require_tenant_id(), is_active=True
        ).select_related("gstin").order_by("code")


class StoreDetailView(generics.RetrieveUpdateAPIView[Store]):
    serializer_class = StoreSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]
    def get_queryset(self) -> Any:
        return scoped_stores(self.request.user, section="setup", minimum="view").select_related("gstin", "goods_guard")

    def perform_update(self, serializer: BaseSerializer[Store]) -> None:
        access = resolve_access(self.request)
        instance = serializer.instance
        assert instance is not None
        site_id = instance.pk
        access.require("org.site.manage", site_id=site_id)
        with access.guard_legacy_write(
            lambda fresh: fresh.can("org.site.manage", site_id=site_id)
        ):
            serializer.save()


# --- Brands --------------------------------------------------------------


class BrandListView(generics.ListCreateAPIView[Brand]):
    serializer_class = BrandSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]

    def get_queryset(self) -> Any:
        qs = scoped_brands(self.request.user, section="setup", minimum="view").filter(is_active=True)
        return text_filter(qs, search_term(self.request), BRAND_SEARCH_FIELDS)

    def perform_create(self, serializer: BaseSerializer[Brand]) -> None:
        access = resolve_access(self.request)
        access.require("vendor.manage")
        with access.guard_legacy_write(lambda fresh: fresh.can("vendor.manage")):
            serializer.save()


class BrandDetailView(generics.RetrieveUpdateAPIView[Brand]):
    serializer_class = BrandSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]
    def get_queryset(self) -> Any:
        return scoped_brands(self.request.user, section="setup", minimum="view")

    def perform_update(self, serializer: BaseSerializer[Brand]) -> None:
        access = resolve_access(self.request)
        instance = serializer.instance
        assert instance is not None
        brand_id = instance.pk
        access.require("vendor.manage", brand_id=brand_id)
        with access.guard_legacy_write(
            lambda fresh: fresh.can("vendor.manage", brand_id=brand_id)
        ):
            serializer.save()


# --- Seasons -------------------------------------------------------------


class SeasonListView(generics.ListCreateAPIView[Season]):
    serializer_class = SeasonSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]

    def get_queryset(self) -> Any:
        resolve_access(self.request).require_action("org.site.route")
        qs = Season.objects.all()
        return text_filter(qs, search_term(self.request), SEASON_SEARCH_FIELDS)

    def perform_create(self, serializer: BaseSerializer[Season]) -> None:
        raise Refusal(
            "TENANT_SCOPE_UNRESOLVED",
            "Legacy seasons need tenant ownership before they can be changed.",
            status=409,
        )


class SeasonDetailView(generics.RetrieveUpdateAPIView[Season]):
    serializer_class = SeasonSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]
    def get_queryset(self) -> Any:
        resolve_access(self.request).require_action("org.site.route")
        return Season.objects.all()

    def perform_update(self, serializer: BaseSerializer[Season]) -> None:
        raise Refusal(
            "TENANT_SCOPE_UNRESOLVED",
            "Legacy seasons need tenant ownership before they can be changed.",
            status=409,
        )


# --- GSTINs --------------------------------------------------------------


class GstinListView(generics.ListCreateAPIView[Gstin]):
    serializer_class = GstinSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]

    def get_queryset(self) -> Any:
        qs = Gstin.objects.select_related("legal_entity").filter(
            tenant_id=require_tenant_id(), is_active=True,
            pk__in=scoped_stores(self.request.user, section="setup", minimum="view").values("gstin_id"),
        )
        return text_filter(qs, search_term(self.request), GSTIN_SEARCH_FIELDS)

    def perform_create(self, serializer: BaseSerializer[Gstin]) -> None:
        access = resolve_access(self.request)
        access.require("org.entity.manage")
        with access.guard_legacy_write(lambda fresh: fresh.can("org.entity.manage")):
            serializer.save()


class GstinDetailView(generics.RetrieveUpdateAPIView[Gstin]):
    serializer_class = GstinSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]
    def get_queryset(self) -> Any:
        return Gstin.objects.select_related("legal_entity").filter(
            tenant_id=require_tenant_id(),
            pk__in=scoped_stores(self.request.user, section="setup", minimum="view").values("gstin_id"),
        )

    def perform_update(self, serializer: BaseSerializer[Gstin]) -> None:
        access = resolve_access(self.request)
        access.require("org.entity.manage")
        with access.guard_legacy_write(lambda fresh: fresh.can("org.entity.manage")):
            serializer.save()


# --- Legal entities ------------------------------------------------------


class LegalEntityListView(generics.ListAPIView[LegalEntity]):
    serializer_class = LegalEntitySerializer
    permission_classes = [IsAuthenticated]
    def get_queryset(self) -> Any:
        return LegalEntity.objects.filter(
            tenant_id=require_tenant_id(), is_active=True,
            pk__in=scoped_stores(self.request.user, section="setup", minimum="view").values("gstin__legal_entity_id"),
        )


class SkuLookupView(APIView):
    """Registry reuse at authoring time (D2 Q16/Q41): has this vendor+style+size been
    seen before? Exact-match filters over the SKU master; the PT editor shows "seen
    before — reusing barcode X" and copies barcode/colour/HSN/MRP from the registry
    instead of minting a duplicate identity. Pure read.

    ``GET /masters/skus/lookup?design=&size=&brand=&barcode=`` — at least one of
    ``design``/``barcode`` is required (never dump the registry)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "required": ["matches", "count"],
                "properties": {
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "barcode": {"type": "string"},
                                "design": {"type": "string"},
                                "color": {"type": "string"},
                                "size": {"type": "string"},
                                "brand": {"type": "string"},
                                "item": {"type": "string"},
                                "hsn": {"type": "string"},
                                "mrp": {"type": "number", "nullable": True},
                                "first_doc_number": {"type": "string", "nullable": True},
                            },
                        },
                    },
                    "count": {"type": "integer"},
                },
            }
        }
    )
    def get(self, request: Request) -> Response:
        resolve_access(request).require_action("product.master.propose")
        params = {
            key: (request.query_params.get(key) or "").strip()
            for key in ("design", "size", "brand", "barcode")
        }
        if not (params["design"] or params["barcode"]):
            return Response({"detail": "Pass design= or barcode= to look up."}, status=400)
        # The legacy Sku table has no tenant key. A barcode or matching display
        # brand cannot establish ownership. An empty result would invite a
        # duplicate barcode, so the old lookup refuses until SO-04 maps it.
        raise Refusal(
            "TENANT_SCOPE_UNRESOLVED",
            "Use the tenant-owned product lookup after legacy SKU mapping.",
            status=409,
        )


# --- Store targets -------------------------------------------------------


#: Setting a store's monthly number is a Money act, not a Setup one (#171). The
#: matrix puts the cell at `money: manage` and the D10 grill made *which* role
#: holds it admin-editable data (Rule 12), so this reads the same
#: `Role.section_access` the sidebar reads and nothing here names a role.
#: Reading stays at `view`, the rung a store person holds ("Expenses only"), so a
#: store may see the number it is judged against without being able to move it.
CanReadOrSetStoreTarget = require_section("money", CAP_VIEW, write_minimum=CAP_MANAGE)


class StoreTargetView(APIView):
    """`GET | PUT /api/masters/store-targets` - the store x month target grid.

    A master, so a PUT *is* the whole write: `(store, month)` is unique and
    setting a target again corrects it. There is no delete, because a store's
    month always has a number even when that number is nought.

    Both verbs gate on **entitlement**, not on the top-bar switcher, and that is
    the one thing about this view worth reading twice.

    The grid's rows come from the store master (`scoped_stores`, which says in its
    own docstring that it is "deliberately *not* narrowed by the active unit").
    Its cells come from here. Gate the two differently and they disagree: an
    Operations Head with Deoghar picked in the top bar would get all fifty store
    rows with only Deoghar's numbers in them, every other cell reading as "no
    target set" for a target that exists, and the year's total quietly collapsing
    to one store. A screen that hides committed money behind a header nobody
    thought they were filtering with is worse than one that refuses.

    So this endpoint follows the master it is keyed on rather than the reading
    convention for documents: one financial year of targets is a single HO
    decision, and you do not look at one store's column of it at a time. Scope is
    still the boundary - a store-scoped caller sees their own store and no other,
    which is the acceptance criterion - the switcher simply gets no vote. Callers
    who want one store ask for it by name with `?store=`.
    """

    permission_classes = [IsAuthenticated, CanReadOrSetStoreTarget]

    @extend_schema(responses={200: StoreTargetSerializer(many=True)})
    def get(self, request: Request) -> Response:
        rows = scope_by_entitlement(
            StoreTarget.objects.select_related("store"), request.user, "store_id"
        , section="money", minimum="view")
        code = (request.query_params.get("store") or "").strip()
        if code:
            # Narrows *within* scope: the filter is applied after the gate, so
            # naming another store's code answers with nothing rather than with
            # that store's target.
            rows = rows.filter(store__code__iexact=code)
        fy = (request.query_params.get("fy") or "").strip()
        if fy:
            try:
                months = financial_year_months(fy)
            except ValueError as exc:
                return Response(refusal_body("VALIDATION", str(exc)), status=400)
            rows = rows.filter(month__in=months)
        return Response(StoreTargetSerializer(rows, many=True).data)

    @extend_schema(request=StoreTargetWriteSerializer, responses={200: StoreTargetSerializer})
    def put(self, request: Request) -> Response:
        form = StoreTargetWriteSerializer(data=request.data)
        if not form.is_valid():
            return Response(refusal_body("VALIDATION", first_message(form.errors)), status=400)
        code = form.validated_data["store"]
        store = Store.objects.filter(
            tenant_id=require_tenant_id(), code__iexact=code, is_active=True
        ).first()
        if store is None:
            # A closed store is not a store to plan against, so it answers the
            # same as one that never existed. The contract says only "store must
            # exist"; refusing the closed ones too is deliberate.
            return Response(
                refusal_body("NOT_FOUND", f"No active store with code '{code}'."), status=404
            )
        # Beyond the contract's own steps, deliberately: `money: manage` says what
        # a person may do, never where. Without this, one rung would set targets
        # for stores the admin never entitled them to.
        entitled = scope_by_entitlement(Store.objects.filter(pk=store.pk), request.user, "id", section="money", minimum="manage")
        if not entitled.exists():
            return Response(
                refusal_body("SCOPE_DENIED", f"{store.code} is not one of your locations."),
                status=403,
            )
        target, _ = StoreTarget.objects.update_or_create(
            store=store,
            month=form.validated_data["month"],
            defaults={
                "target_paise": form.validated_data["target_paise"],
                # `IsAuthenticated` in `permission_classes` guarantees a real
                # user by the time this line runs - never `AnonymousUser`.
                "set_by": cast(User, request.user),
            },
        )
        return Response(StoreTargetSerializer(target).data)


class SummaryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "required": [
                    "entities", "gstins", "stores", "warehouses", "brands", "seasons", "open_season",
                ],
                "properties": {
                    "entities": {"type": "integer"},
                    "gstins": {"type": "integer"},
                    "stores": {"type": "integer"},
                    "warehouses": {"type": "integer"},
                    "brands": {"type": "integer"},
                    "seasons": {"type": "integer"},
                    "open_season": {"type": "string", "nullable": True},
                },
            }
        }
    )
    def get(self, request: Request) -> Response:
        resolve_access(request).require_action("org.site.route")
        stores = scoped_stores(request.user, section="setup", minimum="view")
        gstins = Gstin.objects.filter(
            tenant_id=require_tenant_id(), pk__in=stores.values("gstin_id")
        )
        return Response(
            {
                "entities": LegalEntity.objects.filter(
                    tenant_id=require_tenant_id(), is_active=True,
                    pk__in=gstins.values("legal_entity_id"),
                ).count(),
                "gstins": gstins.filter(is_active=True).count(),
                "stores": stores.count(),
                "warehouses": stores.filter(store_type=Store.StoreType.WAREHOUSE).count(),
                "brands": scoped_brands(request.user, section="setup", minimum="view").filter(is_active=True).count(),
                # Legacy Season has no tenant key. SO-04 must map it before a
                # tenant-specific count or open-season label can be shown.
                "seasons": 0,
                "open_season": None,
            }
        )
