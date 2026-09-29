"""Brands > Reports (store operations ST-BRD-2, ticket 29), under ``/api/reports/``.

``GET  brand-reports?store=&month=``   the month at the viewer's stores: which brands
                                       sold or held stock, and the files the monthly
                                       run kept
``GET  brand-reports/make?store=&brand=&kind=&month=``  one report now, as a file
``GET  brand-reports/files/<id>``      a file the monthly run kept
``GET  brand-layouts``                 the layouts: the company's standard ones and
                                       each brand's own, with what a column can show
``POST brand-layouts``                 save one layout (Accounts)

Reading and making reports is the existing ``reports: view`` rung (baseline
B315), for the viewer's own stores where the ``brand-reports`` switch is on; the
reports carry no cost or margin. Saving a layout is Accounts' (B314), and needs
the switch on at one or more of their stores, as brand terms do (ticket 23).

Taking a file writes one ``reports.export`` audit entry; saving a layout is one
command whose audit record holds the layout before and after.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Count, Sum
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal
from masters.models import Brand, Store
from masters.store_feature_registry import BRAND_REPORTS
from reporting import brand_layouts, brand_reports
from reporting.base import (
    Missing,
    ReportScope,
    note_scope,
    principal_for,
    record_export,
    refresh_minutes,
    report_scope,
    viewer_stores,
)
from reporting.models import (
    BrandReportFile,
    BrandReportLayout,
    BrandReportRun,
    BrandSaleLineFact,
    InventoryItemFact,
    InventorySnapshot,
)
from reporting.views import CanReadReports, ReportMissingSerializer, ReportStoreSerializer

REPORT = "brand-reports"
TITLE = "Brand reports"

MONTH_PARAMETERS = [
    OpenApiParameter("store", str, description="One store's code; all of yours when left out."),
    OpenApiParameter("month", str, description="The month, YYYY-MM; this month when left out."),
]
MAKE_PARAMETERS = [
    OpenApiParameter("store", str, required=True, description="The store's code."),
    OpenApiParameter("brand", int, required=True, description="The brand's id."),
    OpenApiParameter("kind", str, required=True, enum=["sale", "soh"]),
    OpenApiParameter("month", str, description="The month, YYYY-MM; this month when left out."),
]
FILE_RESPONSES = {
    (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): {
        "type": "string",
        "format": "binary",
        "description": "The report in the brand's layout, as .xlsx.",
    },
    (200, "text/csv"): {
        "type": "string",
        "format": "binary",
        "description": "The report in the brand's layout, as .csv.",
    },
}


# -- the wire ---------------------------------------------------------------------------


class BrandRefSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class BrandLayoutRefSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField(
        allow_null=True, help_text="Null: the built-in KDPS sheet, never saved by this company."
    )
    name = serializers.CharField()
    revision = serializers.IntegerField()
    file_type = serializers.CharField()
    own = serializers.BooleanField(help_text="False: the brand uses the company's standard layout.")


class BrandLayoutPairSerializer(serializers.Serializer[Any]):
    sale = BrandLayoutRefSerializer()
    soh = BrandLayoutRefSerializer()


class BrandMonthBrandSerializer(BrandRefSerializer):
    bill_lines = serializers.IntegerField(help_text="Goods lines on the brand's bills, both ways.")
    pieces_sold = serializers.IntegerField(help_text="Pieces sold less pieces given back.")
    pieces_in_stock = serializers.IntegerField(
        help_text="Pieces at the month's stock snapshot of each store."
    )
    layouts = BrandLayoutPairSerializer()


class BrandReportFileSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    store = ReportStoreSerializer()
    brand = BrandRefSerializer()
    kind = serializers.ChoiceField(choices=["sale", "soh"])
    month = serializers.CharField()
    file_name = serializers.CharField()
    rows = serializers.IntegerField()
    size = serializers.IntegerField()
    made_at = serializers.DateTimeField()
    as_of = serializers.DateTimeField(allow_null=True)
    layout_revision = serializers.IntegerField()
    missing = ReportMissingSerializer(many=True)


class BrandReportRunSerializer(serializers.Serializer[Any]):
    store = ReportStoreSerializer()
    made_at = serializers.DateTimeField()
    files = serializers.IntegerField()


class BrandMonthSerializer(serializers.Serializer[Any]):
    report = serializers.CharField()
    title = serializers.CharField()
    month = serializers.CharField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    running = serializers.BooleanField(help_text="The month has not ended: reports are to date.")
    store = serializers.CharField(allow_null=True)
    stores = ReportStoreSerializer(many=True)
    store_options = ReportStoreSerializer(many=True)
    sale_as_of = serializers.DateTimeField(allow_null=True)
    soh_as_of = serializers.DateTimeField(allow_null=True)
    refreshed_every_minutes = serializers.IntegerField()
    sale_basis = serializers.ListField(child=serializers.CharField())
    soh_basis = serializers.ListField(child=serializers.CharField())
    missing = ReportMissingSerializer(many=True)
    brands = BrandMonthBrandSerializer(many=True)
    brand_options = BrandRefSerializer(many=True)
    files = BrandReportFileSerializer(many=True)
    runs = BrandReportRunSerializer(many=True)
    can_edit_layouts = serializers.BooleanField()


class LayoutTitleLineSerializer(serializers.Serializer[Any]):
    text = serializers.CharField()
    size = serializers.IntegerField()


class LayoutColumnSerializer(serializers.Serializer[Any]):
    field = serializers.CharField()
    header = serializers.CharField()
    format = serializers.CharField()
    total = serializers.BooleanField()
    value = serializers.CharField(required=False, help_text="Fixed text columns only.")


class LayoutBodySerializer(serializers.Serializer[Any]):
    file_type = serializers.ChoiceField(choices=list(brand_layouts.FILE_TYPES))
    sheet_name = serializers.CharField(allow_blank=True)
    font = serializers.CharField()
    header_size = serializers.IntegerField()
    header_border = serializers.ChoiceField(choices=list(brand_layouts.HEADER_BORDERS))
    title_lines = LayoutTitleLineSerializer(many=True)
    columns = LayoutColumnSerializer(many=True)


class BrandLayoutSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField(
        allow_null=True, help_text="Null: the built-in KDPS sheet, never saved by this company."
    )
    brand_id = serializers.IntegerField(allow_null=True)
    kind = serializers.ChoiceField(choices=["sale", "soh"])
    name = serializers.CharField()
    revision = serializers.IntegerField()
    layout = LayoutBodySerializer()
    updated_at = serializers.DateTimeField(allow_null=True)


class LayoutFieldSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label
    kind = serializers.CharField()
    formats = serializers.ListField(child=serializers.CharField())
    can_total = serializers.BooleanField()


class LayoutFieldsSerializer(serializers.Serializer[Any]):
    sale = LayoutFieldSerializer(many=True)
    soh = LayoutFieldSerializer(many=True)


class LayoutChoiceSerializer(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # DRF's Field.label


class BrandLayoutsBrandSerializer(BrandRefSerializer):
    sale = BrandLayoutSerializer(allow_null=True)
    soh = BrandLayoutSerializer(allow_null=True)


class BrandLayoutsStandardSerializer(serializers.Serializer[Any]):
    sale = BrandLayoutSerializer()
    soh = BrandLayoutSerializer()


class BrandLayoutsSerializer(serializers.Serializer[Any]):
    can_edit = serializers.BooleanField()
    field_catalogue = LayoutFieldsSerializer(help_text="What a column can show, per report.")
    formats = LayoutChoiceSerializer(many=True)
    placeholders = LayoutChoiceSerializer(many=True)
    file_types = LayoutChoiceSerializer(many=True)
    header_borders = LayoutChoiceSerializer(many=True)
    standard = BrandLayoutsStandardSerializer()
    brands = BrandLayoutsBrandSerializer(many=True)


class SaveLayoutRequestSerializer(serializers.Serializer[Any]):
    command_id = serializers.UUIDField()
    brand_id = serializers.IntegerField(
        allow_null=True, help_text="Null: the company's standard layout."
    )
    kind = serializers.ChoiceField(choices=["sale", "soh"])
    expected_revision = serializers.IntegerField(
        allow_null=True, help_text="The revision you opened; null for a brand with none yet."
    )
    layout = LayoutBodySerializer()


# -- helpers ----------------------------------------------------------------------------


def _store(store: Store) -> dict[str, Any]:
    return {"id": store.pk, "code": store.code, "name": store.name}


def _brand(brand: Brand) -> dict[str, Any]:
    return {"id": brand.pk, "code": brand.code, "name": brand.name}


def _scope(request: Request, when: brand_reports.Period) -> ReportScope:
    params = {
        "store": request.query_params.get("store") or "",
        "date_from": when.first.isoformat(),
        "date_to": when.upto.isoformat(),
    }
    return report_scope(request.user, params, BRAND_REPORTS)


def _brand_of(raw: Any, tenant_id: Any) -> Brand:
    text = str(raw or "").strip()
    if not text.isdigit():
        raise Refusal("INVALID_REQUEST", "brand must be a brand's id.", status=400)
    brand = Brand.objects.filter(pk=int(text), tenant_id=tenant_id).first()
    if brand is None:
        raise Refusal("NOT_FOUND", "No brand has that id.", status=404)
    return brand


def _layout_ref(row: BrandReportLayout, own: bool) -> dict[str, Any]:
    return {
        "id": row.pk,
        "name": row.name,
        "revision": row.revision,
        "file_type": row.layout.get("file_type", "xlsx"),
        "own": own,
    }


def _layout_json(row: BrandReportLayout) -> dict[str, Any]:
    return {
        "id": row.pk,
        "brand_id": row.brand_id,
        "kind": row.kind,
        "name": row.name,
        "revision": row.revision,
        "layout": {"header_border": "grid", **row.layout},
        "updated_at": row.updated_at,
    }


def _file_response(content: bytes, content_type: str, file_name: str) -> HttpResponse:
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{file_name}"'
    return response


def _tenant(request: Request, stores: list[Store]) -> Any:
    return getattr(request.user, "tenant_id", None) or stores[0].tenant_id


# -- the month ---------------------------------------------------------------------------


def _month_body(request: Request) -> dict[str, Any]:
    when = brand_reports.period(request.query_params.get("month"))
    scope = _scope(request, when)
    tenant_id = _tenant(request, scope.stores)
    missing = Missing()
    sale_as_of = brand_reports.as_of_for(brand_layouts.SALE, missing)
    soh_as_of = brand_reports.as_of_for(brand_layouts.SOH, missing)
    note_scope(scope, TITLE, missing)
    brands = list(Brand.objects.filter(tenant_id=tenant_id, is_active=True).order_by("name"))
    index = brand_reports.brand_index(brands)

    lines: dict[str, tuple[int, int]] = {}
    for row in (
        BrandSaleLineFact.objects.filter(
            store_id__in=scope.store_ids, day__gte=when.first, day__lte=when.upto
        )
        .values("brand_key")
        .annotate(lines=Count("id"), pieces=Sum("qty"))
    ):
        lines[row["brand_key"]] = (int(row["lines"]), int(row["pieces"] or 0))
    stock: dict[str, int] = {}
    unsnapped: list[Store] = []
    for store in scope.stores:
        snapshot = (
            InventorySnapshot.objects.filter(store=store, day__gte=when.first, day__lte=when.upto)
            .order_by("-day")
            .first()
        )
        if snapshot is None:
            unsnapped.append(store)
            continue
        for row in (
            InventoryItemFact.objects.filter(store=store, day=snapshot.day)
            .values("brand_key")
            .annotate(pieces=Sum("pieces"))
        ):
            stock[row["brand_key"]] = stock.get(row["brand_key"], 0) + int(row["pieces"] or 0)
    if unsnapped:
        names = ", ".join(f"{s.name} ({s.code})" for s in unsnapped)
        missing.add(
            "NO_SNAPSHOT",
            f"No stock snapshot was taken in {when.first:%B %Y} at {names}: no SOH there.",
        )
    unmatched = sum(n for key, (n, _p) in lines.items() if key not in index)
    if unmatched:
        missing.add(
            "UNMATCHED_BRAND",
            f"{unmatched} bill line(s) name a brand that is not in Brands (or is retired), so "
            "they are in no brand's report.",
        )

    standard = {kind: brand_layouts.standard(tenant_id, kind) for kind in brand_layouts.KINDS}
    own = {
        (row.brand_id, row.kind): row for row in BrandReportLayout.objects.filter(brand__in=brands)
    }
    listed = []
    for brand in brands:
        keys = brand_reports.keys_of(brand)
        bill_lines = sum(lines.get(k, (0, 0))[0] for k in keys)
        sold = sum(lines.get(k, (0, 0))[1] for k in keys)
        held = sum(stock.get(k, 0) for k in keys)
        if not bill_lines and not held:
            continue
        listed.append(
            {
                **_brand(brand),
                "bill_lines": bill_lines,
                "pieces_sold": sold,
                "pieces_in_stock": held,
                "layouts": {
                    kind: _layout_ref(
                        own.get((brand.pk, kind)) or standard[kind], (brand.pk, kind) in own
                    )
                    for kind in brand_layouts.KINDS
                },
            }
        )

    files = [
        {
            "id": f.pk,
            "store": _store(f.store),
            "brand": _brand(f.brand),
            "kind": f.kind,
            "month": when.label,
            "file_name": f.file_name,
            "rows": f.rows,
            "size": f.size,
            "made_at": f.made_at,
            "as_of": f.as_of,
            "layout_revision": f.layout_revision,
            "missing": (f.about or {}).get("missing", []),
        }
        for f in BrandReportFile.objects.filter(store_id__in=scope.store_ids, month=when.first)
        .select_related("store", "brand")
        .defer("content")
        .order_by("store__code", "brand__name", "kind")
    ]
    runs = [
        {"store": _store(r.store), "made_at": r.made_at, "files": r.files}
        for r in BrandReportRun.objects.filter(
            store_id__in=scope.store_ids, month=when.first
        ).select_related("store")
    ]
    return {
        "report": REPORT,
        "title": TITLE,
        "month": when.label,
        "date_from": when.first,
        "date_to": when.upto,
        "running": when.running,
        "store": scope.picked.code if scope.picked else None,
        "stores": [_store(s) for s in scope.stores],
        "store_options": [_store(s) for s in scope.options],
        "sale_as_of": sale_as_of,
        "soh_as_of": soh_as_of,
        "refreshed_every_minutes": refresh_minutes(),
        "sale_basis": brand_reports.SALE_BASIS,
        "soh_basis": brand_reports.SOH_BASIS,
        "missing": missing.items,
        "brands": listed,
        "brand_options": [_brand(b) for b in brands],
        "files": files,
        "runs": runs,
        "can_edit_layouts": brand_layouts.may_edit(request.user),
    }


class BrandReportsView(APIView):
    """The month at the viewer's stores: brands with sales or stock, and the kept files."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_brand_reports",
        parameters=MONTH_PARAMETERS,
        responses=BrandMonthSerializer,
    )
    def get(self, request: Request) -> Response:
        return Response(BrandMonthSerializer(_month_body(request)).data)


