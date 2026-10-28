"""Upload, preview and apply the legacy SOH as the store's stock (test tenant only)."""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Count, Q
from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, check_query, page_limit, parse_int_id, parse_meta
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from files.goods_models import MAX_EVIDENCE_BYTES
from files.goods_services import XLSX, check_workbook, detect_media_type, stage_upload
from masters.store_features import is_real_store
from ptmapper import stock_sync_services as services
from ptmapper.legacy_sales_models import LegacySalesImport
from ptmapper.legacy_sales_views import dto as sales_dto
from ptmapper.legacy_sales_views import site_for
from ptmapper.soh_parser import parse_soh
from ptmapper.stock_sync_models import StockSync, StockSyncRow

ROW_FIELDS = ("id", "barcode", "soh_qty", "book_qty", "applied_qty", "outcome", "exception_code", "exception_note",
              "category", "mrp_differs", "soh_mrp_paise", "book_mrp_paise", "review_state", "reviewed_by_id",
              "reviewed_at")
#: Detail keeps a short sample; the review list pages through every row.
DETAIL_ROWS = 200
MAX_PAGE_SIZE = 500
CATEGORIES = {choice.value for choice in StockSyncRow.Category}


def _load(access: AccessContext, pk: uuid.UUID) -> StockSync:
    sync = StockSync.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    if sync is None or not access.can(services.ACTION, site_id=sync.site_id):
        raise Refusal("NOT_FOUND", "That stock sync was not found.")
    return sync


def _row(values: dict[str, Any]) -> dict[str, Any]:
    out = dict(values)
    out["id"] = str(out["id"])
    out["reviewed_by_id"] = str(out["reviewed_by_id"]) if out["reviewed_by_id"] else None
    out["reviewed_at"] = out["reviewed_at"].isoformat() if out["reviewed_at"] else None
    return out


def review_counts(sync: StockSync) -> dict[str, Any]:
    """Rows per category (``MRP_DIFFERS`` counts every MRP mismatch, quantity-flagged or not) and per review state."""
    pairs = list(sync.rows.values_list("category", "mrp_differs"))
    open_rows = sync.rows.filter(review_state=StockSyncRow.ReviewState.OPEN).count()
    return {"categories": services.category_counts(pairs), "total": len(pairs), "open": open_rows,
            "acknowledged": len(pairs) - open_rows}


def dto(sync: StockSync, *, rows: bool = False) -> dict[str, Any]:
    counts = sync.rows.aggregate(total=Count("id"), exceptions=Count("id", filter=Q(outcome="exception")),
                                 new_items=Count("id", filter=Q(outcome="new_item")))
    out: dict[str, Any] = {
        "id": str(sync.pk), "site_id": sync.site_id, "state": sync.state, "source_name": sync.source_name,
        "cutoff_at": sync.cutoff_at.isoformat(),
        "uploaded_at": sync.created_at.isoformat() if sync.created_at else None,
        "applied_at": sync.applied_at.isoformat() if sync.applied_at else None,
        "metadata": {key: sync.metadata.get(key) for key in ("row_count", "quantity")},
        "outcome": sync.outcome, "changed_rows": counts["total"], "exception_count": counts["exceptions"],
        "new_item_rows": counts["new_items"],
        # Historical: syncs applied before SOH-only rows became flag-only.
        "top_up_import_id": str(sync.top_up_import_id) if sync.top_up_import_id else None,
        "allowed_actions": ["apply", "discard"] if sync.state == StockSync.State.PREVIEWED else [],
    }
    if sync.state == StockSync.State.PREVIEWED:
        out["preview"] = services.preview(sync)
    if sync.state == StockSync.State.APPLIED:
        out["review"] = review_counts(sync)
    if rows:
        out["rows"] = [_row(r) for r in sync.rows.order_by("category", "barcode").values(*ROW_FIELDS)[:DETAIL_ROWS]]
    return out


