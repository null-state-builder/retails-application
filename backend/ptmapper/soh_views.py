"""First-party SOH staging, reviewed mappings and resumable child manifests."""

from __future__ import annotations

import math
import uuid
from typing import Any

from django.db.models import Count, Q, Sum
from drf_spectacular.utils import extend_schema
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, business_body, check_query, decode_cursor, encode_cursor, page, parse_int_id, parse_meta
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from files.goods_models import MAX_EVIDENCE_BYTES
from files.goods_services import check_workbook, detect_media_type, stage_upload, XLSX
from masters.models import Store
from masters.goods_identity_services import vocabulary
from core.commands import database_now
from ptmapper import soh_services as services
from ptmapper.soh_models import SohImport
from ptmapper.soh_parser import parse_soh

SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["id", "record_contract", "revision", "state", "site_id", "source_name", "source_hash", "allowed_actions", "data"],
    "properties": {
        "id": {"type": "string", "format": "uuid"}, "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer", "minimum": 1}, "state": {"type": "string"},
        "content_hash": {"type": "string"}, "site_id": {"type": "string"}, "source_name": {"type": "string"},
        "source_hash": {"type": "string"}, "source_evidence_id": {"type": "string", "format": "uuid", "nullable": True},
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
        "data": {"type": "object", "properties": {
            "metadata": {"type": "object", "additionalProperties": True}, "configuration": {"type": "object", "additionalProperties": True},
            **{key: {"type": "integer", "minimum": 0} for key in ("included_rows", "included_quantity", "excluded_stocked_rows", "verified_rows", "batch_count")},
            "approval_request_id": {"type": "string", "format": "uuid", "nullable": True},
            "requires_inventory_reconciliation": {"type": "boolean"},
            "stock_reconciliation_id": {"type": "string", "format": "uuid", "nullable": True},
            "field_access": {"type": "object", "required": ["readable_fields", "writable_fields"], "properties": {
                key: {"type": "array", "items": {"type": "string"}} for key in ("readable_fields", "writable_fields")}},
            "batches": {"type": "array", "items": {"type": "object", "properties": {
                "index": {"type": "integer"}, "manifest_id": {"type": "string", "format": "uuid"},
                "row_count": {"type": "integer"}, "quantity": {"type": "integer"}, "manifest_state": {"type": "string"}}}},
        }},
    },
}
PAGE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"items": {"type": "array", "items": SCHEMA}, "next_cursor": {"type": "string", "nullable": True}}}
UPLOAD_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False,
    "required": ["file", "site_id", "command_id", "contract_version", "expected_sha256"], "properties": {
        "file": {"type": "string", "format": "binary"}, "site_id": {"type": "integer", "minimum": 1},
        "command_id": {"type": "string", "format": "uuid"}, "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64}}}
MUTATION_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": False,
    "required": ["command_id", "contract_version", "expected_revision"], "properties": {
        "command_id": {"type": "string", "format": "uuid"}, "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer", "minimum": 1}, "configuration": {"type": "object", "additionalProperties": True},
        "batch_index": {"type": "integer", "minimum": 1}, "reason": {"type": "string"},
        "observations": {"type": "array", "minItems": 1, "maxItems": 5000, "items": {"type": "object", "required": ["barcode", "observed_qty", "observed_condition", "reason"], "properties": {
            "barcode": {"type": "string"}, "observed_qty": {"type": "integer", "minimum": 0},
            "observed_condition": {"type": "string", "enum": ["good", "damaged", "wrong", "unidentified"]}, "reason": {"type": "string"}}}},
    }}


def _load(access: AccessContext, pk: uuid.UUID) -> SohImport:
    source = SohImport.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
    if source is None:
        raise Refusal("NOT_FOUND", "That SOH source was not found.")
    if not any(access.covers_all({action}, services.source_cells(source)) for action in (services.STAGE, services.PREPARE, services.APPROVE)):
        raise Refusal("NOT_FOUND", "That SOH source was not found.")
    return source


def _cost(access: AccessContext, source: SohImport) -> bool:
    try:
        services._require(access, services.PREPARE, source, fields=("cost",))
    except Refusal:
        try:
            services._require(access, services.APPROVE, source, fields=("cost",))
        except Refusal:
            return False
    return True


def _can_prepare(access: AccessContext, source: SohImport) -> bool:
    if source.prepared_by_id != access.human_id:
        return False
    try:
        services._require(access, services.PREPARE, source, fields=("cost",))
    except Refusal:
        return False
    return True


