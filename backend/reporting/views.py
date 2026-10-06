"""Reports under ``/api/reports/`` (store operations PRD §17).

``GET sales``              the sales report, one grouping at a time
``GET sales/export.xlsx``  the same report as a spreadsheet
``GET offer-simulation?offer=`` the newest estimate of a draft offer (ticket 30)
``POST offer-simulation``        simulate a draft offer on past bills (ticket 30)
``GET offer-simulation/export.xlsx?offer=`` the newest estimate as a spreadsheet
``GET gst``                the GST report (ticket 47), one view at a time
``GET gst/export.xlsx``    the same report as a spreadsheet
``GET gift-stock``         gift pieces and the input tax credit to reverse (ticket 14)
``GET gift-stock/export.xlsx`` the same report as a spreadsheet
``GET discount-funding``   who funds the discounts (ticket 25), one grouping at a time
``GET discount-funding/export.xlsx`` the same report as a spreadsheet
``GET shrinkage``          the shrinkage report (ticket 44), one grouping at a time
``GET shrinkage/export.xlsx`` the same report as a spreadsheet
``GET margin-share``       the monthly margin share statement (ticket 27), every brand or one
``GET margin-share/export.xlsx`` the same statement as a spreadsheet
``GET inventory``          the inventory report (ticket 43), one grouping at a time
``GET inventory/export.xlsx`` the same report as a spreadsheet
``GET exceptions``         the counter's exceptions (ticket 48), per store or staff member
``GET exceptions/export.xlsx`` the same report as a spreadsheet
``GET brand-performance``  per brand and store: sales, earnings, stock age (ticket 45)
``GET brand-performance/export.xlsx`` the same report as a spreadsheet
``GET staff``              each salesperson's results (ticket 46): the team, or your own
``GET staff/export.xlsx``  the same report as a spreadsheet
``GET staff/targets``      a store's salespeople and their targets for one month
``PUT staff/targets``      set one person's monthly target (audited, before and after)

Who may read: the existing ``reports: view`` section rung (baseline B6) - every
role holds it today, a store role for its own store. Which stores: the viewer's
own (``actionable_stores``, narrowed by the top-bar unit), and only where the
sales report is switched on. Cost and margin are sent only to ``money: manage``.

The GST report is for Accounts: it also needs ``money: manage``, the gate the
IRN queue already answers to (Owner and Accounts hold it; no store role does).
So does the discount funding report: what a brand pays towards a discount is
the commercial side of its terms, which no store role sees. And so does the
margin share statement (ticket 27): a brand's margin is its commercial terms.

The Gift Stock report is read like the Sales report; its cost and credit reach
only ``money: manage`` (baseline B54), and every other reader gets the pieces
without them.

A read writes nothing. An export writes one audit entry, ``reports.export``.
"""

from __future__ import annotations

from accounts.principal import resolve_access

from typing import Any

from django.utils.text import slugify
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import require_section
from accounts.sections import CAP_MANAGE, CAP_OPERATE, CAP_VIEW
from core.refusals import Refusal
from offers.models import Offer
from offers.views import visible_offers
from reporting import (
    brand_performance,
    discount_funding,
    exceptions_report,
    gift_report,
    gst_report,
    inventory_report,
    margin_share,
    offer_return,
    offer_simulation,
    sales_report,
    shrinkage_report,
    staff_report,
    staff_targets,
)
from reporting.base import (
    record_export, report_scope, sees_cost, sees_financial_report, workbook, xlsx_response,
)

CanReadReports = require_section("reports", CAP_VIEW)
#: Ticket 30: reading an estimate is reading the rulebook (``offers_price: view``);
#: running one is authoring work (``operate``), as writing the offer is.
CanSimulateOffers = require_section("offers_price", CAP_VIEW, write_minimum=CAP_OPERATE)
#: Ticket 31: an offer's return is read beside the offer, on the rulebook's gate.
CanReadOfferReturn = require_section("offers_price", CAP_VIEW)
#: GST and IRN work is Accounts' (store operations PRD §3): the IRN queue's gate.
CanReadGst = require_section("money", CAP_MANAGE)
#: Ticket 25: what the brand and KDPS each pay towards discounts - the books' gate.
CanReadFunding = require_section("money", CAP_MANAGE)
#: Ticket 46: staff targets are set by whoever sets store targets (#171).
CanSetStaffTargets = require_section("money", CAP_MANAGE)

QUERY_KEYS = ("store", "date_from", "date_to", "group_by")


class ReportStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class ReportMissingSerializer(serializers.Serializer[Any]):
    code = serializers.CharField()
    text = serializers.CharField()


class ReportGroupingSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label


class SalesReportRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    bills = serializers.IntegerField()
    pieces = serializers.FloatField(allow_null=True)
    value_paise = serializers.IntegerField()
    gross_paise = serializers.IntegerField(allow_null=True)
    disc_paise = serializers.IntegerField(allow_null=True)
    asp_paise = serializers.IntegerField(allow_null=True)
    abv_paise = serializers.IntegerField(allow_null=True)
    upt = serializers.FloatField(allow_null=True)
    discount_pct = serializers.FloatField(allow_null=True)
    target_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="Absent when targets are not shown to you."
    )
    target_pct = serializers.FloatField(allow_null=True, required=False)
    cost_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="Absent unless you may see cost."
    )
    margin_paise = serializers.IntegerField(allow_null=True, required=False)
    margin_pct = serializers.FloatField(allow_null=True, required=False)


class SalesReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    shows_target = serializers.BooleanField()
    group_by = serializers.CharField()
    groupings = ReportGroupingSerializer(many=True)
    rows = SalesReportRowSerializer(many=True)
    total = SalesReportRowSerializer()