class LegacySyncOverviewView(GoodsAPIView):
    """Both nightly options for one store: SOH replaces and sales-report imports."""

    @extend_schema(operation_id="goods_v1_legacy_sync_overview", responses={200: dict})
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"site_id"})
        site_id = parse_int_id(params.get("site_id"), "site_id")
        store = site_for(access, site_id, services.ACTION)
        syncs = StockSync.objects.filter(tenant_id=access.tenant_id, site_id=site_id).order_by("-cutoff_at")[:30]
        sales = LegacySalesImport.objects.filter(tenant_id=access.tenant_id, site_id=site_id).order_by("-date_to", "-created_at")[:30]
        last_sales = sales.model.objects.filter(tenant_id=access.tenant_id, site_id=site_id,
                                                state=LegacySalesImport.State.APPLIED).order_by("-date_to").first()
        cutoff = services.previous_cutoff(store)
        result = {"test_environment": not is_real_store(store),
                  "last_soh_cutoff": cutoff.isoformat() if cutoff else None,
                  "last_sales_date": last_sales.date_to.isoformat() if last_sales else None,
                  "soh_syncs": [dto(sync) for sync in syncs], "sales_imports": [sales_dto(row) for row in sales]}
        access.revalidate_delivery()
        return Response(result)


class StockSyncUploadView(GoodsAPIView):
    parser_classes = [MultiPartParser]

    @extend_schema(operation_id="goods_v1_stock_sync_upload", request={"multipart/form-data": OpenApiTypes.OBJECT},
                   responses={201: dict})
    def post(self, request: Request) -> Response:
        access = self.access(request)
        site_id = parse_int_id(request.data.get("site_id"), "site_id")
        services.require_test_store(site_for(access, site_id, services.ACTION))
        cutoff = parse_datetime(str(request.data.get("cutoff_at") or ""))
        if cutoff is None or cutoff.tzinfo is None:
            raise Refusal("INVALID_REQUEST", "Give the time the SOH was exported, with its timezone.")
        uploaded = request.FILES.get("file")
        if uploaded is None or (uploaded.size is not None and uploaded.size > MAX_EVIDENCE_BYTES):
            raise Refusal("SOH_SOURCE_INVALID", "Attach the SOH XLSX (at most 20 MB).", status=422)
        data = uploaded.read(MAX_EVIDENCE_BYTES + 1)
        if detect_media_type(data, uploaded.name or "soh.xlsx") != XLSX:
            raise Refusal("SOH_SOURCE_INVALID", "The SOH must be an XLSX workbook.", status=422)
        check_workbook(data)
        metadata, parsed = parse_soh(data)
        try:
            command_id = uuid.UUID(str(request.data.get("command_id")))
        except ValueError:
            raise Refusal("INVALID_REQUEST", "Provide a command_id UUID.") from None
        evidence = stage_upload(access.principal(), command_id=uuid.uuid5(command_id, "source"), data=data,
                                filename=uploaded.name or "soh.xlsx", kind="manifest",
                                scope={"scope_kind": "sites", "site_ids": [site_id], "brand_ids": [],
                                       "sensitive_fields": ["cost", "financial"]},
                                expected_sha256=str(request.data.get("expected_sha256") or ""),
                                contains_fields=["cost", "financial"])
        meta = parse_meta({"command_id": str(command_id), "contract_version": "goods-v1"}, revision_bound=False)

        def handler(run: CommandRun) -> CommandResult:
            sync = services.create_sync(run, site_id=site_id, evidence=evidence, cutoff_at=cutoff,
                                        metadata=metadata, parsed=parsed)
            return CommandResult(resource_type="stock_sync", resource_id=str(sync.pk), status_code=201)

        result = self.run_command(request, access=access, action=services.ACTION, meta=meta,
                                  business_input={"site_id": site_id, "source_hash": evidence.sha256,
                                                  "cutoff_at": cutoff.isoformat()},
                                  handler=handler, site_id=site_id, evidence_hashes=[evidence.sha256])
        body = dto(_load(access, uuid.UUID(str(result.resource_id))))
        access.revalidate_delivery()
        return Response(body, status=201)


class StockSyncDetailView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_stock_sync_detail", responses={200: dict})
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        body = dto(_load(access, pk), rows=True)
        access.revalidate_delivery()
        return Response(body)