class BrandReportMakeView(APIView):
    """One brand's report at one store for one month, made now in the brand's layout."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(
        operation_id="reports_brand_reports_make",
        parameters=MAKE_PARAMETERS,
        responses=FILE_RESPONSES,
    )
    def get(self, request: Request) -> Any:
        when = brand_reports.period(request.query_params.get("month"))
        if not (request.query_params.get("store") or "").strip():
            raise Refusal(
                "INVALID_REQUEST", "Choose a store: a report is for one store.", status=400
            )
        kind = (request.query_params.get("kind") or "").strip()
        if kind not in brand_layouts.KINDS:
            raise Refusal("INVALID_REQUEST", "kind must be sale or soh.", status=400)
        scope = _scope(request, when)
        assert scope.picked is not None
        brand = _brand_of(request.query_params.get("brand"), scope.picked.tenant_id)
        made = brand_reports.make(scope.picked, brand, kind, when)
        record_export(
            request.user,
            report=f"{REPORT}:{kind}",
            scope=scope,
            detail={
                "brand": brand.code,
                "month": when.label,
                "rows": made.rows,
                "layout_id": made.layout.pk,
                "layout_revision": made.layout.revision,
                "made": "on_demand",
            },
        )
        return _file_response(made.content, made.content_type, made.file_name)


class BrandReportFileView(APIView):
    """A file the monthly run kept, exactly as it was made."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(operation_id="reports_brand_reports_file", responses=FILE_RESPONSES)
    def get(self, request: Request, pk: int) -> Any:
        # Only the viewer's own stores' files: a file id says nothing about another
        # store, or another company's store that happens to share its code.
        mine = [store.pk for store in viewer_stores(request.user)]
        kept = (
            BrandReportFile.objects.select_related("store", "brand")
            .filter(pk=pk, store_id__in=mine)
            .first()
        )
        if kept is None:
            raise Refusal("NOT_FOUND", "That report file was not found.", status=404)
        when = brand_reports.period(kept.month.strftime("%Y-%m"))
        params = {
            "store": kept.store.code,
            "date_from": when.first.isoformat(),
            "date_to": when.upto.isoformat(),
        }
        try:
            scope = report_scope(request.user, params, BRAND_REPORTS)
        except Refusal as refusal:
            if refusal.code == "SCOPE_DENIED":
                # Out of scope is not found: a file id says nothing about another store.
                raise Refusal("NOT_FOUND", "That report file was not found.", status=404) from None
            raise
        record_export(
            request.user,
            report=f"{REPORT}:{kept.kind}",
            scope=scope,
            detail={
                "brand": kept.brand.code,
                "month": when.label,
                "rows": kept.rows,
                "file_id": kept.pk,
                "made": "monthly",
            },
        )
        return _file_response(bytes(kept.content), kept.content_type, kept.file_name)