_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter(
        "group_by", str, enum=list(sales_report.GROUPINGS), description="Default: day."
    ),
]


def _group_by(request: Request) -> str:
    unknown = sorted(set(request.query_params) - set(QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    group_by = (request.query_params.get("group_by") or "day").strip()
    if group_by not in sales_report.GROUPINGS:
        raise Refusal(
            "INVALID_REQUEST",
            f"group_by must be one of {', '.join(sales_report.GROUPINGS)}.",
        )
    return group_by


def _body(request: Request) -> dict[str, Any]:
    group_by = _group_by(request)
    scope = report_scope(request.user, request.query_params, sales_report.FEATURE_KEY)
    return {"scope": scope, "body": sales_report.build(scope, group_by)}


class SalesReportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_sales", parameters=_PARAMETERS, responses=SalesReportSerializer
    )
    def get(self, request: Request) -> Response:
        # Built as a plain dict rather than through the serializer: cost and target
        # keys a viewer may not see are absent, not null, and a serializer would
        # put them back as nulls. The serializer documents the shape.
        return Response(_body(request)["body"])


class SalesReportExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_sales_export",
        parameters=_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The sales report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=sales_report.GROUPINGS[body["group_by"]],
            columns=sales_report.columns(body),
            rows=body["rows"],
            total=body["total"],
        )
        record_export(
            request.user, access=resolve_access(request),
            report=sales_report.REPORT,
            scope=built["scope"],
            detail={"group_by": body["group_by"], "rows": len(body["rows"])},
            contains_cost=body["shows_cost"],
            contains_targets=body["shows_target"],
            contains_team=body["group_by"] == "salesperson",
        )
        return xlsx_response(content, "sales-report")


# -- offer simulation (ticket 30, ST-OFR-3) ------------------------------------------


class OfferSimulationRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    bills = serializers.IntegerField()
    bills_affected = serializers.IntegerField()
    pieces = serializers.IntegerField()
    mrp_paise = serializers.IntegerField()
    discount_paise = serializers.IntegerField()
    discount_pct = serializers.FloatField(allow_null=True)
    actual_disc_paise = serializers.IntegerField()
    gifts = serializers.IntegerField()
    cost_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="Absent unless you may see cost."
    )
    margin_paise = serializers.IntegerField(allow_null=True, required=False)
    margin_pct = serializers.FloatField(allow_null=True, required=False)
    actual_margin_paise = serializers.IntegerField(allow_null=True, required=False)
    margin_effect_paise = serializers.IntegerField(allow_null=True, required=False)
    uncosted_pieces = serializers.IntegerField(required=False)


class OfferSimulationSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    estimate = serializers.BooleanField()
    formula_version = serializers.CharField()
    offer = serializers.IntegerField()
    simulation = serializers.IntegerField(
        allow_null=True, help_text="None until the offer is first simulated."
    )
    run_at = serializers.DateTimeField(allow_null=True)
    run_by = serializers.CharField()
    as_of = serializers.DateTimeField(allow_null=True)
    stale = serializers.BooleanField()
    stores = ReportStoreSerializer(many=True)
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    periods = OfferSimulationRowSerializer(many=True)


class OfferSimulationRunSerializer(serializers.Serializer[Any]):
    offer = serializers.IntegerField(min_value=1)


def _offer(request: Request, raw: Any) -> Offer:
    try:
        pk = int(str(raw or "").strip())
    except ValueError:
        raise Refusal("INVALID_REQUEST", "offer must be an offer id.") from None
    offer = visible_offers(request.user, include_ended=True).filter(pk=pk).first()
    if offer is None:
        raise Refusal("NOT_FOUND", f"No offer {pk}.", status=404)
    return offer


class OfferSimulationView(APIView):
    """What a draft offer would have cost on real past bills - an estimate."""

    permission_classes = [IsAuthenticated, CanSimulateOffers]

    @extend_schema(
        operation_id="reports_offer_simulation",
        parameters=[OpenApiParameter("offer", int, required=True, description="The offer's id.")],
        responses=OfferSimulationSerializer,
    )
    def get(self, request: Request) -> Response:
        offer = _offer(request, request.query_params.get("offer"))
        return Response(offer_simulation.read(request.user, offer))

    @extend_schema(
        operation_id="reports_offer_simulation_run",
        request=OfferSimulationRunSerializer,
        responses={201: OfferSimulationSerializer},
    )
    def post(self, request: Request) -> Response:
        offer = _offer(request, (request.data or {}).get("offer"))
        offer_simulation.run(request.user, offer)
        return Response(offer_simulation.read(request.user, offer), status=201)


