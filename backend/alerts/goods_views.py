"""Goods-v1 notifications, owned exceptions and the change stream (E179, E180, E185-E188).

The three alert routes used to share ``/api/alerts``, ``/api/alerts/history``
and ``/api/alerts/seen`` with the legacy bell through the old contract
dispatcher, so drf-spectacular described the legacy operation and never these
(#303). They answer under ``/api/goods-v1/`` alone now, and the schemas below
are what this module emits: the views hand-build dict responses rather than run
a serializer, so nothing here is introspectable without them.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Iterator
from typing import Any

from django.db.models import F
from django.http import StreamingHttpResponse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.goods_api import (
    GoodsAPIView,
    business_body,
    check_query,
    decode_cursor,
    page,
    paginate,
    parse_int_id,
    parse_meta,
    parse_uuid,
    resource_dto,
)
from alerts.goods_models import (
    ExceptionEvent,
    GoodsException,
    GoodsNotification,
    NotificationAcknowledgement,
)
from alerts.goods_services import (
    add_exception_event,
    exception_dto,
    exception_event_dto,
    notification_dto,
)
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from core.tenancy import tenant_context

#: The grant that lets a person follow document changes on the stream.
STREAM_READER = "pt.view"


def document_cells(rows: list[Any]) -> dict[Any, frozenset[tuple[Any, Any]]]:
    """Each document's (site, brand) cells, as its own detail read judges them.

    A PT covers every line SKU's brand; a GRN or booking its brand; any other document
    has no brand, so only a grant not limited to a brand is told it changed.
    """
    from django.apps import apps

    from ptmapper.goods_pt_services import pt_cells

    sites = {row["document_id"]: row["document__site_id"] for row in rows}
    cells: dict[Any, frozenset[tuple[Any, Any]]] = {
        document_id: frozenset({(site_id, None)}) for document_id, site_id in sites.items()
    }
    if not sites:
        return cells
    ids = sorted(sites, key=str)
    for app_label, model_name, brand_path in (
        ("inbound", "GoodsGrn", "arrival__brand_id"),
        ("vendors", "GoodsBooking", "brand_id"),
    ):
        model = apps.get_model(app_label, model_name)
        for document_id, brand_id in model._default_manager.filter(document_id__in=ids).values_list(
            "document_id", brand_path
        ):
            cells[document_id] = frozenset({(sites[document_id], brand_id)})
    pts = apps.get_model("ptmapper", "GoodsPt")._default_manager
    for goods_pt in pts.select_related("document", "grn__arrival").filter(document_id__in=ids):
        cells[goods_pt.document_id] = pt_cells(goods_pt)
    return cells


#: How long one change-stream response stays open before the client reconnects.
STREAM_SECONDS = 20.0
STREAM_POLL = 2.0


NOTIFICATION_READERS = ("exception.view", "stock.view", "pt.view", "approvals.view")
EXCEPTION_READERS = ("exception.view", "exception.manage")


def _my_notifications(access: Any) -> list[GoodsNotification]:
    """My notifications whose site and brand my grants cover now.

    A notification with no site or no brand is a record without that dimension, so
    only a grant unlimited in it covers it - addressing alone is not authority.
    """
    rows = list(
        GoodsNotification.objects.filter(
            tenant_id=access.tenant_id, recipient_id=access.human_id
        ).order_by("-recorded_at", "id")
    )
    return [
        row
        for row in rows
        if any(
            access.can_at_store(action, row.site_id)
            if row.site_id is not None and row.brand_id is None
            else access.can(action, site_id=row.site_id, brand_id=row.brand_id)
            for action in NOTIFICATION_READERS
        )
    ]


REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}


def _page_response(item_schema: dict[str, Any], description: str) -> dict[str, Any]:
    """``Page<T>`` (design §6.1): a cursor-paged list envelope."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "items": {"type": "array", "items": item_schema},
            "next_cursor": {"type": "string", "nullable": True},
            "as_of": {"type": "string", "format": "date-time"},
        },
    }