# -- layouts -----------------------------------------------------------------------------


def _require_switch(request: Request) -> list[Store]:
    """The viewer's stores where brand reports are on; refused when there are none."""
    today = timezone.localdate().isoformat()
    scope = report_scope(request.user, {"date_from": today, "date_to": today}, BRAND_REPORTS)
    return scope.options


def _layouts_body(request: Request) -> dict[str, Any]:
    stores = _require_switch(request)
    tenant_id = _tenant(request, stores)
    brands = list(Brand.objects.filter(tenant_id=tenant_id, is_active=True).order_by("name"))
    own = {
        (row.brand_id, row.kind): row for row in BrandReportLayout.objects.filter(brand__in=brands)
    }

    def fields(kind: str) -> list[dict[str, Any]]:
        return [
            {
                "key": f.key,
                "label": f.label,
                "kind": f.kind,
                "formats": list(brand_layouts.FORMATS_OF_KIND[f.kind]),
                "can_total": f.kind in brand_layouts.TOTALLED,
            }
            for f in brand_layouts.FIELDS[kind]
        ]

    def choices(pairs: dict[str, str]) -> list[dict[str, str]]:
        return [{"key": key, "label": label} for key, label in pairs.items()]

    return {
        "can_edit": brand_layouts.may_edit(request.user),
        "field_catalogue": {kind: fields(kind) for kind in brand_layouts.KINDS},
        "formats": choices(brand_layouts.FORMATS),
        "placeholders": choices(brand_layouts.PLACEHOLDERS),
        "file_types": choices(brand_layouts.FILE_TYPES),
        "header_borders": choices(brand_layouts.HEADER_BORDERS),
        "standard": {
            kind: _layout_json(brand_layouts.standard(tenant_id, kind))
            for kind in brand_layouts.KINDS
        },
        "brands": [
            {
                **_brand(brand),
                **{
                    kind: _layout_json(own[(brand.pk, kind)]) if (brand.pk, kind) in own else None
                    for kind in brand_layouts.KINDS
                },
            }
            for brand in brands
        ],
    }