def _financial(access: AccessContext, source: SohImport) -> bool:
    """Raw financial evidence needs both fields in one qualifying assignment.

    The fixed own-PT cost exception permits row valuation, not external-book
    reconciliation, source footer values or reading the opaque source file.
    """
    return any(access.covers_all({action}, services.source_cells(source), {"cost", "financial"})
               for action in (services.PREPARE, services.APPROVE))


def source_dto(access: AccessContext, source: SohImport) -> dict[str, Any]:
    from outbound.goods_soh_models import SohReconciliation

    reconciliation = SohReconciliation.objects.filter(source_import=source).first()
    existing_stock = services.requires_inventory_reconciliation(source)
    includes = source.rows.filter(quantity__gt=0, exclusion_reason="")
    count = includes.count()
    protected = _cost(access, source)
    financial = _financial(access, source)
    writable = _can_prepare(access, source)
    allowed = []
    if source.prepared_by_id is None and not source.batches.exists() and source.state != "withdrawn":
        try:
            services._require(access, services.PREPARE, source, fields=("cost",), claim=True)
        except Refusal:
            pass
        else:
            allowed.append("claim")
    if access.covers_all({services.STAGE}, services.source_cells(source)) and not source.batches.exists() and source.state != "withdrawn":
        allowed.append("verify")
    if writable and source.state != "withdrawn":
        allowed.extend(["prepare", "submit"] if not source.batches.exists() else [])
        if source.state in {"approved", "applied"} and not services.requires_inventory_reconciliation(source):
            allowed.append("apply")
    if access.covers_all({services.APPROVE}, services.source_cells(source), {"cost", "financial"}):
        allowed.append("approve")
        if not source.batches.exists() and source.state != "withdrawn":
            allowed.append("withdraw")
    if (existing_stock and reconciliation is None and source.state == "approved"
            and access.covers_all({"count.run"}, {(source.site_id, None)})):
        allowed.append("reconcile")
    config = dict(source.configuration)
    if not protected:
        config = {key: value for key, value in config.items() if key in {"brand_mappings", "season_mappings", "size_mappings", "hsn_mappings", "cutoff_at"}}
    if not financial:
        for key in services.FINANCIAL_NOTES:
            config.pop(key, None)
    metadata = {key: value for key, value in source.source_metadata.items() if key in {"sheet", "header_row", "row_count", "stocked_rows", "quantity", "zero_stock_rows", "negative_rows", "stock_mrp_paise", "rate_meaning", "columns"}}
    if financial:
        metadata.update({key: source.source_metadata[key] for key in ("source_amount_paise", "quantity_rate_paise", "source_rate_difference_paise") if key in source.source_metadata})
    return {"id": str(source.pk), "record_contract": "goods-v1", "revision": source.revision, "state": source.state,
            "content_hash": source.reviewed_hash, "site_id": str(source.site_id), "source_name": source.source_name,
            "source_hash": source.source_hash, "source_evidence_id": str(source.source_evidence_id) if financial else None,
            "allowed_actions": allowed,
            "data": {"metadata": metadata, "configuration": config,
                     "included_rows": count, "included_quantity": includes.aggregate(total=Sum("quantity"))["total"] or 0,
                     "excluded_stocked_rows": source.rows.filter(quantity__gt=0).exclude(exclusion_reason="").count(),
                     "verified_rows": includes.exclude(verification={}).count(), "batch_count": math.ceil(count / services.MAX_BATCH),
                     "approval_request_id": str(source.approval_request_id) if source.approval_request_id else None,
                     "requires_inventory_reconciliation": existing_stock,
                     "stock_reconciliation_id": str(reconciliation.pk) if reconciliation else None,
                     "field_access": {"readable_fields": (["cost"] if protected else []) + (["financial"] if financial else []),
                                      "writable_fields": (["cost"] if writable else []) + (["financial"] if writable and financial else [])},
                     "batches": [{"index": b.batch_index, "manifest_id": str(b.manifest_id), "row_count": b.row_count,
                                  "quantity": b.quantity, "manifest_state": "approved" if b.manifest.approved_version_id else "draft"}
                                 for b in source.batches.select_related("manifest").order_by("batch_index")]}}