#: `alerts.goods_services.notification_dto`.
NOTIFICATION_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "NotificationDTO (E179, E180).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "event_kind": {"type": "string"},
        "subject_id": {"type": "string"},
        "site_id": {"type": "string", "nullable": True},
        "title": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
        "due_at": {"type": "string", "format": "date-time", "nullable": True},
        "seen": {"type": "boolean"},
    },
}

#: `alerts.goods_services.exception_dto`.
EXCEPTION_DATA: dict[str, Any] = {
    "type": "object",
    "description": "ExceptionDTO (E185-E187).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "kind": {"type": "string"},
        "subject_id": {"type": "string"},
        "site_id": {"type": "string", "nullable": True},
        "owner_role": {"type": "string", "nullable": True},
        "owner_human_id": {"type": "string", "nullable": True},
        "opened_at": {"type": "string", "format": "date-time"},
        "due_at": {
            "type": "string",
            "format": "date-time",
            "nullable": True,
            "description": "Null when this exception's SLA is measured in working days and "
            "the business has approved no working calendar (GSA-T08): no deadline, never an "
            "inferred one. Such an exception is never overdue, and sorts last.",
        },
        "age_seconds": {"type": "integer"},
        "overdue": {"type": "boolean"},
        "state": {"type": "string", "enum": ["open", "resolved"]},
        "reason_code": {"type": "string", "nullable": True},
        "allowed_resolution_actions": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "revision": {"type": "integer"},
        "calendar_version_id": {
            "type": "string",
            "nullable": True,
            "description": "The approved working_calendar version that fixed due_at, if one did.",
        },
    },
}

#: `alerts.goods_services.exception_event_dto`.
EXCEPTION_EVENT_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "One row of an exception's event log, newest first (design §8.2).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "event_kind": {"type": "string"},
        "actor_id": {"type": "string", "nullable": True},
        "recorded_at": {"type": "string", "format": "date-time"},
        "reason_code": {"type": "string", "nullable": True},
        "payload": {"type": "object", "additionalProperties": True},
    },
}

EXCEPTION_RESOURCE: dict[str, Any] = {
    "type": "object",
    "description": "ResourceDTO<ExceptionDTO>.",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "revision": {"type": "integer"},
        "content_hash": {"type": "string"},
        "state": {"type": "string"},
        "number": {"type": "string", "nullable": True},
        "version": {"type": "integer", "nullable": True},
        "context": {"type": "object", "additionalProperties": True},
        "allowed_actions": {"type": "array", "items": {"type": "string"}},
        "data": EXCEPTION_DATA,
    },
}

#: E180's acknowledgement: the notification ids this call marked seen.
SEEN_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "The notification ids this call marked seen (E180).",
    "properties": {"seen": {"type": "array", "items": {"type": "string", "format": "uuid"}}},
}

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