class BrandLayoutsView(APIView):
    """Read every layout; Accounts saves one."""

    permission_classes = [IsAuthenticated, CanReadReports]

    @extend_schema(operation_id="reports_brand_layouts", responses=BrandLayoutsSerializer)
    def get(self, request: Request) -> Response:
        return Response(BrandLayoutsSerializer(_layouts_body(request)).data)

    @extend_schema(
        operation_id="reports_brand_layouts_save",
        request=SaveLayoutRequestSerializer,
        responses=BrandLayoutSerializer,
    )
    def post(self, request: Request) -> Response:
        if not brand_layouts.may_edit(request.user):
            raise Refusal(
                "ACTION_DENIED", "Only Accounts can change a brand's report layout.", status=403
            )
        stores = _require_switch(request)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            command_id = uuid.UUID(str(data.get("command_id") or ""))
        except ValueError:
            raise Refusal("INVALID_REQUEST", "command_id must be a UUID.", status=400) from None
        kind = data.get("kind")
        if kind not in brand_layouts.KINDS:
            raise Refusal("INVALID_REQUEST", "kind must be sale or soh.", status=400)
        raw_revision = data.get("expected_revision")
        if raw_revision is not None and (
            not isinstance(raw_revision, int) or isinstance(raw_revision, bool)
        ):
            raise Refusal(
                "INVALID_REQUEST", "expected_revision must be a whole number.", status=400
            )
        tenant_id = _tenant(request, stores)
        brand = None if data.get("brand_id") is None else _brand_of(data.get("brand_id"), tenant_id)
        layout = data.get("layout")
        # Checked before the command too, so a wrong layout is refused with its reason
        # and leaves no command behind to replay.
        brand_layouts.clean(kind, layout)
        saved: dict[str, BrandReportLayout] = {}

        def handler(run: CommandRun) -> CommandResult:
            saved["row"] = brand_layouts.save(
                run,
                user=request.user,
                brand=brand,
                kind=kind,
                layout=layout if isinstance(layout, dict) else {},
                expected_revision=raw_revision,
            )
            return CommandResult(resource_type="brand_layout", resource_id=str(saved["row"].pk))

        result = execute_command(
            principal_for(request.user, tenant_id),
            CommandSpec(
                brand_layouts.SAVE_ACTION,
                command_id,
                {
                    "brand_id": brand.pk if brand else None,
                    "kind": kind,
                    "expected_revision": raw_revision,
                    "layout": layout,
                },
                subject_key=f"brand_layout:{brand.pk if brand else 'standard'}:{kind}",
            ),
            handler,
        )
        row = saved.get("row") or BrandReportLayout.objects.get(pk=int(result.resource_id or 0))
        return Response(BrandLayoutSerializer(_layout_json(row)).data)