class OfferSimulationExportView(APIView):
    """The newest estimate of an offer as a spreadsheet (§17). Recorded in the audit log."""

    permission_classes = [IsAuthenticated, CanSimulateOffers]

    @extend_schema(
        operation_id="reports_offer_simulation_export",
        parameters=[OpenApiParameter("offer", int, required=True, description="The offer's id.")],
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The offer simulation as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        offer = _offer(request, request.query_params.get("offer"))
        return xlsx_response(offer_simulation.export(request.user, offer, access=resolve_access(request)), "offer-simulation")


# -- Return on each offer (store operations ticket 31, ST-OFR-1) ------------------------

OFFER_RETURN_PARAMETERS = [
    OpenApiParameter("offer", int, required=True, description="The offer's id."),
    OpenApiParameter(
        "baseline_from",
        str,
        description="The baseline's first day, YYYY-MM-DD. With baseline_to; both left out "
        "means the same number of days just before the offer started.",
    ),
    OpenApiParameter("baseline_to", str, description="The baseline's last day, YYYY-MM-DD."),
]


class OfferReturnPeriodSerializer(serializers.Serializer[Any]):
    """One period's figures; every figure is None while the copy is not built."""

    key = serializers.ChoiceField(choices=["offer", "baseline"])
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    days = serializers.IntegerField()
    stated = serializers.BooleanField()
    bills = serializers.IntegerField(allow_null=True)
    pieces = serializers.IntegerField(allow_null=True)
    mrp_paise = serializers.IntegerField(allow_null=True)
    sales_paise = serializers.IntegerField(allow_null=True)
    discount_paise = serializers.IntegerField(allow_null=True)
    discount_pct = serializers.FloatField(allow_null=True)
    offer_pieces = serializers.IntegerField(allow_null=True)
    offer_discount_paise = serializers.IntegerField(allow_null=True)
    sales_per_day_paise = serializers.IntegerField(allow_null=True)
    pieces_per_day = serializers.FloatField(allow_null=True)
    brand_funded_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="Absent unless you may see cost."
    )
    margin_paise = serializers.IntegerField(allow_null=True, required=False)
    margin_pct = serializers.FloatField(allow_null=True, required=False)
    margin_per_day_paise = serializers.IntegerField(allow_null=True, required=False)
    uncosted_pieces = serializers.IntegerField(allow_null=True, required=False)


class OfferReturnChangeSerializer(serializers.Serializer[Any]):
    sales_per_day_paise = serializers.FloatField(
        allow_null=True, help_text="Per-day change against the baseline, in percent."
    )
    pieces_per_day = serializers.FloatField(allow_null=True)
    discount_pct = serializers.FloatField(allow_null=True, help_text="Change in points.")
    margin_per_day_paise = serializers.FloatField(allow_null=True, required=False)
    margin_pct = serializers.FloatField(allow_null=True, required=False)


class OfferReturnSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    offer = serializers.IntegerField()
    offer_name = serializers.CharField()
    as_of = serializers.DateTimeField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    periods = OfferReturnPeriodSerializer(many=True)
    change = OfferReturnChangeSerializer()


class OfferReturnView(APIView):
    """What an offer earned and cost once it ran, against a baseline period."""

    permission_classes = [IsAuthenticated, CanReadOfferReturn]

    @extend_schema(
        operation_id="reports_offer_return",
        parameters=OFFER_RETURN_PARAMETERS,
        responses=OfferReturnSerializer,
    )
    def get(self, request: Request) -> Response:
        offer = _offer(request, request.query_params.get("offer"))
        return Response(offer_return.read(request.user, offer, request.query_params))


class OfferReturnExportView(APIView):
    """An offer's return as a spreadsheet (§17). Recorded in the audit log."""

    permission_classes = [IsAuthenticated, CanReadOfferReturn]

    @extend_schema(
        operation_id="reports_offer_return_export",
        parameters=OFFER_RETURN_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The offer's return as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        offer = _offer(request, request.query_params.get("offer"))
        content = offer_return.export(request.user, offer, request.query_params, access=resolve_access(request))
        return xlsx_response(content, "offer-return")


# -- GST (store operations ticket 47, ST-RPT-5) -----------------------------------------

GST_QUERY_KEYS = ("store", "date_from", "date_to", "view")


class GstColumnSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    kind = serializers.ChoiceField(choices=["text", "money", "number", "rate", "date"])


class GstReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    not_a_filing = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    view = serializers.CharField()
    views = ReportGroupingSerializer(many=True)
    columns = GstColumnSerializer(many=True)
    rows = serializers.ListField(
        child=serializers.DictField(),
        help_text="One object per row, keyed by the columns' keys (and `key`).",
    )
    total = serializers.DictField()


_GST_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter("view", str, enum=list(gst_report.VIEWS), description="Default: rate."),
]


def _gst_view(request: Request) -> str:
    unknown = sorted(set(request.query_params) - set(GST_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    view = (request.query_params.get("view") or "rate").strip()
    if view not in gst_report.VIEWS:
        raise Refusal("INVALID_REQUEST", f"view must be one of {', '.join(gst_report.VIEWS)}.")
    return view


def _gst_body(request: Request) -> dict[str, Any]:
    view = _gst_view(request)
    scope = report_scope(request.user, request.query_params, gst_report.FEATURE_KEY)
    if not sees_financial_report(request.user, scope.stores):
        raise Refusal("SCOPE_DENIED", "That financial report is outside your scope.", status=403)
    return {"scope": scope, "body": gst_report.build(scope, view)}


class GstReportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports, CanReadGst]

    @extend_schema(
        operation_id="reports_gst", parameters=_GST_PARAMETERS, responses=GstReportSerializer
    )
    def get(self, request: Request) -> Response:
        return Response(_gst_body(request)["body"])


class GstReportExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports, CanReadGst]

    @extend_schema(
        operation_id="reports_gst_export",
        parameters=_GST_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The GST report as a spreadsheet, prepared for filing.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _gst_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=gst_report.VIEWS[body["view"]],
            columns=gst_report.columns(body["view"]),
            rows=body["rows"],
            total=body["total"],
            sheet=gst_report.SHEET,
        )
        record_export(
            request.user, access=resolve_access(request),
            report=gst_report.REPORT,
            scope=built["scope"],
            detail={"view": body["view"], "rows": len(body["rows"])},
            contains_financial=True,
        )
        return xlsx_response(content, f"gst-report-{body['view'].replace('_', '-')}")


# -- Gift stock (store operations ticket 14, ST-CMP-7) --------------------------------

