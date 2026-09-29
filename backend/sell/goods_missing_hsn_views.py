"""Stock > Items with no HSN (store operations ticket 12, §6 ST-CMP-3).

``GET /api/goods-v1/sell/missing-hsn[?site_id=]`` - the items a store's counter
holds whose HSN is blank or not an HSN, with how each is fixed
(``sell.services.missing_hsn``).

Readers hold ``stock: view`` or higher, for the selling stores in their own scope
where the ``hsn-on-every-item`` switch is on (B24's rule for a new screen). A
store outside that is not found. A read: it changes nothing and leaves no audit
record. The fix itself goes through the PT's own reverse-and-reissue, which is
audited there.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, check_query, parse_int_id
from accounts.permissions import user_can
from accounts.sections import CAP_VIEW
from core.refusals import Refusal
from masters.hsn import FEATURE_KEY
from masters.models import Store
from masters.scoping import actionable_stores
from masters.store_features import feature, switch_states
from sell.services.missing_hsn import missing_hsn_items


class MissingHsnStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class MissingHsnRowSerializer(serializers.Serializer[Any]):
    barcode = serializers.CharField()
    season = serializers.CharField(allow_blank=True)
    design = serializers.CharField(allow_blank=True)
    brand = serializers.CharField(allow_blank=True)
    item = serializers.CharField(allow_blank=True)
    size = serializers.CharField(allow_blank=True)
    color = serializers.CharField(allow_blank=True)
    hsn = serializers.CharField(allow_blank=True, help_text="What the records hold now.")
    qty = serializers.IntegerField(help_text="Pieces the counter could sell here now.")
    fix = serializers.ChoiceField(
        choices=["reissue_pt", "no_audited_path"],
        help_text="reissue_pt: reverse and reissue the PT named; no_audited_path: none built yet.",
    )
    pt_document_id = serializers.CharField(allow_null=True)
    pt_number = serializers.CharField(allow_blank=True)


class MissingHsnSerializer(serializers.Serializer[Any]):
    stores = MissingHsnStoreSerializer(many=True)
    site_id = serializers.IntegerField(allow_null=True)
    items = MissingHsnRowSerializer(many=True)


def missing_hsn_stores(user: Any) -> list[Store]:
    """The selling stores in this person's scope where the switch is on."""
    stores = list(actionable_stores(user, section="stock").filter(store_type=Store.StoreType.STORE))
    states = switch_states(stores, [feature(FEATURE_KEY)])
    return [store for store, state in zip(stores, states, strict=True) if state.enabled]


class GoodsMissingHsnView(GoodsAPIView):
    @extend_schema(responses=MissingHsnSerializer)
    def get(self, request: Request) -> Response:
        self.access(request)
        params = check_query(request, allowed=("site_id",))
        if not user_can(request.user, "stock", CAP_VIEW):
            raise Refusal("ACTION_DENIED", "You do not have access to Stock.")
        stores = missing_hsn_stores(request.user)
        chosen: Store | None = stores[0] if stores else None
        if params.get("site_id"):
            wanted = parse_int_id(params["site_id"], "site_id")
            chosen = next((store for store in stores if store.pk == wanted), None)
            if chosen is None:
                raise Refusal("NOT_FOUND", "That store's list is not available.", status=404)
        rows = missing_hsn_items(chosen) if chosen is not None else []
        body = {
            "stores": [{"id": s.pk, "code": s.code, "name": s.name} for s in stores],
            "site_id": chosen.pk if chosen is not None else None,
            "items": [row.as_json() for row in rows],
        }
        return Response(MissingHsnSerializer(body).data)