class StockSyncMutationView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_stock_sync_mutate", request=OpenApiTypes.OBJECT, responses={200: dict})
    def post(self, request: Request, pk: uuid.UUID, operation: str) -> Response:
        access = self.access(request)
        sync = _load(access, pk)
        if operation not in {"apply", "discard"}:
            raise Refusal("NOT_FOUND", "That operation was not found.")
        access.require(services.ACTION, site_id=sync.site_id)
        meta = parse_meta(request.data, revision_bound=False)

        def handler(run: CommandRun) -> CommandResult:
            if operation == "apply":
                services.apply(run, sync)
            else:
                services.discard(run, sync)
            return CommandResult(resource_type="stock_sync", resource_id=str(sync.pk))

        self.run_command(request, access=access, action=services.ACTION, meta=meta, business_input={"operation": operation},
                         handler=handler, resource_ids=[str(pk)], site_id=sync.site_id)
        body = dto(_load(access, pk), rows=True)
        access.revalidate_delivery()
        return Response(body)


class StockSyncRowsView(GoodsAPIView):
    """The flagged rows of one sync, a page at a time, with counts per category.

    ``?category=`` narrows to one category (``MRP_DIFFERS`` means every row whose
    MRP differs); ``?review_state=open|acknowledged`` to one review state.
    """

    @extend_schema(operation_id="goods_v1_stock_sync_rows", responses={200: dict})
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, {"category", "review_state", "offset", "limit"})
        sync = _load(access, pk)
        rows = sync.rows.all()
        category = params.get("category") or ""
        if category and category not in CATEGORIES:
            raise Refusal("INVALID_REQUEST", f"category must be one of {', '.join(sorted(CATEGORIES))}.")
        if category == StockSyncRow.Category.MRP_DIFFERS:
            rows = rows.filter(mrp_differs=True)
        elif category:
            rows = rows.filter(category=category)
        state = params.get("review_state") or ""
        if state and state not in {choice.value for choice in StockSyncRow.ReviewState}:
            raise Refusal("INVALID_REQUEST", "review_state must be open or acknowledged.")
        if state:
            rows = rows.filter(review_state=state)
        limit = page_limit(params, default=100, maximum=MAX_PAGE_SIZE)
        try:
            offset = max(int(params.get("offset") or 0), 0)
        except ValueError:
            raise Refusal("INVALID_REQUEST", "offset must be an integer.") from None
        total = rows.count()
        page = [_row(r) for r in rows.order_by("category", "barcode").values(*ROW_FIELDS)[offset:offset + limit]]
        body = {"sync_id": str(sync.pk), "state": sync.state, "counts": review_counts(sync), "total": total,
                "offset": offset, "limit": limit, "rows": page}
        access.revalidate_delivery()
        return Response(body)


class StockSyncAcknowledgeView(GoodsAPIView):
    """Mark flagged rows reviewed. It changes no stock and no item: that is the SOH's job."""

    @extend_schema(operation_id="goods_v1_stock_sync_rows_acknowledge", request=OpenApiTypes.OBJECT,
                   responses={200: dict})
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        sync = _load(access, pk)
        access.require(services.ACTION, site_id=sync.site_id)
        meta = parse_meta(request.data, revision_bound=False)
        raw = request.data.get("row_ids")
        if not isinstance(raw, list) or not raw:
            raise Refusal("INVALID_REQUEST", "Give row_ids: the rows to acknowledge.")
        try:
            row_ids = {uuid.UUID(str(value)) for value in raw}
        except ValueError:
            raise Refusal("INVALID_REQUEST", "Every row id must be a UUID.") from None
        holder: dict[str, Any] = {}

        def handler(run: CommandRun) -> CommandResult:
            holder["result"] = services.acknowledge(run, sync, row_ids)
            return CommandResult(resource_type="stock_sync", resource_id=str(sync.pk))

        self.run_command(request, access=access, action=services.ACTION, meta=meta,
                         business_input={"operation": "acknowledge", "row_ids": sorted(str(i) for i in row_ids)},
                         handler=handler, resource_ids=[str(pk)], site_id=sync.site_id)
        body = {"sync_id": str(sync.pk), **holder.get("result", {}), "counts": review_counts(_load(access, pk))}
        access.revalidate_delivery()
        return Response(body)
