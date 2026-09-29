"""Setup > Audit Log (store operations PRD ST-OPS-3, ticket 02).

Under ``/api/goods-v1/masters/``, read-only:

``GET audit-log``              one page of entries, newest first, with the filters'
                               choices (``audit.view``; see ``masters.audit_log``)
``GET audit-log/export.xlsx``  the same filtered entries as a spreadsheet

There is no write route: an entry cannot be edited or deleted through this API
(any other method is 405), and the audit table refuses it in the database too.
Taking a copy is itself recorded, as ``audit.log.export``, so the log shows who
exported it.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import GoodsAPIView, check_query, page_limit
from core.commands import CommandResult, CommandRun, CommandSpec, execute_command
from core.refusals import Refusal
from masters import audit_log
from masters.goods_views import REFUSAL_RESPONSE
from masters.store_features import feature
from ptmapper.goods_workbook import workbook_bytes

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NO_OPTIONS: dict[str, list[Any]] = {"stores": [], "people": [], "record_types": []}
FILTER_KEYS = ("person", "store", "record_type", "date_from", "date_to")


class AuditValueSerializer(serializers.Serializer[Any]):
    field = serializers.CharField()
    redacted = serializers.BooleanField()
    value = serializers.JSONField(allow_null=True)


class AuditEntrySerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    recorded_at = serializers.DateTimeField()
    event_at = serializers.DateTimeField()
    actor_id = serializers.CharField(allow_null=True)
    actor_name = serializers.CharField(
        allow_null=True, help_text="Shown only where your grants already reveal that person."
    )
    actor_hidden = serializers.BooleanField(help_text="A person made it, but is not shown to you.")
    service_code = serializers.CharField(allow_null=True)
    store_id = serializers.IntegerField(allow_null=True)
    store_code = serializers.CharField(allow_null=True)
    store_name = serializers.CharField(allow_null=True)
    action = serializers.CharField()
    record_type = serializers.CharField()
    record_key = serializers.CharField(allow_null=True)
    outcome = serializers.CharField()
    reason_code = serializers.CharField(allow_null=True)
    before = AuditValueSerializer(many=True, allow_null=True)
    after = AuditValueSerializer(many=True, allow_null=True)


class AuditStoreSerializer(serializers.Serializer[Any]):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class AuditPersonSerializer(serializers.Serializer[Any]):
    id = serializers.CharField()
    name = serializers.CharField()


class AuditLogSerializer(serializers.Serializer[Any]):
    items = AuditEntrySerializer(many=True)
    next_cursor = serializers.CharField(allow_null=True)
    stores = AuditStoreSerializer(many=True, help_text="First page only; empty on a cursor page.")
    people = AuditPersonSerializer(many=True, help_text="First page only; empty on a cursor page.")
    record_types = serializers.ListField(
        child=serializers.CharField(), help_text="First page only; empty on a cursor page."
    )
    as_of = serializers.DateTimeField()


_FILTER_PARAMETERS = [
    OpenApiParameter("person", str, description="A person id from `people`."),
    OpenApiParameter("store", int, description="A store id from `stores`."),
    OpenApiParameter("record_type", str, description="A record type from `record_types`."),
    OpenApiParameter("date_from", str, description="First day, YYYY-MM-DD, India time."),
    OpenApiParameter("date_to", str, description="Last day, YYYY-MM-DD, India time."),
]


def _scope(view: GoodsAPIView, request: Request) -> audit_log.Scope:
    access = view.access(request)
    return audit_log.reader_scope(access, feature(audit_log.FEATURE_KEY))


class GoodsAuditLogView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_audit_log",
        parameters=[
            *_FILTER_PARAMETERS,
            OpenApiParameter("cursor", str, description="Opaque next-page cursor."),
            OpenApiParameter("limit", int, description="Entries per page, 1 to 100."),
        ],
        responses={
            200: AuditLogSerializer,
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request) -> Response:
        params = check_query(request, allowed=(*FILTER_KEYS, "cursor", "limit"))
        scope = _scope(self, request)
        filters = audit_log.parse_filters(params)
        limit = page_limit(params, default=50, maximum=100)
        rows = audit_log.after_cursor(
            audit_log.filtered_events(scope, filters), params.get("cursor") or None
        )
        window = list(audit_log.ordered(rows)[: limit + 1])
        page = window[:limit]
        body = {
            "items": audit_log.entries(scope, page),
            "next_cursor": audit_log.encode_cursor(page[-1]) if len(window) > limit else None,
            # The filters' choices scan the whole scope, so only the first page
            # carries them; a "show older" page answers them empty.
            **(audit_log.options(scope) if not params.get("cursor") else NO_OPTIONS),
            "as_of": timezone.now(),
        }
        return Response(AuditLogSerializer(body).data)


class GoodsAuditLogExportView(GoodsAPIView):
    @extend_schema(
        operation_id="goods_v1_audit_log_export",
        parameters=_FILTER_PARAMETERS,
        responses={
            (200, XLSX): {
                "type": "string",
                "format": "binary",
                "description": "The filtered audit log as a spreadsheet.",
            },
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
            409: REFUSAL_RESPONSE,
        },
    )
    def get(self, request: Request) -> HttpResponse:
        params = check_query(request, allowed=FILTER_KEYS)
        scope = _scope(self, request)
        filters = audit_log.parse_filters(params)
        cap = int(settings.KDPS_AUDIT_LOG_EXPORT_MAX_ROWS)
        events = list(audit_log.ordered(audit_log.filtered_events(scope, filters))[: cap + 1])
        if len(events) > cap:
            raise Refusal(
                "EXPORT_TOO_LARGE",
                f"More than {cap} entries match. Narrow the dates or filters and export again.",
                status=409,
            )
        items = audit_log.entries(scope, events)
        _record_export(scope, filters, len(items))
        response = HttpResponse(
            workbook_bytes("Audit log", audit_log.export_rows(items)), content_type=XLSX
        )
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        response["Content-Disposition"] = f'attachment; filename="audit-log-{stamp}.xlsx"'
        return response


def _record_export(scope: audit_log.Scope, filters: audit_log.Filters, count: int) -> None:
    """Who took a copy of the log, with which filters. Failing to record it fails the export."""
    detail = {"filters": filters.as_dict(), "rows": count, "format": "xlsx"}

    def handler(run: CommandRun) -> CommandResult:
        run.audit_after = detail
        return CommandResult(resource_type="audit_log_export")

    execute_command(
        scope.access.principal(),
        CommandSpec(
            audit_log.EXPORT_ACTION,
            uuid.uuid4(),
            detail,
            subject_key="audit_log:export",
            site_id=filters.store_id if filters.store_id in scope.store_ids else None,
        ),
        handler,
    )