GIFT_QUERY_KEYS = ("store", "date_from", "date_to", "view")


class GiftReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    not_a_filing = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    view = serializers.CharField()
    views = ReportGroupingSerializer(many=True)
    columns = GstColumnSerializer(many=True)
    rows = serializers.ListField(
        child=serializers.DictField(),
        help_text="One object per row, keyed by the columns' keys (and `key`). Cost and "
        "credit keys are absent unless `shows_cost`.",
    )
    total = serializers.DictField()


_GIFT_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter("view", str, enum=list(gift_report.VIEWS), description="Default: gstin."),
]


def _gift_body(request: Request) -> dict[str, Any]:
    unknown = sorted(set(request.query_params) - set(GIFT_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    view = (request.query_params.get("view") or "gstin").strip()
    if view not in gift_report.VIEWS:
        raise Refusal("INVALID_REQUEST", f"view must be one of {', '.join(gift_report.VIEWS)}.")
    scope = report_scope(
        request.user,
        request.query_params,
        gift_report.FEATURE_KEY,
        readable_when_off=gift_report.stores_with_tags(),
    )
    shows_cost = sees_cost(request.user, scope.stores)
    return {
        "scope": scope,
        "shows_cost": shows_cost,
        "body": gift_report.build(scope, view, shows_cost=shows_cost),
    }


class GiftReportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_gift_stock",
        parameters=_GIFT_PARAMETERS,
        responses=GiftReportSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(_gift_body(request)["body"])


class GiftReportExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_gift_stock_export",
        parameters=_GIFT_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The gift stock report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _gift_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=gift_report.VIEWS[body["view"]],
            columns=gift_report.columns(body["view"], built["shows_cost"]),
            rows=body["rows"],
            total=body["total"],
            sheet=gift_report.SHEET,
        )
        record_export(
            request.user, access=resolve_access(request),
            report=gift_report.REPORT,
            scope=built["scope"],
            detail={
                "view": body["view"],
                "rows": len(body["rows"]),
                "shows_cost": built["shows_cost"],
            },
            contains_cost=built["shows_cost"],
        )
        return xlsx_response(content, f"gift-stock-{body['view']}")


# -- discount funding (ticket 25) ---------------------------------------------------

FUNDING_QUERY_KEYS = ("store", "date_from", "date_to", "group_by")


class DiscountFundingColumnSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    kind = serializers.ChoiceField(choices=["text", "money", "number"])


class DiscountFundingRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    funder = serializers.CharField(required=False, help_text="By offer only.")
    lines = serializers.IntegerField()
    discount_paise = serializers.IntegerField()
    brand_paise = serializers.IntegerField()
    kdps_paise = serializers.IntegerField()
    unknown_paise = serializers.IntegerField()


class DiscountFundingReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    group_by = serializers.CharField()
    groupings = ReportGroupingSerializer(many=True)
    columns = DiscountFundingColumnSerializer(many=True)
    rows = DiscountFundingRowSerializer(many=True)
    total = DiscountFundingRowSerializer()


_FUNDING_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter(
        "group_by", str, enum=list(discount_funding.GROUPINGS), description="Default: offer."
    ),
]


def _funding_body(request: Request) -> dict[str, Any]:
    unknown = sorted(set(request.query_params) - set(FUNDING_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    group_by = (request.query_params.get("group_by") or "offer").strip()
    if group_by not in discount_funding.GROUPINGS:
        raise Refusal(
            "INVALID_REQUEST",
            f"group_by must be one of {', '.join(discount_funding.GROUPINGS)}.",
        )
    scope = report_scope(request.user, request.query_params, discount_funding.FEATURE_KEY)
    if not sees_financial_report(request.user, scope.stores):
        raise Refusal("SCOPE_DENIED", "That financial report is outside your scope.", status=403)
    return {"scope": scope, "body": discount_funding.build(scope, group_by)}


class DiscountFundingView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports, CanReadFunding]

    @extend_schema(
        operation_id="reports_discount_funding",
        parameters=_FUNDING_PARAMETERS,
        responses=DiscountFundingReportSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(_funding_body(request)["body"])


class DiscountFundingExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports, CanReadFunding]

    @extend_schema(
        operation_id="reports_discount_funding_export",
        parameters=_FUNDING_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The discount funding report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _funding_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=discount_funding.GROUPINGS[body["group_by"]],
            columns=discount_funding.columns(body["group_by"]),
            rows=body["rows"],
            total=body["total"],
            sheet=discount_funding.SHEET,
        )
        record_export(
            request.user, access=resolve_access(request),
            report=discount_funding.REPORT,
            scope=built["scope"],
            detail={"group_by": body["group_by"], "rows": len(body["rows"])},
            contains_financial=True,
        )
        return xlsx_response(content, f"discount-funding-{body['group_by']}")


# -- shrinkage (store operations ticket 44, ST-INV-4) -------------------------------------

SHRINKAGE_QUERY_KEYS = ("store", "date_from", "date_to", "group_by")


class ShrinkageColumnSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    kind = serializers.ChoiceField(choices=["text", "money", "number", "rate"])


class ShrinkageReportRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    documents = serializers.IntegerField()
    pieces_lost = serializers.IntegerField()
    pieces_sold = serializers.FloatField()
    pieces_pct = serializers.FloatField(allow_null=True)
    sales_paise = serializers.IntegerField()
    cost_paise = serializers.IntegerField(
        required=False, help_text="Absent unless you may see cost."
    )
    cost_share_pct = serializers.FloatField(
        allow_null=True, required=False, help_text="Absent unless you may see cost."
    )


class ShrinkageReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    group_by = serializers.CharField()
    groupings = ReportGroupingSerializer(many=True)
    columns = ShrinkageColumnSerializer(many=True)
    rows = ShrinkageReportRowSerializer(many=True)
    total = ShrinkageReportRowSerializer()


_SHRINKAGE_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter(
        "group_by", str, enum=list(shrinkage_report.GROUPINGS), description="Default: store."
    ),
]