class SohImportListView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_soh_import_list", responses={200: PAGE_SCHEMA})
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"site_id", "cursor", "limit"})
        source_rows = SohImport.objects.filter(tenant_id=access.tenant_id)
        if params.get("site_id"):
            source_rows = source_rows.filter(site_id=parse_int_id(params["site_id"], "site_id"))
        rows = [row for row in source_rows.order_by("-created_at", "id") if any(access.covers_all({a}, services.source_cells(row)) for a in (services.STAGE, services.PREPARE, services.APPROVE))]
        offset = decode_cursor(params.get("cursor"))
        window = rows[offset:offset + 25]
        result = page([source_dto(access, row) for row in window], encode_cursor(offset + 25) if offset + 25 < len(rows) else None)
        access.revalidate_delivery()
        return Response(result)


class SohImportUploadView(GoodsAPIView):
    parser_classes = [MultiPartParser]

    @extend_schema(operation_id="goods_v1_soh_import_upload", request={"multipart/form-data": UPLOAD_SCHEMA}, responses={201: SCHEMA})
    def post(self, request: Request) -> Response:
        access = self.access(request)
        site_id = parse_int_id(request.data.get("site_id"), "site_id")
        access.require(services.STAGE, site_id=site_id)
        if not Store.objects.filter(tenant_id=access.tenant_id, pk=site_id, is_active=True).exists():
            raise Refusal("NOT_FOUND", "That store was not found.")
        uploaded = request.FILES.get("file")
        if uploaded is None or (uploaded.size is not None and uploaded.size > MAX_EVIDENCE_BYTES):
            raise Refusal("SOH_SOURCE_INVALID", "Attach an XLSX source of at most 20 MB.", status=422)
        data = uploaded.read(MAX_EVIDENCE_BYTES + 1)
        if detect_media_type(data, uploaded.name or "source.xlsx") != XLSX:
            raise Refusal("SOH_SOURCE_INVALID", "SOH sources must be XLSX workbooks.", status=422)
        check_workbook(data)
        metadata, parsed = parse_soh(data)
        # Opaque upload does not grant reading the raw financial source. Its field
        # classification is server-owned even if the uploader has no cost grant.
        try:
            command_id = uuid.UUID(str(request.data.get("command_id")))
        except ValueError:
            raise Refusal("INVALID_REQUEST", "Provide a command_id UUID.") from None
        if request.data.get("contract_version") != "goods-v1":
            raise Refusal("INVALID_REQUEST", "contract_version must be goods-v1.")
        evidence = stage_upload(access.principal(), command_id=uuid.uuid5(command_id, "source"), data=data,
                                filename=uploaded.name or "source.xlsx", kind="manifest",
                                scope={"scope_kind": "sites", "site_ids": [site_id], "brand_ids": [], "sensitive_fields": ["cost", "financial"]},
                                expected_sha256=str(request.data.get("expected_sha256") or ""), contains_fields=["cost", "financial"])
        meta = parse_meta({"command_id": str(command_id), "contract_version": "goods-v1"}, revision_bound=False)

        def handler(run: CommandRun) -> CommandResult:
            source = services.create_source(run, site_id=site_id, evidence=evidence, metadata=metadata, parsed=parsed)
            return CommandResult(resource_type="soh_import", resource_id=str(source.pk), status_code=201)

        result = self.run_command(request, access=access, action=services.STAGE, meta=meta,
                                  business_input={"site_id": site_id, "source_hash": evidence.sha256}, handler=handler,
                                  site_id=site_id, evidence_hashes=[evidence.sha256])
        dto = source_dto(access, _load(access, uuid.UUID(str(result.resource_id))))
        access.revalidate_delivery()
        return Response(dto, status=201)


class SohImportDetailView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_soh_import_detail", responses={200: SCHEMA})
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        check_query(request, set())
        dto = source_dto(access, _load(access, pk))
        access.revalidate_delivery()
        return Response(dto)


