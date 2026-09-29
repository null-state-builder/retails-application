"""Stock > Stock Ageing (store operations ticket 33, ST-INV-2).

``GET  /api/goods-v1/stock/ageing[?site_id=]``  the store's stock aged against its
                                                season (``stockledger.goods_ageing``)
``POST /api/goods-v1/stock/ageing/season-end``  record the day a season ended

Readers hold ``stock: view`` or higher, for the selling stores in their own scope
where the ``season-ageing`` switch is on. A store outside that is not found. The
read changes nothing and carries no cost or margin.

Legacy seasons lack a tenant owner. Their names remain shared reference data
until SO-04 reconciles them; end-date writes are blocked at this route.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    check_query,
    parse_int_id,
)
from accounts.permissions import user_can, user_can_at
from accounts.sections import CAP_VIEW
from core.refusals import Refusal
from masters.models import Season, Store
from masters.scoping import actionable_stores
from masters.store_feature_registry import SEASON_AGEING
from masters.store_features import feature, switch_states
from stockledger.goods_ageing import GROUPS, idle_days_policy, store_ageing

SEASON_END_ACTION = "masters.season.end_date"


class AgeingStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class AgeingRowSerializer(serializers.Serializer[Any]):
    origin_id = serializers.CharField(help_text="With arrived_here_on, names the row.")
    sku_id = serializers.CharField()
    barcode = serializers.CharField(allow_blank=True)
    brand = serializers.CharField(allow_blank=True)
    item = serializers.CharField(allow_blank=True)
    design = serializers.CharField(allow_blank=True)
    size = serializers.CharField(allow_blank=True)
    colour = serializers.CharField(allow_blank=True)
    season_code = serializers.CharField(allow_blank=True)
    season_label = serializers.CharField(allow_blank=True)
    group = serializers.ChoiceField(choices=list(GROUPS))
    qty = serializers.IntegerField()
    first_arrived_on = serializers.DateField(
        allow_null=True, help_text="First arrival in the company; null when unknown."
    )
    days_in_company = serializers.IntegerField(allow_null=True)
    in_company_before = serializers.DateField(
        allow_null=True,
        help_text="Opening stock with no recorded arrival: it was here before this day.",
    )
    arrived_here_on = serializers.DateField()
    days_here = serializers.IntegerField()
    last_sale_on = serializers.DateField(
        allow_null=True, help_text="The item's last sale at this store."
    )
    since = serializers.DateField(allow_null=True, help_text="The day the age counts from.")
    age_days = serializers.IntegerField(allow_null=True)
    aged = serializers.BooleanField()


class AgeingGroupSerializer(serializers.Serializer[Any]):
    group = serializers.ChoiceField(choices=list(GROUPS))
    qty = serializers.IntegerField()
    aged_qty = serializers.IntegerField()
    items = serializers.IntegerField()
    oldest_days = serializers.IntegerField(allow_null=True)


class AgeingSeasonSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()
    status = serializers.CharField()
    ended_on = serializers.DateField(allow_null=True)
    historical_unknown = serializers.BooleanField()


class StockAgeingSerializer(serializers.Serializer[Any]):
    stores = AgeingStoreSerializer(many=True)
    site_id = serializers.IntegerField(allow_null=True)
    today = serializers.DateField()
    idle_days = serializers.IntegerField(
        allow_null=True, help_text="In-season days with no sale that flag a piece."
    )
    goods_records = serializers.BooleanField(
        help_text="False: this store's stock is not on the goods records, so it cannot be aged."
    )
    groups = AgeingGroupSerializer(many=True)
    rows = AgeingRowSerializer(many=True)
    seasons = AgeingSeasonSerializer(many=True)
    can_set_season_end = serializers.BooleanField()


class SeasonEndRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    contract_version = serializers.ChoiceField(choices=["goods-v1"])
    season_id = serializers.IntegerField()
    ended_on = serializers.DateField(allow_null=True, help_text="Null clears the end date.")


def ageing_stores(user: Any) -> list[Store]:
    """Stores where one assignment covers the report's complete brand scope."""
    stores = [
        store for store in actionable_stores(user, section="stock").filter(
            store_type=Store.StoreType.STORE
        )
        if user_can_at(user, "stock", CAP_VIEW, site_id=store.pk, brand_id=None)
    ]
    states = switch_states(stores, [feature(SEASON_AGEING)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


def season_json(season: Season) -> dict[str, Any]:
    return {
        "id": season.pk,
        "code": season.code,
        "name": season.name,
        "status": season.status,
        "ended_on": season.ended_on,
        "historical_unknown": season.historical_unknown,
    }


class GoodsStockAgeingView(GoodsAPIView):
    """The store's stock aged against its season (``stock: view``, in scope, switch on)."""

    @extend_schema(
        parameters=[OpenApiParameter("site_id", int, required=False)],
        responses=StockAgeingSerializer,
    )
    def get(self, request: Request) -> Response:
        self.access(request)
        params = check_query(request, allowed=("site_id",))
        if not user_can(request.user, "stock", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Stock.")
        stores = ageing_stores(request.user)
        chosen: Store | None = stores[0] if stores else None
        if params.get("site_id"):
            wanted = parse_int_id(params["site_id"], "site_id")
            chosen = next((store for store in stores if store.pk == wanted), None)
            if chosen is None:
                raise Refusal(
                    "NOT_FOUND", "That store's stock ageing is not available.", status=404
                )
        today = timezone.localdate()
        idle = idle_days_policy()
        report = store_ageing(chosen, today, idle) if chosen is not None else None
        body = {
            "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in stores],
            "site_id": chosen.pk if chosen is not None else None,
            "today": today,
            "idle_days": idle,
            "goods_records": report.goods_records if report else True,
            "groups": [total.as_json() for total in report.totals()] if report else [],
            "rows": [row.as_json() for row in report.rows] if report else [],
            "seasons": [season_json(s) for s in Season.objects.order_by("-sort_order", "code")]
            if stores
            else [],
            "can_set_season_end": False,
        }
        return Response(StockAgeingSerializer(body).data)


class GoodsSeasonEndView(GoodsAPIView):
    """Block changes to a shared legacy season until SO-04 maps its tenant owner."""

    @extend_schema(request=SeasonEndRequestSerializer, responses=AgeingSeasonSerializer)
    def post(self, request: Request) -> Response:
        self.access(request)
        raise Refusal(
            "TENANT_SCOPE_UNRESOLVED",
            "Map this season to a tenant-owned master before editing its end date.",
            status=409,
        )