def _shrinkage_group_by(request: Request) -> str:
    unknown = sorted(set(request.query_params) - set(SHRINKAGE_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    group_by = (request.query_params.get("group_by") or "store").strip()
    if group_by not in shrinkage_report.GROUPINGS:
        raise Refusal(
            "INVALID_REQUEST",
            f"group_by must be one of {', '.join(shrinkage_report.GROUPINGS)}.",
        )
    return group_by


def _shrinkage_body(request: Request) -> dict[str, Any]:
    group_by = _shrinkage_group_by(request)
    scope = report_scope(request.user, request.query_params, shrinkage_report.FEATURE_KEY)
    return {"scope": scope, "body": shrinkage_report.build(scope, group_by)}


class ShrinkageReportView(APIView):
    """Pieces and cost lost to shrinkage as a share of sales. Store roles get pieces only."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_shrinkage",
        parameters=_SHRINKAGE_PARAMETERS,
        responses=ShrinkageReportSerializer,
    )
    def get(self, request: Request) -> Response:
        # A plain dict, as the sales report: cost keys a viewer may not see are
        # absent, not null. The serializer documents the shape.
        return Response(_shrinkage_body(request)["body"])


class ShrinkageReportExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_shrinkage_export",
        parameters=_SHRINKAGE_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The shrinkage report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _shrinkage_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=shrinkage_report.GROUPINGS[body["group_by"]],
            columns=shrinkage_report.columns(body["group_by"], body["shows_cost"]),
            rows=body["rows"],
            total=body["total"],
        )
        record_export(
            request.user, access=resolve_access(request),
            report=shrinkage_report.REPORT,
            scope=built["scope"],
            detail={"group_by": body["group_by"], "rows": len(body["rows"])},
            contains_cost=body["shows_cost"],
        )
        return xlsx_response(content, "shrinkage-report")


# -- margin share statement (ticket 27) ----------------------------------------------

MARGIN_QUERY_KEYS = ("store", "month", "brand")
#: Ticket 27: how each sale divides between the brand and KDPS - the books' gate.
CanReadMarginShare = require_section("money", CAP_MANAGE)


class MarginShareBrandSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    name = serializers.CharField()


class MarginShareRowSerializer(serializers.Serializer[Any]):
    """Every brand: ``label`` to ``unsplit_paise``. One brand: ``day`` to ``note``.
    A share is null on a line that is not split."""

    key = serializers.CharField()
    label = serializers.CharField(required=False)  # type: ignore[assignment]  # DRF's Field.label
    model = serializers.CharField(required=False)
    margin = serializers.CharField(required=False)
    lines = serializers.IntegerField(required=False)
    day = serializers.CharField(required=False)
    store = serializers.CharField(required=False)
    bill = serializers.CharField(required=False)
    barcode = serializers.CharField(required=False)
    season = serializers.CharField(required=False)
    qty = serializers.IntegerField(required=False)
    value_paise = serializers.IntegerField()
    kdps_paise = serializers.IntegerField(allow_null=True)
    brand_paise = serializers.IntegerField(allow_null=True)
    unsplit_paise = serializers.IntegerField(required=False)
    note = serializers.CharField(required=False, allow_blank=True)


class MarginShareUnknownSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    lines = serializers.IntegerField()
    value_paise = serializers.IntegerField()
    reasons = serializers.ListField(child=serializers.CharField())


class MarginShareReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    estimate = serializers.BooleanField(help_text="Always true until OQ-50 is decided.")
    month = serializers.CharField()
    brand = MarginShareBrandSerializer(allow_null=True)
    columns = DiscountFundingColumnSerializer(many=True)
    rows = MarginShareRowSerializer(many=True)
    total = MarginShareRowSerializer()
    unknown_brands = MarginShareUnknownSerializer(many=True)


_MARGIN_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("month", str, description="YYYY-MM, India time. Default: this month."),
    OpenApiParameter("brand", int, description="One brand's statement, by id; blank for all."),
]


def _margin_body(request: Request) -> dict[str, Any]:
    params = request.query_params
    unknown = sorted(set(params) - set(MARGIN_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    month, first, last = margin_share.month_bounds(params.get("month"))
    brand = margin_share.brand_of(params.get("brand"))
    period = {
        "store": params.get("store") or "",
        "date_from": first.isoformat(),
        "date_to": last.isoformat(),
    }
    scope = report_scope(request.user, period, margin_share.FEATURE_KEY)
    if not sees_financial_report(request.user, scope.stores):
        raise Refusal("SCOPE_DENIED", "That financial report is outside your scope.", status=403)
    return {"scope": scope, "brand": brand, "body": margin_share.build(scope, month, brand)}


class MarginShareView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports, CanReadMarginShare]

    @extend_schema(
        operation_id="reports_margin_share",
        parameters=_MARGIN_PARAMETERS,
        responses=MarginShareReportSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(_margin_body(request)["body"])


class MarginShareExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports, CanReadMarginShare]

    @extend_schema(
        operation_id="reports_margin_share_export",
        parameters=_MARGIN_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The margin share statement as a spreadsheet, labelled an estimate.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _margin_body(request)
        body, brand = built["body"], built["brand"]
        content = workbook(
            body=body,
            grouping_label=f"Brand: {brand.name}" if brand else "Every brand",
            columns=margin_share.columns(brand),
            rows=body["rows"],
            total=body["total"],
            sheet=margin_share.SHEET,
        )
        record_export(
            request.user, access=resolve_access(request),
            report=margin_share.REPORT,
            scope=built["scope"],
            detail={
                "month": body["month"],
                "brand": brand.pk if brand else None,
                "rows": len(body["rows"]),
            },
            contains_financial=True,
        )
        who = slugify(brand.name) if brand else "all-brands"
        return xlsx_response(content, f"margin-share-{who}-{body['month']}")


# -- inventory (store operations ticket 43, ST-RPT-2) --------------------------------------

INVENTORY_QUERY_KEYS = ("store", "date_from", "date_to", "group_by", "span")


class InventoryReportRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    model = serializers.CharField(
        required=False, help_text="By brand, and only if you may see cost."
    )
    sold = serializers.FloatField()
    received = serializers.IntegerField(allow_null=True)
    sell_through_pct = serializers.FloatField(allow_null=True)
    on_hand = serializers.IntegerField(allow_null=True)
    weekly_sales = serializers.FloatField(allow_null=True)
    weeks_cover = serializers.FloatField(allow_null=True)
    dead_pieces = serializers.IntegerField(allow_null=True)
    dead_pct = serializers.FloatField(allow_null=True)
    stock_cost_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="Absent unless you may see cost."
    )
    dead_cost_paise = serializers.IntegerField(allow_null=True, required=False)
    margin_paise = serializers.IntegerField(allow_null=True, required=False)
    margin_pct = serializers.FloatField(allow_null=True, required=False)
    gmroi = serializers.FloatField(allow_null=True, required=False)


class InventoryReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    group_by = serializers.CharField()
    span = serializers.CharField()
    groupings = ReportGroupingSerializer(many=True)
    spans = ReportGroupingSerializer(many=True)
    columns = ShrinkageColumnSerializer(many=True)
    rows = InventoryReportRowSerializer(many=True)
    total = InventoryReportRowSerializer()


_INVENTORY_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter(
        "group_by", str, enum=list(inventory_report.GROUPINGS), description="Default: store."
    ),
    OpenApiParameter(
        "span",
        str,
        enum=list(inventory_report.SPANS),
        description="Sell-through over the chosen dates (default) or each season to date.",
    ),
]


def _inventory_body(request: Request) -> dict[str, Any]:
    params = request.query_params
    unknown = sorted(set(params) - set(INVENTORY_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    group_by = (params.get("group_by") or "store").strip()
    if group_by not in inventory_report.GROUPINGS:
        raise Refusal(
            "INVALID_REQUEST",
            f"group_by must be one of {', '.join(inventory_report.GROUPINGS)}.",
        )
    span = (params.get("span") or "period").strip()
    if span not in inventory_report.SPANS:
        raise Refusal(
            "INVALID_REQUEST", f"span must be one of {', '.join(inventory_report.SPANS)}."
        )
    scope = report_scope(request.user, params, inventory_report.FEATURE_KEY)
    return {"scope": scope, "body": inventory_report.build(scope, group_by, span)}


class InventoryReportView(APIView):
    """Sell-through, weeks of cover, GMROI and dead stock. Store roles get pieces only."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_inventory",
        parameters=_INVENTORY_PARAMETERS,
        responses=InventoryReportSerializer,
    )
    def get(self, request: Request) -> Response:
        # A plain dict, as the sales report: keys a viewer may not see are absent,
        # not null. The serializer documents the shape.
        return Response(_inventory_body(request)["body"])


class InventoryReportExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_inventory_export",
        parameters=_INVENTORY_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The inventory report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _inventory_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=(
                f"{inventory_report.GROUPINGS[body['group_by']]}; sell-through over "
                f"{inventory_report.SPANS[body['span']].lower()}"
            ),
            columns=inventory_report.columns(body["group_by"], body["shows_cost"]),
            rows=body["rows"],
            total=body["total"],
        )
        record_export(
            request.user, access=resolve_access(request),
            report=inventory_report.REPORT,
            scope=built["scope"],
            detail={"group_by": body["group_by"], "span": body["span"], "rows": len(body["rows"])},
            contains_cost=body["shows_cost"],
        )
        return xlsx_response(content, f"inventory-report-{body['group_by']}")