class SohImportRowsView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_soh_import_rows", responses={200: {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": True}}, "next_cursor": {"type": "string", "nullable": True},
        "total": {"type": "integer"}, "revision": {"type": "integer"}}}})
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, {"cursor", "q", "limit", "stocked", "group"})
        source = _load(access, pk)
        protected = _cost(access, source)
        rows = source.rows.order_by("ordinal")
        if params.get("group") == "vocabulary":
            values = vocabulary(access.tenant_id, database_now())
            access.revalidate_delivery()
            return Response({"items": [{"id": str(value.id), "dimension": dimension, "label": value.label,
                                          "value_key": value.value_key, "config_version_id": str(value.config_version_id)}
                                        for dimension, entries in values.items() for value in entries if not value.retired], "next_cursor": None})
        if params.get("stocked") == "1":
            rows = rows.filter(quantity__gt=0)
        if params.get("q"):
            rows = rows.filter(Q(barcode__icontains=params["q"]) | Q(source_brand__icontains=params["q"]))
        if params.get("group") == "brand":
            groups = list(rows.values("source_brand").annotate(row_count=Count("id"), quantity=Sum("quantity")).order_by("source_brand"))
            access.revalidate_delivery()
            return Response({"items": groups, "next_cursor": None})
        if params.get("group") in {"season", "size", "category"}:
            key = f"source__{params['group']}"
            groups = list(rows.values(key).annotate(row_count=Count("id"), quantity=Sum("quantity")).order_by(key))
            access.revalidate_delivery()
            return Response({"items": [{"source_value": row[key] or "", "row_count": row["row_count"], "quantity": row["quantity"]} for row in groups], "next_cursor": None})
        offset = decode_cursor(params.get("cursor"))
        try:
            limit = min(max(int(params.get("limit") or 100), 1), 5000)
        except ValueError:
            raise Refusal("INVALID_REQUEST", "limit must be a whole number.") from None
        window = list(rows[offset:offset + limit + 1])
        items = []
        for row in window[:limit]:
            mapping = dict(row.mapping)
            if not protected:
                mapping.pop("basic_paise", None)
            item: dict[str, Any] = {"barcode": row.barcode, "source_row_key": row.source_row_key,
                                    "brand": row.source_brand, "quantity": row.quantity, "mrp_paise": str(row.mrp_paise) if row.mrp_paise is not None else None,
                                    "item_name": row.source.get("itemname"), "size": row.source.get("size"), "category": row.source.get("category"),
                                    "season": row.source.get("season"), "mapping": mapping,
                                    "verification": {key: value for key, value in row.verification.items() if key != "observer_id"},
                                    "exclusion_reason": row.exclusion_reason}
            if protected:
                item["source_rate_paise"] = str(row.source_rate_paise) if row.source_rate_paise is not None else None
            items.append(item)
        access.revalidate_delivery()
        return Response({"items": items, "next_cursor": encode_cursor(offset + limit) if len(window) > limit else None,
                         "total": rows.count(), "revision": source.revision})


class SohImportMutationView(GoodsAPIView):
    @extend_schema(operation_id="goods_v1_soh_import_mutate", request={"application/json": MUTATION_SCHEMA}, responses={200: SCHEMA})
    def post(self, request: Request, pk: uuid.UUID, operation: str) -> Response:
        access = self.access(request)
        source = _load(access, pk)
        action = services.STAGE if operation == "verify" else services.APPROVE if operation == "withdraw" else services.PREPARE
        if operation not in {"verify", "withdraw"}:
            services._require(access, action, source, fields=("cost",), claim=operation == "claim")
        else:
            services._require(access, action, source)
        meta = parse_meta(request.data, revision_bound=True)
        fields = {"configuration"} if operation == "prepare" else {"observations"} if operation == "verify" else {"batch_index"} if operation == "apply" else {"reason"} if operation == "withdraw" else set()
        body = business_body(request.data, fields, required=fields)

        def handler(run: CommandRun) -> CommandResult:
            if operation == "claim":
                services.claim_preparation(run, access, source, meta.expected_revision)
            elif operation == "prepare":
                services.prepare(run, access, source, body["configuration"], meta.expected_revision)
            elif operation == "verify":
                services.verify_rows(run, access, source, body["observations"], meta.expected_revision)
            elif operation == "submit":
                services.submit(run, access, source, meta.expected_revision)
            elif operation == "apply":
                if isinstance(body["batch_index"], bool) or not isinstance(body["batch_index"], int):
                    raise Refusal("INVALID_REQUEST", "batch_index must be a whole number.")
                services.apply_batch(run, access, source, body["batch_index"], meta.expected_revision)
            elif operation == "withdraw":
                services.withdraw(run, access, source, str(body["reason"]), meta.expected_revision)
            else:
                raise Refusal("NOT_FOUND", "That SOH operation was not found.")
            return CommandResult(resource_type="soh_import", resource_id=str(source.pk))

        self.run_command(request, access=access, action=f"opening.import.{operation}", meta=meta, business_input=body,
                         handler=handler, resource_ids=[str(pk)], site_id=source.site_id)
        dto = source_dto(access, _load(access, pk))
        access.revalidate_delivery()
        return Response(dto)