class GoodsAlertInboxView(GoodsAPIView):
    """E179: my unseen notifications, scoped again at delivery time."""

    @extend_schema(
        responses=_responses(
            200, _page_response(NOTIFICATION_ITEM, "Page<NotificationDTO>."), _READ_REFUSALS
        )
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        seen = set(
            NotificationAcknowledgement.objects.filter(recipient_id=access.human_id).values_list(
                "notification_id", flat=True
            )
        )
        rows = [r for r in _my_notifications(access) if r.pk not in seen]
        window, cursor = paginate(rows, params)
        return Response(page([notification_dto(r, False) for r in window], cursor))


class GoodsAlertHistoryView(GoodsAPIView):
    """E180: every notification I received, seen or not."""

    @extend_schema(
        responses=_responses(
            200, _page_response(NOTIFICATION_ITEM, "Page<NotificationDTO>."), _READ_REFUSALS
        )
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        seen = set(
            NotificationAcknowledgement.objects.filter(recipient_id=access.human_id).values_list(
                "notification_id", flat=True
            )
        )
        rows = _my_notifications(access)
        window, cursor = paginate(rows, params)
        return Response(page([notification_dto(r, r.pk in seen) for r in window], cursor))


class GoodsAlertSeenView(GoodsAPIView):
    """E187: acknowledge that I saw notifications; the business cause stays open."""

    @extend_schema(responses=_responses(200, SEEN_RESPONSE, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, {"notification_ids"}, required=["notification_ids"])
        ids_raw = body["notification_ids"]
        if not isinstance(ids_raw, list) or len(ids_raw) > 1000:
            raise Refusal("INVALID_REQUEST", "notification_ids must be a list of up to 1000 IDs.")
        ids = [parse_uuid(value, "notification_ids") for value in ids_raw]
        mine = {n.pk for n in _my_notifications(access)}
        if any(i not in mine for i in ids):
            raise Refusal("NOT_FOUND", "A notification was not found.")

        def handler(run: CommandRun) -> CommandResult:
            already = set(
                NotificationAcknowledgement.objects.filter(
                    recipient_id=access.human_id, notification_id__in=ids
                ).values_list("notification_id", flat=True)
            )
            for notification_id in ids:
                if notification_id in already:
                    continue
                run.record(
                    NotificationAcknowledgement(
                        notification_id=notification_id,
                        recipient_id=access.human_id,
                        seen_at=run.now,
                    )
                )
            return CommandResult(resource_type="notification_ack")

        self.run_command(
            request,
            access=access,
            action="alerts.seen",
            meta=meta,
            business_input={"notification_ids": sorted(str(i) for i in ids)},
            handler=handler,
        )
        return Response({"seen": [str(i) for i in ids]})


def _exception_sites(access: Any) -> set[int] | None:
    """Sites whose exceptions I may read. An exception belongs to its store, so any grant
    at the store reaches it (``AccessContext.store_site_ids``)."""
    union: set[int] = set()
    for action in EXCEPTION_READERS:
        sites = access.store_site_ids(action)
        if sites is None:
            return None
        union |= sites
    return union


class ExceptionListView(GoodsAPIView):
    """E185: owned goods exceptions in scope, with age, due date and resolution routes."""

    @extend_schema(
        responses=_responses(
            200, _page_response(EXCEPTION_DATA, "Page<ExceptionDTO>."), _READ_REFUSALS
        )
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        if not any(access.holds(action) for action in EXCEPTION_READERS):
            raise Refusal("ACTION_DENIED", "You do not have permission to read exceptions.")
        sites = _exception_sites(access)
        # Soonest deadline first; an exception with no deadline at all (GSA-T08:
        # a working-day SLA in a business with no approved calendar) sorts last
        # rather than ahead of everything, which is what the database's own
        # ascending default would do on some backends.
        queryset = GoodsException.objects.filter(tenant_id=access.tenant_id).order_by(
            F("due_at").asc(nulls_last=True), "id"
        )
        if sites is not None:
            queryset = queryset.filter(site_id__in=sorted(sites))
        if params.get("site_id"):
            site_id = parse_int_id(params["site_id"], "site_id")
            if sites is not None and site_id not in sites:
                raise Refusal("NOT_FOUND", "That site was not found.")
            queryset = queryset.filter(site_id=site_id)
        rows = list(queryset)
        window, cursor = paginate(rows, params)
        now = timezone.now()
        return Response(page([exception_dto(r, now) for r in window], cursor))


class ExceptionEventView(GoodsAPIView):
    """E186: assign an exception or add a note; resolution belongs to the fixing command.

    Also its own read (small addition, ticket 08): the same event log a note or
    assignment appends to, newest first, for the notification centre's drawer
    (design §8.2). Scoped exactly like `ExceptionEventView.post` and `E185` —
    the same `exception.view`/`exception.manage` grant at the exception's site.
    """

    @extend_schema(
        responses=_responses(
            200,
            _page_response(EXCEPTION_EVENT_ITEM, "Page<ExceptionEventDTO>, newest first."),
            _READ_REFUSALS,
        )
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request)
        exception = GoodsException.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
        if exception is None or not (
            access.can_at_store("exception.view", exception.site_id)
            or access.can_at_store("exception.manage", exception.site_id)
        ):
            raise Refusal("NOT_FOUND", "That exception was not found.")
        rows = list(ExceptionEvent.objects.filter(exception_id=pk).order_by("-recorded_at", "-id"))
        window, cursor = paginate(rows, params)
        return Response(page([exception_event_dto(r) for r in window], cursor))

    @extend_schema(responses=_responses(200, EXCEPTION_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"event_kind", "owner_human_id", "note", "reason_code"},
            required=["event_kind"],
        )
        exception = GoodsException.objects.filter(tenant_id=access.tenant_id, pk=pk).first()
        if exception is None or not (
            access.can_at_store("exception.view", exception.site_id)
            or access.can_at_store("exception.manage", exception.site_id)
        ):
            raise Refusal("NOT_FOUND", "That exception was not found.")
        access.require_at_store("exception.manage", exception.site_id)
        owner = (
            parse_uuid(body["owner_human_id"], "owner_human_id")
            if body.get("owner_human_id")
            else None
        )

        def handler(run: CommandRun) -> CommandResult:
            current = (
                GoodsException.objects.filter(pk=pk).values_list("revision", flat=True).first()
            )
            if meta.expected_revision != current:
                raise Refusal("REVISION_SUPERSEDED", "The exception changed; reload it.")
            updated = add_exception_event(
                run,
                pk,
                event_kind=str(body["event_kind"]),
                owner_human_id=owner,
                note=body.get("note"),
            )
            run.audit_site_id = updated.site_id
            return CommandResult(resource_type="exception", resource_id=str(updated.pk))

        result = self.run_command(
            request,
            access=access,
            action="exceptions.event",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=exception.subject_key,
            site_id=exception.site_id,
        )
        refreshed = GoodsException.objects.get(pk=pk)
        return Response(
            resource_dto(
                id=refreshed.pk,
                data=exception_dto(refreshed, timezone.now()),
                revision=refreshed.revision,
                state=refreshed.state,
                context={"site_id": refreshed.site_id},
            ),
            status=result.status_code,
        )


class ConfirmOriginUnavailableView(GoodsAPIView):
    """E241 (GSA-T10): the dedicated owner action that closes a synthetic opening
    row's "historical origin unavailable" investigation. It never edits the row's
    origin/season evidence or approves real opening; a generic E186 note/assign
    cannot substitute for it (``ExceptionEventView`` refuses that combination
    with ``EXCEPTION_RESOLUTION_ROUTE``)."""

    @extend_schema(responses=_responses(200, EXCEPTION_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        from ptmapper import goods_manifest_services as manifest_services

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"manifest_row_id", "reason_code", "evidence_ids", "reviewed_hash"},
            required=["manifest_row_id", "reason_code", "reviewed_hash"],
        )
        exception = GoodsException.objects.filter(
            tenant_id=access.tenant_id, pk=pk, kind="opening_origin_unavailable"
        ).first()
        if exception is None or not access.can_at_store("exception.manage", exception.site_id):
            raise Refusal("NOT_FOUND", "That exception was not found.")
        access.require_at_store(manifest_services.MANIFEST_ACTION, exception.site_id)
        manifest_row_id = parse_uuid(body["manifest_row_id"], "manifest_row_id")
        evidence_ids = body.get("evidence_ids") or []
        if not isinstance(evidence_ids, list) or len(evidence_ids) > 20:
            raise Refusal("INVALID_REQUEST", "evidence_ids must be a list of at most 20 IDs.")

        def handler(run: CommandRun) -> CommandResult:
            current = (
                GoodsException.objects.filter(pk=pk).values_list("revision", flat=True).first()
            )
            if meta.expected_revision != current:
                raise Refusal("REVISION_SUPERSEDED", "The exception changed; reload it.")
            manifest_services.confirm_origin_unavailable(
                run,
                exception_id=pk,
                manifest_row_id=manifest_row_id,
                reason_code=str(body["reason_code"]),
                evidence_ids=[parse_uuid(e, "evidence_ids") for e in evidence_ids],
                reviewed_hash=str(body["reviewed_hash"]),
            )
            return CommandResult(resource_type="exception", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="exceptions.confirm_origin_unavailable",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=exception.subject_key,
            site_id=exception.site_id,
        )
        refreshed = GoodsException.objects.get(pk=pk)
        return Response(
            resource_dto(
                id=refreshed.pk,
                data=exception_dto(refreshed, timezone.now()),
                revision=refreshed.revision,
                state=refreshed.state,
                context={"site_id": refreshed.site_id},
            ),
            status=result.status_code,
        )


class _AcceptEventStreamNegotiation(DefaultContentNegotiation):
    """The browser `EventSource` that reads this route sends `Accept:
    text/event-stream` — no registered DRF renderer matches that, so
    `APIView.initial()`'s ordinary negotiation refuses the request with 406
    before `get()` ever runs (small fix, ticket 08: the route existed but no
    real `EventSource` could reach it). This view's response is a
    hand-written `StreamingHttpResponse`, never a DRF `Response`, so which
    renderer content negotiation would have picked is moot — accept whatever
    the client asked for."""

    def select_renderer(
        self, request: Request, renderers: Iterable[Any], format_suffix: str | None = None
    ) -> tuple[Any, str]:
        renderer = next(iter(renderers))
        return renderer, renderer.media_type


class EventStreamView(GoodsAPIView):
    """E188: server-sent change identifiers in scope; clients refetch what changed."""

    content_negotiation_class = _AcceptEventStreamNegotiation

    @extend_schema(
        responses={
            (200, "text/event-stream"): {
                "type": "string",
                "description": (
                    "A server-sent event stream of change identifiers in scope (E188). "
                    "Each event names a document that moved; the client refetches it."
                ),
            },
            400: REFUSAL_RESPONSE,
            401: REFUSAL_RESPONSE,
            403: REFUSAL_RESPONSE,
        }
    )
    def get(self, request: Request) -> StreamingHttpResponse:
        access = self.access(request)
        params = check_query(request, {"cursor"})
        start = decode_cursor(params.get("cursor"))

        def changes(after: int) -> tuple[list[dict[str, Any]], int]:
            from core.kernel_models import DocumentEvent

            reach = access.site_reach(STREAM_READER)
            events = DocumentEvent.objects.filter(tenant_id=access.tenant_id)
            if reach is not None:
                events = events.filter(document__site_id__in=sorted(reach))
            rows = list(
                events.order_by("recorded_at", "id").values(
                    "id", "document_id", "event_kind", "document__site_id"
                )
            )
            cells = document_cells(rows)
            items = [
                {"id": str(r["id"]), "subject_id": str(r["document_id"]), "kind": r["event_kind"]}
                for r in rows
                if access.covers_all({STREAM_READER}, cells[r["document_id"]])
            ] + [
                {"id": str(n.pk), "subject_id": n.subject_key, "kind": "notification"}
                for n in _my_notifications(access)
            ]
            return items[after:], len(items)

        def stream() -> Iterator[bytes]:
            cursor = start
            deadline = time.monotonic() + STREAM_SECONDS
            while True:
                # Delivery is authorised again at every poll: a revoked or expired
                # session, or a narrowed grant, stops or narrows what is still sent.
                # The response streams after the request's tenant binding has ended, so
                # each poll binds the trusted tenant again for row-level security.
                with tenant_context(access.tenant_id):
                    if not access.refresh():
                        break
                    fresh, total = changes(cursor)
                for offset, item in enumerate(fresh, start=cursor + 1):
                    yield f"id: {offset}\nevent: change\ndata: {json.dumps(item)}\n\n".encode()
                cursor = total
                if time.monotonic() >= deadline:
                    break
                yield b": keep-alive\n\n"
                time.sleep(STREAM_POLL)

        response = StreamingHttpResponse(stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