# -- exceptions report (ticket 48) --------------------------------------------------

EXCEPTIONS_QUERY_KEYS = ("store", "date_from", "date_to", "group_by")


class ExceptionsColumnSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    kind = serializers.CharField(help_text="text, money or number.")


class ExceptionsReportRowSerializer(serializers.Serializer[Any]):
    """By store or staff member: ``label`` and every count. The list: ``label``
    (the day) to ``value_paise``. The total carries whichever the view has."""

    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    cancelled_bills = serializers.IntegerField(required=False)
    cancelled_paise = serializers.IntegerField(required=False)
    prices_typed = serializers.IntegerField(required=False)
    price_typed_paise = serializers.IntegerField(required=False)
    manual_discounts = serializers.IntegerField(required=False)
    manual_discount_paise = serializers.IntegerField(required=False)
    pin_uses = serializers.IntegerField(required=False)
    cash_variances = serializers.IntegerField(required=False)
    cash_variance_paise = serializers.IntegerField(required=False)
    number_holes = serializers.IntegerField(required=False)
    no_bill_returns = serializers.IntegerField(required=False)
    no_bill_return_paise = serializers.IntegerField(required=False)
    no_bill_caps = serializers.IntegerField(required=False)
    late_syncs = serializers.IntegerField(required=False)
    store = serializers.CharField(required=False)
    staff = serializers.CharField(required=False)
    kind = serializers.CharField(required=False)
    reference = serializers.CharField(required=False)
    events = serializers.IntegerField(required=False)
    value_paise = serializers.IntegerField(required=False, allow_null=True)


class ExceptionsReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    group_by = serializers.CharField()
    groupings = ReportGroupingSerializer(many=True)
    columns = ExceptionsColumnSerializer(many=True)
    rows = ExceptionsReportRowSerializer(many=True)
    total = ExceptionsReportRowSerializer()


_EXCEPTIONS_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter(
        "group_by", str, enum=list(exceptions_report.GROUPINGS), description="Default: store."
    ),
]


def _exceptions_body(request: Request) -> dict[str, Any]:
    unknown = sorted(set(request.query_params) - set(EXCEPTIONS_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    group_by = (request.query_params.get("group_by") or "store").strip()
    if group_by not in exceptions_report.GROUPINGS:
        raise Refusal(
            "INVALID_REQUEST",
            f"group_by must be one of {', '.join(exceptions_report.GROUPINGS)}.",
        )
    scope = report_scope(request.user, request.query_params, exceptions_report.FEATURE_KEY)
    return {"scope": scope, "body": exceptions_report.build(scope, group_by)}


class ExceptionsReportView(APIView):
    """The counter's exceptions per store and staff member (ticket 48, ST-RPT-6)."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_exceptions",
        parameters=_EXCEPTIONS_PARAMETERS,
        responses=ExceptionsReportSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(_exceptions_body(request)["body"])


class ExceptionsReportExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_exceptions_export",
        parameters=_EXCEPTIONS_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The exceptions report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _exceptions_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=exceptions_report.GROUPINGS[body["group_by"]],
            columns=exceptions_report.columns(body["group_by"]),
            rows=body["rows"],
            total=body["total"],
        )
        record_export(
            request.user, access=resolve_access(request),
            report=exceptions_report.REPORT,
            scope=built["scope"],
            detail={"group_by": body["group_by"], "rows": len(body["rows"])},
        )
        return xlsx_response(content, f"exceptions-report-{body['group_by']}")


# -- brand performance (store operations ticket 45, ST-RPT-3) ------------------------

BRAND_PERFORMANCE_QUERY_KEYS = ("store", "date_from", "date_to", "group_by")


class BrandPerformanceRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    store = serializers.CharField(required=False, help_text="By brand and store only.")
    model = serializers.CharField(required=False, help_text="Only if you may see cost.")
    sold = serializers.FloatField()
    sales_paise = serializers.IntegerField()
    disc_paise = serializers.IntegerField()
    discount_pct = serializers.FloatField(allow_null=True)
    returned = serializers.FloatField()
    returns_paise = serializers.IntegerField()
    returns_pct = serializers.FloatField(allow_null=True)
    received = serializers.IntegerField(allow_null=True)
    sell_through_pct = serializers.FloatField(allow_null=True)
    on_hand = serializers.IntegerField(allow_null=True)
    avg_age_days = serializers.IntegerField(allow_null=True)
    dead_pieces = serializers.IntegerField(allow_null=True)
    ended_pieces = serializers.IntegerField(allow_null=True)
    aged_pct = serializers.FloatField(allow_null=True)
    stock_cost_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="Absent unless you may see cost."
    )
    margin_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="An estimate until OQ-50 is decided."
    )
    commission_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="An estimate until OQ-50 is decided."
    )
    earned_pct = serializers.FloatField(allow_null=True, required=False)
    gmroi = serializers.FloatField(
        allow_null=True, required=False, help_text="Outright brands only; an estimate."
    )


class BrandPerformanceSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    group_by = serializers.CharField()
    groupings = ReportGroupingSerializer(many=True)
    estimate_fields = serializers.ListField(
        child=serializers.CharField(), help_text="The columns that are estimates (OQ-50)."
    )
    estimate_note = serializers.CharField(allow_null=True)
    columns = ShrinkageColumnSerializer(many=True)
    rows = BrandPerformanceRowSerializer(many=True)
    total = BrandPerformanceRowSerializer()


_BRAND_PERFORMANCE_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
    OpenApiParameter(
        "group_by",
        str,
        enum=list(brand_performance.GROUPINGS),
        description="Default: brand_store.",
    ),
]


def _brand_performance_body(request: Request) -> dict[str, Any]:
    params = request.query_params
    unknown = sorted(set(params) - set(BRAND_PERFORMANCE_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    group_by = (params.get("group_by") or "brand_store").strip()
    if group_by not in brand_performance.GROUPINGS:
        raise Refusal(
            "INVALID_REQUEST",
            f"group_by must be one of {', '.join(brand_performance.GROUPINGS)}.",
        )
    scope = report_scope(request.user, params, brand_performance.FEATURE_KEY)
    return {"scope": scope, "body": brand_performance.build(scope, group_by)}


class BrandPerformanceView(APIView):
    """Per brand and store: sales, margin or commission, sell-through, discount depth,
    returns and stock age. Store roles get no margin, commission or GMROI."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_brand_performance",
        parameters=_BRAND_PERFORMANCE_PARAMETERS,
        responses=BrandPerformanceSerializer,
    )
    def get(self, request: Request) -> Response:
        # A plain dict, as the sales report: keys a viewer may not see are absent,
        # not null. The serializer documents the shape.
        return Response(_brand_performance_body(request)["body"])


class BrandPerformanceExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_brand_performance_export",
        parameters=_BRAND_PERFORMANCE_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The brand performance report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _brand_performance_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label=brand_performance.GROUPINGS[body["group_by"]],
            columns=brand_performance.columns(body["group_by"], body["shows_cost"]),
            rows=body["rows"],
            total=body["total"],
        )
        record_export(
            request.user, access=resolve_access(request),
            report=brand_performance.REPORT,
            scope=built["scope"],
            detail={"group_by": body["group_by"], "rows": len(body["rows"])},
            contains_cost=body["shows_cost"],
        )
        return xlsx_response(content, f"brand-performance-{body['group_by']}")


