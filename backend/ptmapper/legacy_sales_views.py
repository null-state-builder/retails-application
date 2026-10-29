"""Upload, preview and apply the legacy software's sales report (test tenant only)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from django.db.models import Count, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, check_query, parse_int_id, parse_meta
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from files.goods_models import MAX_EVIDENCE_BYTES
from files.goods_services import XLSX, check_workbook, detect_media_type
from masters.models import Store
from ptmapper import legacy_sales_services as services
from ptmapper.legacy_sales_models import LegacySalesImport
from ptmapper.legacy_sales_parser import parse_sales
from ptmapper.stock_sync_services import require_test_store

ROW_FIELDS = ("ordinal", "business_date", "bill_no", "barcode", "quantity", "applied_qty",
              "outcome", "exception_code", "exception_note")


def _load(access: AccessContext, pk: uuid.UUID) -> LegacySalesImport:
    source = LegacySalesImport.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    if source is None or not access.can(services.ACTION, site_id=source.site_id):
        raise Refusal("NOT_FOUND", "That sales import was not found.")
    return source


def site_for(access: AccessContext, site_id: int, action: str) -> Store:
    access.require(action, site_id=site_id)
    store = Store.objects.filter(tenant_id=access.tenant_id, pk=site_id, is_active=True).first()
    if store is None:
        raise Refusal("NOT_FOUND", "That store was not found.")
    return store


def dto(source: LegacySalesImport, *, rows: bool = False) -> dict[str, Any]:
    counts = source.rows.aggregate(total=Count("id"), exceptions=Count("id", filter=Q(outcome="exception")))
    out: dict[str, Any] = {
        "id": str(source.pk), "site_id": source.site_id, "state": source.state,
        "source_name": source.source_name, "source_hash": source.source_hash,
        "date_from": source.date_from.isoformat(), "date_to": source.date_to.isoformat(),
        "uploaded_at": source.created_at.isoformat() if source.created_at else None,
        "applied_at": source.applied_at.isoformat() if source.applied_at else None,
        "metadata": {key: source.metadata.get(key) for key in
                     ("row_count", "bill_count", "sold_quantity", "returned_quantity", "net_paise")},
        "outcome": source.outcome,
        "row_count": counts["total"], "exception_count": counts["exceptions"],
        "allowed_actions": ["apply", "discard"] if source.state == LegacySalesImport.State.PREVIEWED else [],
    }
    if source.state == LegacySalesImport.State.PREVIEWED:
        out["preview"] = {"test_bills_to_reverse": len(services.bills_for(source.site, source.date_from, source.date_to))}
    if rows:
        listed = source.rows.order_by("outcome", "ordinal").values(*ROW_FIELDS)
        out["rows"] = [{**row, "business_date": row["business_date"].isoformat()} for row in listed]
    return out


class LegacySalesListView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_legacy_sales_list", responses={200: dict})
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"site_id"})
        site_id = parse_int_id(params.get("site_id"), "site_id")
        site_for(access, site_id, services.ACTION)
        sources = LegacySalesImport.objects.filter(tenant_id=access.tenant_id, site_id=site_id).order_by("-date_to", "-created_at")[:60]
        result = {"items": [dto(source) for source in sources]}
        access.revalidate_delivery()
        return Response(result)


class LegacySalesUploadView(GoodsAPIView):
    parser_classes = [MultiPartParser]

    @extend_schema(operation_id="goods_v1_legacy_sales_upload", request={"multipart/form-data": OpenApiTypes.OBJECT},
                   responses={201: dict})
    def post(self, request: Request) -> Response:
        access = self.access(request)
        site_id = parse_int_id(request.data.get("site_id"), "site_id")
        require_test_store(site_for(access, site_id, services.ACTION))
        uploaded = request.FILES.get("file")
        if uploaded is None or (uploaded.size is not None and uploaded.size > MAX_EVIDENCE_BYTES):
            raise Refusal("LEGACY_SALES_INVALID", "Attach the sales report XLSX (at most 20 MB).", status=422)
        data = uploaded.read(MAX_EVIDENCE_BYTES + 1)
        if detect_media_type(data, uploaded.name or "sales.xlsx") != XLSX:
            raise Refusal("LEGACY_SALES_INVALID", "The sales report must be an XLSX workbook.", status=422)
        check_workbook(data)
        metadata, parsed = parse_sales(data)
        digest = hashlib.sha256(data).hexdigest()
        meta = parse_meta({"command_id": str(request.data.get("command_id")), "contract_version": "goods-v1"},
                          revision_bound=False)

        def handler(run: CommandRun) -> CommandResult:
            source = services.create_import(run, site_id=site_id, source_hash=digest,
                                            source_name=uploaded.name or "sales.xlsx", metadata=metadata, rows=parsed)
            return CommandResult(resource_type="legacy_sales_import", resource_id=str(source.pk), status_code=201)

        result = self.run_command(request, access=access, action=services.ACTION, meta=meta,
                                  business_input={"site_id": site_id, "source_hash": digest}, handler=handler,
                                  site_id=site_id, evidence_hashes=[digest])
        body = dto(_load(access, uuid.UUID(str(result.resource_id))), rows=True)
        access.revalidate_delivery()
        return Response(body, status=201)


class LegacySalesDetailView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_legacy_sales_detail", responses={200: dict})
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        body = dto(_load(access, pk), rows=True)
        access.revalidate_delivery()
        return Response(body)


class LegacySalesMutationView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_legacy_sales_mutate", request=OpenApiTypes.OBJECT, responses={200: dict})
    def post(self, request: Request, pk: uuid.UUID, operation: str) -> Response:
        access = self.access(request)
        source = _load(access, pk)
        if operation not in {"apply", "discard"}:
            raise Refusal("NOT_FOUND", "That operation was not found.")
        access.require(services.ACTION, site_id=source.site_id)
        meta = parse_meta(request.data, revision_bound=False)

        def handler(run: CommandRun) -> CommandResult:
            if operation == "apply":
                services.apply(run, source)
            else:
                services.discard(run, source)
            return CommandResult(resource_type="legacy_sales_import", resource_id=str(source.pk))

        self.run_command(request, access=access, action=services.ACTION, meta=meta, business_input={"operation": operation},
                         handler=handler, resource_ids=[str(pk)], site_id=source.site_id)
        body = dto(_load(access, pk), rows=True)
        access.revalidate_delivery()
        return Response(body)
