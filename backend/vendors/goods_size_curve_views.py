"""Size curve on the booking form (store operations ticket 40, ST-BUY-2).

``GET  /api/goods-v1/bookings/size-curve?site_id=&brand_id=&season_id=``
    every category's size split for that brand at that store, from the same
    season last year; or, where there is none, why not.
``POST /api/goods-v1/bookings/size-curve/fills``
    split one style total over one category's curve (audited; nothing else is
    written, and the booking itself is saved as before).

Only someone with ``booking.manage`` over that exact store and brand asks,
regardless of the store's stock contract. Anyone else is refused, and a store
or brand outside scope is not found. Where the
switch is off at the store, the read answers "switched off" with no curve (the
booking form asks for every store it books for, and switched off is not an
error there), and a fill is refused.  No cost or price is in any answer: the
curve is sold pieces by size.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, parse_int_id, parse_meta
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal, issue
from masters.models import Brand, Season, Store
from masters.store_feature_registry import SIZE_CURVE
from masters.store_features import is_feature_on, require_feature
from vendors import size_curve
from vendors.goods_services import BOOKING_ACTION, season_retired
from vendors.size_curve_models import SizeCurveFill

FILL_ACTION = "buying.size_curve.fill"
#: The largest style total one fill splits: far above any real booking line.
MAX_TOTAL = 100_000
STYLE_LENGTH = 120
CATEGORY_LENGTH = 120
#: The read's reason when the switch is off at the store: no curve is offered.
SWITCHED_OFF = "switched_off"

REASON_WORDS = {
    size_curve.NO_LAST_YEAR: (
        "This season's code does not say which season last year it follows "
        "(it should look like SS26 or AW25), so there is no size curve. Type the sizes."
    ),
    size_curve.NO_HISTORY: (
        "No bill at this store names this brand in season {reference} with a category and a "
        "size, so there is no size curve. Type the sizes."
    ),
}


class SizeCurveSizeSerializer(serializers.Serializer[Any]):
    size = serializers.CharField()
    pieces = serializers.IntegerField(help_text="Pieces sold in the reference season, net.")
    share_permille = serializers.IntegerField(help_text="Share of the category, in tenths of %.")


class SizeCurveSerializer(serializers.Serializer[Any]):
    category = serializers.CharField()
    pieces = serializers.IntegerField()
    sizes = SizeCurveSizeSerializer(many=True)


class SizeCurveNameSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class SizeCurvesSerializer(serializers.Serializer[Any]):
    store = SizeCurveNameSerializer()
    brand = SizeCurveNameSerializer()
    season = SizeCurveNameSerializer()
    reference_season = serializers.CharField(
        allow_null=True, help_text="The same season last year; null when the code says none."
    )
    reason = serializers.ChoiceField(
        choices=[size_curve.NO_LAST_YEAR, size_curve.NO_HISTORY, SWITCHED_OFF],
        allow_null=True,
        help_text="Why there is no curve at all; null when there is one.",
    )
    message = serializers.CharField(allow_blank=True, help_text="The reason, in words.")
    curves = SizeCurveSerializer(many=True)
    uncategorised_pieces = serializers.IntegerField()
    unsized_pieces = serializers.IntegerField()


class SizeCurveFillRequestSerializer(serializers.Serializer[Any]):
    site_id = serializers.IntegerField()
    brand_id = serializers.IntegerField()
    season_id = serializers.IntegerField()
    category = serializers.CharField(max_length=CATEGORY_LENGTH)
    style_code = serializers.CharField(max_length=STYLE_LENGTH)
    total = serializers.IntegerField(min_value=1, max_value=MAX_TOTAL)


class SizeCurveFillSizeSerializer(serializers.Serializer[Any]):
    size = serializers.CharField()
    qty = serializers.IntegerField()


class SizeCurveFillSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    store = SizeCurveNameSerializer()
    style_code = serializers.CharField()
    category = serializers.CharField()
    season = serializers.CharField()
    reference_season = serializers.CharField()
    total = serializers.IntegerField()
    sizes = SizeCurveFillSizeSerializer(many=True, help_text="Adds up to total; sizes in order.")


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------


def _row(rows: Any, pk: int, what: str) -> Any:
    found = rows.filter(pk=pk).first()
    if found is None:
        raise Refusal("NOT_FOUND", f"That {what} was not found.", status=404)
    return found


def _may_book(_request: Request, access: AccessContext, store: Store, brand: Brand) -> None:
    """Refuse unless this person may book ``brand`` for ``store`` (the booking's own rule)."""
    access.require(BOOKING_ACTION, site_id=store.pk, brand_id=brand.pk)


def _asked(
    request: Request, access: AccessContext, site_id: Any, brand_id: Any, season_id: Any
) -> tuple[Store, Brand, Season]:
    store = _row(Store.objects.filter(is_active=True), parse_int_id(site_id, "site_id"), "store")
    # Only what a booking itself can name: an active brand and a season not retired.
    brand = _row(Brand.objects.filter(is_active=True), parse_int_id(brand_id, "brand_id"), "brand")
    season = _row(Season.objects.all(), parse_int_id(season_id, "season_id"), "season")
    if season_retired(season.pk):
        raise Refusal("NOT_FOUND", "That season was not found.", status=404)
    _may_book(request, access, store, brand)
    return store, brand, season


def _name(row: Any) -> dict[str, Any]:
    return {"id": row.pk, "code": row.code, "name": row.name}


def _message(found: size_curve.StoreCurves) -> str:
    reason = found.reason
    if reason is None:
        return ""
    return REASON_WORDS[reason].format(reference=found.reference_code or "")


# ---------------------------------------------------------------------------
# The views
# ---------------------------------------------------------------------------


class GoodsSizeCurveView(GoodsAPIView):
    """Every category's size split for a brand at a store (who may book it; none when off)."""

    @extend_schema(
        operation_id="goods_v1_bookings_size_curve",
        parameters=[
            OpenApiParameter("site_id", int, required=True, description="The line's store."),
            OpenApiParameter("brand_id", int, required=True),
            OpenApiParameter("season_id", int, required=True, description="The booking's season."),
        ],
        responses=SizeCurvesSerializer,
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, allowed=("site_id", "brand_id", "season_id"))
        store, brand, season = _asked(
            request, access, params.get("site_id"), params.get("brand_id"), params.get("season_id")
        )
        body: dict[str, Any] = {
            "store": _name(store),
            "brand": _name(brand),
            "season": _name(season),
            "reference_season": size_curve.reference_season_code(season.code),
            "reason": SWITCHED_OFF,
            "message": f"The size curve is switched off for {store.name}.",
            "curves": [],
            "uncategorised_pieces": 0,
            "unsized_pieces": 0,
        }
        if is_feature_on(store, SIZE_CURVE):
            found = size_curve.store_curves(store, brand, season)
            body.update(
                reason=found.reason,
                message=_message(found),
                curves=[curve.as_json() for curve in found.curves],
                uncategorised_pieces=found.uncategorised_pieces,
                unsized_pieces=found.unsized_pieces,
            )
        return Response(SizeCurvesSerializer(body).data)


def _text(body: dict[str, Any], key: str, limit: int) -> str:
    raw = body.get(key)
    text = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not text:
        raise Refusal(
            "INVALID_REQUEST",
            f"{key} is required.",
            issues=[issue("REQUIRED", f"{key} is required", field=key)],
        )
    if len(text) > limit:
        raise Refusal(
            "INVALID_REQUEST",
            f"{key} can be at most {limit} characters.",
            issues=[issue("INVALID", "too long", field=key)],
        )
    return text


def _total(body: dict[str, Any]) -> int:
    raw = body.get("total")
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_TOTAL:
        raise Refusal(
            "INVALID_REQUEST",
            f"The style total must be a whole number of pieces from 1 to {MAX_TOTAL}.",
            issues=[issue("INVALID", "not a style total", field="total")],
        )
    return raw


def _audit_line(fill: SizeCurveFill) -> dict[str, Any]:
    return {
        "store": fill.store_id,
        "brand": fill.brand_id,
        "season": fill.season_id,
        "style_code": fill.style_code,
        "category": fill.category,
        "reference_season": fill.reference_season,
        "history": fill.history,
    }


class GoodsSizeCurveFillView(GoodsAPIView):
    """Split one style total over a category's curve (audited, before and after)."""

    @extend_schema(
        operation_id="goods_v1_bookings_size_curve_fill",
        request=SizeCurveFillRequestSerializer,
        responses={201: SizeCurveFillSerializer},
    )
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            {"site_id", "brand_id", "season_id", "category", "style_code", "total"},
            required=["site_id", "brand_id", "season_id", "category", "style_code", "total"],
        )
        style_code = _text(body, "style_code", STYLE_LENGTH)
        category = _text(body, "category", CATEGORY_LENGTH)
        total = _total(body)
        store, brand, season = _asked(
            request, access, body["site_id"], body["brand_id"], body["season_id"]
        )
        business_input = {
            "site_id": store.pk,
            "brand_id": brand.pk,
            "season_id": season.pk,
            "category": category,
            "style_code": style_code,
            "total": total,
        }

        def handler(run: CommandRun) -> CommandResult:
            # Inside the command, so a replay of a fill already made answers what
            # it made even if the switch has gone off since.
            require_feature(store, SIZE_CURVE)
            found = size_curve.store_curves(store, brand, season)
            curve = found.curve(category)
            if curve is None or found.reference_code is None:
                raise Refusal(
                    "NO_SIZE_CURVE",
                    _message(found)
                    or (
                        f"No {category} of this brand sold at this store in "
                        f"{found.reference_code}, so there is no size curve for it. "
                        "Type the sizes."
                    ),
                    status=422,
                )
            split = size_curve.allocate(total, dict(curve.sizes))
            fill = SizeCurveFill.objects.create(
                tenant_id=run.tenant_id,
                store=store,
                brand=brand,
                season=season,
                reference_season=found.reference_code,
                category=curve.category,
                style_code=style_code,
                total=total,
                history=[{"size": size, "pieces": pieces} for size, pieces in curve.sizes],
                split=[{"size": size, "qty": qty} for size, qty in split],
                made_at=run.now,
                made_by_id=run.principal.human_id,
            )
            run.audit_subject_key = f"size_curve_fill:{fill.pk}"
            # Before: the line as the buyer typed it, a style total with no size.
            # After: the same line split into sizes by the curve.
            run.audit_before = {
                **_audit_line(fill),
                "sizes": [{"size": None, "qty": total}],
            }
            run.audit_after = {**_audit_line(fill), "sizes": fill.split}
            return CommandResult(
                resource_type="size_curve_fill", resource_id=str(fill.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action=FILL_ACTION,
            meta=meta,
            business_input=business_input,
            handler=handler,
            subject_key="size_curve_fill:new",
            site_id=store.pk,
        )
        fill = SizeCurveFill.objects.select_related("store", "season").get(
            pk=str(result.resource_id)
        )
        payload = {
            "id": str(fill.pk),
            "store": _name(fill.store),
            "style_code": fill.style_code,
            "category": fill.category,
            "season": fill.season.code,
            "reference_season": fill.reference_season,
            "total": fill.total,
            "sizes": fill.split,
        }
        return Response(SizeCurveFillSerializer(payload).data, status=result.status_code)