# -- staff performance report (ticket 46) -------------------------------------------

STAFF_QUERY_KEYS = ("store", "date_from", "date_to")


class StaffReportRowSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    store = serializers.CharField()
    bills = serializers.IntegerField()
    sold_pieces = serializers.FloatField()
    sold_paise = serializers.IntegerField()
    returned_pieces = serializers.FloatField()
    returned_paise = serializers.IntegerField()
    return_pct = serializers.FloatField(allow_null=True)
    pieces = serializers.FloatField()
    value_paise = serializers.IntegerField()
    upt = serializers.FloatField(allow_null=True)
    abv_paise = serializers.IntegerField(allow_null=True)
    target_paise = serializers.IntegerField(
        allow_null=True, required=False, help_text="Absent when targets are not shown to you."
    )
    target_pct = serializers.FloatField(allow_null=True, required=False)


class StaffReportSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    formula_version = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    shows_cost = serializers.BooleanField()
    view = serializers.ChoiceField(
        choices=[staff_report.TEAM, staff_report.OWN],
        help_text="team: every salesperson in scope; own: only the viewer's own results.",
    )
    shows_target = serializers.BooleanField()
    can_set_targets = serializers.BooleanField()
    columns = ExceptionsColumnSerializer(many=True)
    rows = StaffReportRowSerializer(many=True)
    total = StaffReportRowSerializer()


_STAFF_PARAMETERS = [
    OpenApiParameter("store", str, description="One of your stores, by code; blank for all."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
]


def _staff_body(request: Request) -> dict[str, Any]:
    unknown = sorted(set(request.query_params) - set(STAFF_QUERY_KEYS))
    if unknown:
        raise Refusal("INVALID_REQUEST", f"Unknown filter: {', '.join(unknown)}.")
    scope = report_scope(request.user, request.query_params, staff_report.FEATURE_KEY)
    return {"scope": scope, "body": staff_report.build(scope)}


class StaffReportView(APIView):
    """Each salesperson's results from split shares (ticket 46, ST-RPT-4)."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_staff",
        parameters=_STAFF_PARAMETERS,
        responses=StaffReportSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(_staff_body(request)["body"])


class StaffReportExportView(APIView):
    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_staff_export",
        parameters=_STAFF_PARAMETERS,
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
                "type": "string",
                "format": "binary",
                "description": "The staff performance report as a spreadsheet.",
            }
        },
    )
    def get(self, request: Request) -> Any:
        built = _staff_body(request)
        body = built["body"]
        content = workbook(
            body=body,
            grouping_label="Salesperson",
            columns=staff_report.columns(body["shows_target"]),
            rows=body["rows"],
            total=body["total"],
        )
        record_export(
            request.user, access=resolve_access(request),
            report=staff_report.REPORT,
            scope=built["scope"],
            detail={"view": body["view"], "rows": len(body["rows"])},
            contains_targets=body["shows_target"],
            contains_team=body["view"] == staff_report.TEAM,
        )
        return xlsx_response(content, "staff-performance-report")


class StaffTargetPersonSerializer(serializers.Serializer[Any]):
    staff_id = serializers.CharField()
    name = serializers.CharField()
    code = serializers.CharField()
    active = serializers.BooleanField()
    target_paise = serializers.IntegerField(allow_null=True)
    revision = serializers.IntegerField(help_text="Send it back when you change the target.")


class StaffTargetsSerializer(serializers.Serializer[Any]):
    store = serializers.CharField()
    month = serializers.DateField()
    switched_on = serializers.BooleanField()
    people = StaffTargetPersonSerializer(many=True)


class StaffTargetSetSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField(help_text="New for each change; the same on a resend.")
    store = serializers.CharField()
    staff_id = serializers.CharField()
    month = serializers.CharField(help_text="YYYY-MM.")
    target_paise = serializers.IntegerField(min_value=0)
    revision = serializers.IntegerField(
        min_value=0, help_text="The revision you loaded; 0 when no target was set."
    )


class StaffTargetSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    store = serializers.CharField()
    staff_id = serializers.CharField()
    name = serializers.CharField()
    month = serializers.DateField()
    target_paise = serializers.IntegerField()
    revision = serializers.IntegerField()


class StaffTargetsView(APIView):
    """Each salesperson's monthly target at a store: read and set (ticket 46)."""

    permission_classes = [IsAuthenticated, CanReadReports, CanSetStaffTargets]

    @extend_schema(
        operation_id="reports_staff_targets",
        parameters=[
            OpenApiParameter("store", str, required=True, description="One of your stores."),
            OpenApiParameter("month", str, description="YYYY-MM; default this month."),
        ],
        responses=StaffTargetsSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(staff_targets.people(request.user, request.query_params))

    @extend_schema(
        operation_id="reports_staff_targets_set",
        request=StaffTargetSetSerializer,
        responses=StaffTargetSerializer,
    )
    def put(self, request: Request) -> Response:
        asked = staff_targets.parse(request.user, request.data)
        return Response(staff_targets.set_target(request.user, asked))
