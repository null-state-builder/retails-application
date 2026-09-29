"""Goods-v1 vendor and booking endpoints (E021-E025, E092/E093, E108-E112, E238)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.actions import TENANT_MASTER_READ_ACTIONS
from accounts.goods_api import (
    PAGE_PARAMETERS,
    GoodsAPIView,
    business_body,
    check_query,
    page,
    paginate,
    parse_meta,
    resource_dto,
)
from accounts.principal import AccessContext
from core.commands import CommandResult, CommandRun
from core.refusals import Refusal
from inbound import goods_input as inp
from vendors.goods_models import BookingReceiptLink, GoodsBooking
from vendors.goods_services import (
    BOOKING_ACTION,
    BOOKING_READ_ACTIONS,
    HEADER_KEYS,
    VENDOR_ACTION,
    VENDOR_KEYS,
    booking_by_document,
    booking_close_kinds,
    booking_content_hash,
    booking_head,
    booking_home_site,
    booking_lines,
    booking_progress,
    booking_readable,
    booking_state,
    close_booking,
    confirm_booking,
    correct_booking,
    correction_home,
    create_booking,
    create_vendor,
    latest_vendor_versions,
    link_receipts,
    parse_booking,
    parse_correction,
    parse_vendor,
    retire_vendor,
    update_booking,
    update_vendor,
    vendor_data,
    vendor_revision,
    vendor_state,
)
from vendors.models import Vendor

# ---------------------------------------------------------------------------
# Documented responses (ticket 05)
#
# These views hand-build dict responses, so drf-spectacular sees nothing without
# the schemas below. Ticket 05 could not describe the vendor and booking list
# and create operations: they shared their paths with the legacy readers through
# the old contract dispatcher, and OpenAPI allows one operation per path and
# method (#303). They answer under ``/api/goods-v1/`` alone now, so every
# operation in this module is described. The envelopes are repeated rather than
# imported so this module documents its own contract, as
# ``masters/goods_views.py`` does for its own.
# ---------------------------------------------------------------------------

REFUSAL_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "error": {"type": "string"},
        "details": {"type": "object", "additionalProperties": True},
        "retryable": {"type": "boolean"},
    },
}


def _dto_response(data_schema: dict[str, Any], description: str) -> dict[str, Any]:
    """The ``ResourceDTO<...>`` envelope of design §6.1 around one ``data`` schema."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "id": {"type": "string"},
            "record_contract": {"type": "string", "enum": ["goods-v1"]},
            "revision": {"type": "integer"},
            "content_hash": {"type": "string"},
            "state": {
                "type": "string",
                "enum": ["draft", "confirmed", "short_closed", "cancelled"],
            },
            "number": {"type": "string", "nullable": True},
            "version": {"type": "integer", "nullable": True},
            "context": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string", "nullable": True},
                    "entity_id": {"type": "string", "nullable": True},
                    "brand_id": {"type": "string", "nullable": True},
                },
            },
            "data": data_schema,
            "allowed_actions": {"type": "array", "items": {"type": "string"}},
        },
    }


BOOKING_PROGRESS_LINE: dict[str, Any] = {
    "type": "object",
    "description": (
        "One booking line's progress. Outstanding is booked minus received plus "
        "reversed, never below zero. Size, colour and MRP are optional on a "
        "booking line and stay null when they were not given."
    ),
    "properties": {
        "line_key": {"type": "string"},
        "ordered_qty": {"type": "integer"},
        "received_qty": {"type": "integer"},
        "reversed_qty": {"type": "integer"},
        "outstanding_qty": {"type": "integer"},
        "style_code": {"type": "string", "nullable": True},
        "description": {"type": "string", "nullable": True},
        "size_value_id": {"type": "string", "nullable": True},
        "size_label": {"type": "string", "nullable": True},
        "colour_value_id": {"type": "string", "nullable": True},
        "colour_label": {"type": "string", "nullable": True},
        "mrp_paise": {
            "type": "string",
            "nullable": True,
            "description": "Integer paise as a base-10 string; null is unknown, never zero.",
        },
        "cost_paise": {
            "type": "string",
            "nullable": True,
            "description": (
                "Indicative cost per piece in integer paise; the PT holds the binding "
                "cost. Omitted without the `cost` field grant."
            ),
        },
        "destination_site_id": {"type": "string", "nullable": True},
    },
}

#: Display names for the ids a booking header carries, so a reader of the
#: booking is not sent to master lists they may not be allowed to read.
BOOKING_NAMES: dict[str, Any] = {
    "type": "object",
    "description": "Names of the booking's vendor, brand, season and site.",
    "properties": {
        "vendor": {"type": "string", "nullable": True},
        "brand": {"type": "string", "nullable": True},
        "season": {"type": "string", "nullable": True},
        "site": {"type": "string", "nullable": True},
    },
}

BOOKING_PROGRESS_DATA: dict[str, Any] = {
    "type": "object",
    "description": "BookingProgressDTO (design §6.1), after every dated correction.",
    "properties": {
        "booking_header": {"type": "object", "additionalProperties": True},
        "names": BOOKING_NAMES,
        "lines": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": BOOKING_PROGRESS_LINE},
                "next_cursor": {"type": "string", "nullable": True},
                "total": {"type": "integer"},
            },
        },
    },
}

BOOKING_DOCUMENT_DATA: dict[str, Any] = {
    "type": "object",
    "description": "BookingPayload (design §5.3): the header fields plus its current lines.",
    "additionalProperties": True,
}

BOOKING_PROGRESS_RESOURCE = _dto_response(BOOKING_PROGRESS_DATA, "ResourceDTO<BookingProgressDTO>.")
BOOKING_DOCUMENT_RESOURCE = _dto_response(BOOKING_DOCUMENT_DATA, "ResourceDTO<BookingPayload>.")

VENDOR_DATA: dict[str, Any] = {
    "type": "object",
    "description": "MasterPayload.vendor (E021-E025).",
    "properties": {
        "code": {"type": "string"},
        "name": {"type": "string"},
        "gstin": {"type": "string", "nullable": True},
        "agent_ref": {"type": "string", "nullable": True},
    },
}

VENDOR_RESOURCE = _dto_response(VENDOR_DATA, "ResourceDTO<MasterPayload.vendor>.")

#: One row of E092's booking worklist: the document's identity and where it has
#: got to, never its lines or its money.
BOOKING_LIST_ITEM: dict[str, Any] = {
    "type": "object",
    "description": "BookingListItemDTO (E092).",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "record_contract": {"type": "string", "enum": ["goods-v1"]},
        "kind": {"type": "string", "enum": ["booking"]},
        "purpose": {"type": "string", "enum": ["booking"]},
        "number": {"type": "string", "nullable": True},
        "state": {"type": "string"},
        "site_id": {
            "type": "string",
            "nullable": True,
            "description": "Null until the buyer names a destination (GSA-T05).",
        },
        "brand_id": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "owner_role": {"type": "string"},
        "due_at": {"type": "string", "nullable": True},
        "vendor_id": {"type": "string"},
        "season_id": {"type": "string"},
        "names": BOOKING_NAMES,
        "booked_qty": {"type": "integer", "description": "Pieces on the current lines."},
        "arrived_qty": {
            "type": "integer",
            "description": "Pieces linked as received, less any a counter-GRN reversed.",
        },
        "mrp_value_paise": {
            "type": "string",
            "nullable": True,
            "description": "Quantity times MRP over the lines that carry one; null if none does.",
        },
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


VENDOR_PAGE = _page_response(VENDOR_RESOURCE, "Page<ResourceDTO<MasterPayload.vendor>>.")
BOOKING_PAGE = _page_response(BOOKING_LIST_ITEM, "Page<BookingListItemDTO>.")

_READ_REFUSALS = (400, 401, 403, 404)
_WRITE_REFUSALS = (400, 401, 403, 404, 409, 422, 503)


def _responses(status: int, schema: dict[str, Any], codes: tuple[int, ...]) -> dict[int, Any]:
    return {status: schema, **{code: REFUSAL_RESPONSE for code in codes}}


DETAIL_QUERY_KEYS = frozenset({"version", "line_cursor", "history_cursor"})


# MutationMeta is required by parse_meta for every goods-v1 write. These
# request bodies mirror the closed key sets in goods_services and business_body.
_TEXT = {"type": "string"}
_UUID = {"type": "string", "format": "uuid"}
_ID = {"oneOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": "^[0-9]+$"}]}
_PAISE = {"type": "string", "pattern": "^[0-9]+$", "nullable": True}


def _mutation_request(
    fields: dict[str, Any], *, required: tuple[str, ...] = (), revision_bound: bool = True
) -> dict[str, Any]:
    meta = {
        "command_id": _UUID,
        "contract_version": {"type": "string", "enum": ["goods-v1"]},
        "expected_revision": {"type": "integer", "minimum": 1},
    }
    required_fields = ["command_id", "contract_version", *required]
    if revision_bound:
        required_fields.append("expected_revision")
    return {
        "type": "object",
        "properties": {**meta, **fields},
        "required": required_fields,
        "additionalProperties": False,
    }


_VENDOR_FIELDS = {"code": _TEXT, "name": _TEXT, "gstin": _TEXT, "agent_ref": _TEXT}
VENDOR_CREATE_REQUEST = _mutation_request(
    _VENDOR_FIELDS, required=("code", "name"), revision_bound=False
)
VENDOR_UPDATE_REQUEST = _mutation_request(_VENDOR_FIELDS)
VENDOR_RETIRE_REQUEST = _mutation_request(
    {"reason_code": _TEXT, "effective_at": {"type": "string", "format": "date-time"}},
    required=("reason_code", "effective_at"),
)
_BOOKING_LINE = {
    "type": "object",
    "required": ["line_key", "style_code", "qty"],
    "properties": {
        "line_key": _UUID,
        "style_code": _TEXT,
        "description": _TEXT,
        "size": _TEXT,
        "size_value_id": _UUID,
        "colour_value_id": _UUID,
        "qty": {"type": "integer", "minimum": 1},
        "destination_site_id": _ID,
        "mrp_paise": _PAISE,
        "cost_paise": _PAISE,
    },
    "additionalProperties": False,
}
_BOOKING_FIELDS = {
    "vendor_id": _ID,
    "brand_id": _ID,
    "season_id": _ID,
    "entity_id": _ID,
    "destination_site_id": _ID,
    "commercial_label": _TEXT,
    "vendor_ref": _TEXT,
    "agreement_evidence_id": _UUID,
    "expected_date": {"type": "string", "format": "date"},
    "source_evidence_id": _UUID,
    "lines": {"type": "array", "items": _BOOKING_LINE},
    "notes": _TEXT,
}
_BOOKING_REQUIRED = ("vendor_id", "brand_id", "season_id", "entity_id", "lines")
BOOKING_CREATE_REQUEST = _mutation_request(
    _BOOKING_FIELDS, required=_BOOKING_REQUIRED, revision_bound=False
)
BOOKING_UPDATE_REQUEST = _mutation_request(_BOOKING_FIELDS, required=_BOOKING_REQUIRED)
BOOKING_CONFIRM_REQUEST = _mutation_request({"reviewed_hash": _TEXT}, required=("reviewed_hash",))
BOOKING_CLOSE_REQUEST = _mutation_request(
    {"action": {"type": "string", "enum": ["short_close", "cancel"]},
     "reason_code": _TEXT, "note": _TEXT},
    required=("action", "reason_code"),
)
_RECEIPT_LINK = {
    "type": "object",
    "required": ["booking_line_key", "grn_line_key", "qty"],
    "properties": {
        "booking_line_key": _UUID,
        "grn_line_key": _UUID,
        "qty": {"type": "integer", "minimum": 1},
        "counter_of_id": _UUID,
    },
    "additionalProperties": False,
}
BOOKING_RECEIPT_REQUEST = _mutation_request(
    {"grn_id": _UUID, "links": {"type": "array", "items": _RECEIPT_LINK, "minItems": 1},
     "reason_code": _TEXT, "effective_at": {"type": "string", "format": "date-time"}},
    required=("grn_id", "links", "reason_code", "effective_at"),
)
_HEADER_CHANGES = {
    "type": "object",
    "properties": {"expected_date": {"type": "string", "format": "date"},
                   "destination_site_id": _ID, "notes": _TEXT},
    "additionalProperties": False,
}
_LINE_CHANGE = {
    "type": "object",
    "required": ["original_line_key", "replacement_line_key"],
    "properties": {
        "original_line_key": _UUID,
        "replacement_line_key": _UUID,
        "qty": {"type": "integer", "minimum": 1},
        "destination_site_id": _ID,
        "size_value_id": _UUID,
        "colour_value_id": _UUID,
        "description": _TEXT,
        "cost_paise": _PAISE,
    },
    "additionalProperties": False,
}
BOOKING_CORRECTION_REQUEST = _mutation_request(
    {"reason_code": _TEXT, "effective_at": {"type": "string", "format": "date-time"},
     "evidence_ids": {"type": "array", "items": _UUID}, "header_changes": _HEADER_CHANGES,
     "line_changes": {"type": "array", "items": _LINE_CHANGE}},
    required=("reason_code", "effective_at"),
)


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


def _vendor_resource(vendor: Vendor) -> dict[str, Any]:
    latest = latest_vendor_versions([vendor.pk]).get(vendor.pk)
    data = vendor_data(vendor, latest)
    revision = vendor_revision(latest)
    return resource_dto(
        id=vendor.pk,
        data=data,
        revision=revision,
        state=vendor_state(vendor, latest),
        context={},
        content={"data": data, "revision": revision},
    )


def _require_vendor_read(access: AccessContext) -> None:
    """E021/E023: an authenticated read grant for vendors, not merely any grant row."""
    if not access.all_actions() & TENANT_MASTER_READ_ACTIONS:
        raise Refusal("ACTION_DENIED", "You do not have permission to read vendors.")


def _vendor_or_404(pk: int) -> Vendor:
    vendor = Vendor.objects.filter(pk=pk).first()
    if vendor is None:
        raise Refusal("NOT_FOUND", "That vendor was not found.")
    return vendor


class GoodsVendorListCreateView(GoodsAPIView):
    """E021 list and E022 create."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "q",
                str,
                required=False,
                description="Case-insensitive substring match against code or name.",
            ),
            OpenApiParameter(
                "brand_id",
                int,
                required=False,
                description="Only vendors carrying this brand.",
            ),
            *PAGE_PARAMETERS,
        ],
        responses=_responses(200, VENDOR_PAGE, _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request)
        _require_vendor_read(access)
        queryset = Vendor.objects.all().order_by("name", "id")
        if params.get("q"):
            term = params["q"][:100]
            queryset = queryset.filter(code__icontains=term) | queryset.filter(name__icontains=term)
        if params.get("brand_id"):
            queryset = queryset.filter(brands__id=inp.legacy_id(params["brand_id"], "brand_id"))
        window, cursor = paginate(list(queryset.distinct()), params)
        return Response(page([_vendor_resource(v) for v in window], cursor))

    @extend_schema(request={"application/json": VENDOR_CREATE_REQUEST}, responses=_responses(201, VENDOR_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(request.data, VENDOR_KEYS, required=["code", "name"])
        payload = parse_vendor(body, partial=False)
        access.require(VENDOR_ACTION)

        def handler(run: CommandRun) -> CommandResult:
            vendor = create_vendor(run, payload)
            return CommandResult(
                resource_type="vendor", resource_id=str(vendor.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="vendors.create",
            meta=meta,
            business_input=body,
            handler=handler,
            subject_key=f"vendor:{payload['code']}",
        )
        vendor = _vendor_or_404(int(str(result.resource_id)))
        return Response(_vendor_resource(vendor), status=result.status_code)


class GoodsVendorDetailView(GoodsAPIView):
    """E023 read and E024 correct."""

    @extend_schema(
        operation_id="goods_v1_vendors_detail",
        responses=_responses(200, VENDOR_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        check_query(request, DETAIL_QUERY_KEYS)
        _require_vendor_read(access)
        return Response(_vendor_resource(_vendor_or_404(pk)))

    @extend_schema(request={"application/json": VENDOR_UPDATE_REQUEST}, responses=_responses(200, VENDOR_RESOURCE, _WRITE_REFUSALS))
    def patch(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, VENDOR_KEYS)
        payload = parse_vendor(body, partial=True)
        access.require(VENDOR_ACTION)
        _vendor_or_404(pk)

        def handler(run: CommandRun) -> CommandResult:
            vendor = update_vendor(run, pk, payload, expected_revision=meta.expected_revision)
            return CommandResult(resource_type="vendor", resource_id=str(vendor.pk))

        result = self.run_command(
            request,
            access=access,
            action="vendors.update",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"vendor:{pk}",
        )
        return Response(_vendor_resource(_vendor_or_404(pk)), status=result.status_code)


class GoodsVendorRetireView(GoodsAPIView):
    """E025: retire with a reason, from an effective time; history keeps the vendor."""

    @extend_schema(request={"application/json": VENDOR_RETIRE_REQUEST}, responses=_responses(200, VENDOR_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: int) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, {"reason_code", "effective_at"}, required=["reason_code", "effective_at"]
        )
        reason_code = inp.text(body["reason_code"], "reason_code", 60, required=True)
        effective_at = inp.timestamp(body["effective_at"], "effective_at")
        access.require("master.retire")
        _vendor_or_404(pk)
        access.require_step_up()

        def handler(run: CommandRun) -> CommandResult:
            access.require_step_up(run.now)
            vendor = retire_vendor(
                run,
                pk,
                reason_code=str(reason_code),
                effective_at=effective_at,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="vendor", resource_id=str(vendor.pk))

        result = self.run_command(
            request,
            access=access,
            action="vendors.retire",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            subject_key=f"vendor:{pk}",
        )
        return Response(_vendor_resource(_vendor_or_404(pk)), status=result.status_code)


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------


def _readable_booking(access: AccessContext, pk: uuid.UUID) -> GoodsBooking:
    if not any(action in access.all_actions() for action in BOOKING_READ_ACTIONS):
        raise Refusal("ACTION_DENIED", "You do not have permission to read bookings.")
    booking = booking_by_document(pk)
    if booking is None or not booking_readable(access, booking):
        raise Refusal("NOT_FOUND", "That booking was not found.")
    return booking


def _writable_booking(access: AccessContext, pk: uuid.UUID) -> GoodsBooking:
    booking = booking_by_document(pk)
    if booking is None:
        if BOOKING_ACTION not in access.all_actions():
            raise Refusal("ACTION_DENIED", "You do not have permission to manage bookings.")
        raise Refusal("NOT_FOUND", "That booking was not found.")
    access.require(
        BOOKING_ACTION,
        site_id=booking.document.site_id,
        brand_id=booking.brand_id,
        entity_id=booking.document.entity_id,
    )
    return booking


def _shows_cost(access: AccessContext, booking: GoodsBooking) -> bool:
    """A booking's cost is read only through a reading grant that carries ``cost``."""
    return "cost" in access.field_grants(
        site_id=booking.document.site_id,
        brand_id=booking.brand_id,
        entity_id=booking.document.entity_id,
        actions=BOOKING_READ_ACTIONS,
    )


def _without_cost(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cost is omitted, never null-filled, without the field grant (as a PT line's is)."""
    return [{k: v for k, v in line.items() if k != "cost_paise"} for line in lines]


def _names(
    bookings: list[GoodsBooking], headers: dict[uuid.UUID, dict[str, Any]]
) -> Callable[[GoodsBooking], dict[str, str | None]]:
    """Each booking's vendor, brand, season and site names, read in four queries."""
    from masters.models import Brand, Season, Store

    def ids(key: str) -> set[int]:
        return {int(h[key]) for h in headers.values() if h.get(key)}

    vendors = dict(Vendor.objects.filter(pk__in=ids("vendor_id")).values_list("pk", "name"))
    brands = dict(
        Brand.objects.filter(pk__in={b.brand_id for b in bookings}).values_list("pk", "name")
    )
    seasons = dict(Season.objects.filter(pk__in=ids("season_id")).values_list("pk", "name"))
    site_ids = {b.document.site_id for b in bookings if b.document.site_id is not None}
    site_ids |= ids("destination_site_id")
    sites = dict(Store.objects.filter(pk__in=site_ids).values_list("pk", "name"))

    def of(booking: GoodsBooking) -> dict[str, str | None]:
        header = headers.get(booking.document_id, {})
        site = header.get("destination_site_id") or booking.document.site_id
        return {
            "vendor": vendors.get(int(header["vendor_id"])) if header.get("vendor_id") else None,
            "brand": brands.get(booking.brand_id),
            "season": seasons.get(int(header["season_id"])) if header.get("season_id") else None,
            "site": sites.get(int(site)) if site else None,
        }

    return of


def _booking_resource(
    access: AccessContext,
    booking: GoodsBooking,
    *,
    progress: bool,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    head = booking_head(booking)
    state = booking_state(booking, head)
    shows_cost = _shows_cost(access, booking)
    if progress:
        header, items = booking_progress(booking, head)
        if not shows_cost:
            items = _without_cost(items)
        window, cursor = paginate(
            items,
            {"cursor": (params or {}).get("line_cursor", ""), "limit": "500"},
            default=500,
            maximum=500,
        )
        data: dict[str, Any] = {
            "booking_header": header,
            "names": _names([booking], {booking.document_id: header})(booking),
            "lines": {"items": window, "next_cursor": cursor, "total": len(items)},
        }
    else:
        header, lines, _root = booking_lines(booking, head)
        data = {**header, "lines": lines if shows_cost else _without_cost(lines)}
    allowed: list[str] = []
    if access.can(
        BOOKING_ACTION,
        site_id=booking.document.site_id,
        brand_id=booking.brand_id,
        entity_id=booking.document.entity_id,
    ):
        allowed = (
            ["bookings.update", "bookings.request_approval"]
            if state == "draft"
            else (
                ["bookings.close", "bookings.receipt_links", "bookings.corrections"]
                if state == "confirmed"
                else ["bookings.receipt_links"]
                if state == "short_closed"
                else []
            )
        )
        if booking.document.site_id is None:
            # GSA-T05: no receiving against a booking until it names a destination.
            allowed = [action for action in allowed if action != "bookings.receipt_links"]
    body = resource_dto(
        id=booking.document_id,
        data=data,
        revision=head.revision,
        state=state,
        context={
            "site_id": booking.document.site_id,
            "entity_id": booking.document.entity_id,
            "brand_id": booking.brand_id,
        },
        number=booking.document.official_number,
        version=head.live_version.version if head.live_version is not None else None,
        allowed_actions=allowed,
    )
    body["content_hash"] = booking_content_hash(booking, head)
    return body


def _reload(pk: uuid.UUID) -> GoodsBooking:
    booking = booking_by_document(pk)
    if booking is None:
        raise Refusal("NOT_FOUND", "That booking was not found.")
    return booking


#: The states `booking_state()` answers with. A `state` filter must name one
#: of these rather than being silently ignored (ticket 02D).
BOOKING_STATES = frozenset({"draft", "confirmed", "short_closed", "cancelled"})


class GoodsBookingListCreateView(GoodsAPIView):
    """E092 list and E108 create (unnumbered draft)."""

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "state",
                str,
                required=False,
                enum=sorted(BOOKING_STATES),
                description="Only bookings in this state — an arrival picker wants confirmed only.",
            ),
            OpenApiParameter(
                "q",
                str,
                required=False,
                description="Case-insensitive substring match against the official number.",
            ),
            OpenApiParameter(
                "site_id",
                int,
                required=False,
                description=(
                    "Only bookings held at this site; a site the caller cannot reach is "
                    "NOT_FOUND. A booking with no destination yet is at no site."
                ),
            ),
            OpenApiParameter(
                "brand_id", int, required=False, description="Only bookings for this brand."
            ),
            *PAGE_PARAMETERS,
        ],
        responses=_responses(200, BOOKING_PAGE, _READ_REFUSALS),
    )
    def get(self, request: Request) -> Response:
        access = self.access(request)
        params = check_query(request, {"q", "cursor", "limit", "site_id", "brand_id", "state"})
        if not any(action in access.all_actions() for action in BOOKING_READ_ACTIONS):
            raise Refusal("ACTION_DENIED", "You do not have permission to read bookings.")
        site_filter = inp.optional_legacy_id(params.get("site_id") or None, "site_id")
        brand_filter = inp.optional_legacy_id(params.get("brand_id") or None, "brand_id")
        state_filter = params.get("state") or None
        if state_filter is not None and state_filter not in BOOKING_STATES:
            raise Refusal("INVALID_REQUEST", f"state must be one of {sorted(BOOKING_STATES)}.")
        if site_filter is not None and not any(
            access.can_reach_site(a, site_filter) for a in BOOKING_READ_ACTIONS
        ):
            raise Refusal("NOT_FOUND", "That site was not found.")
        query = (params.get("q") or "").strip().casefold()
        # Cheap checks first: scope, site, brand and number. A state is read off the
        # head already loaded by `select_related`; only a closed booking needs its
        # close event, and those are read in one query, not one per row (ticket 02D).
        rows = [
            booking
            for booking in GoodsBooking.objects.select_related(
                "document", "document__head"
            ).order_by("-created_at", "id")
            if booking_readable(access, booking)
            and (site_filter is None or booking.document.site_id == site_filter)
            and (brand_filter is None or booking.brand_id == brand_filter)
            and (not query or query in (booking.document.official_number or "").casefold())
        ]
        if state_filter is not None:
            closed = (
                booking_close_kinds(rows) if state_filter in ("short_closed", "cancelled") else {}
            )
            rows = [
                booking
                for booking in rows
                if (
                    "draft"
                    if booking.document.head.live_version_id is None
                    else closed.get(booking.document_id, "closed")
                    if booking.closed_at is not None
                    else "confirmed"
                )
                == state_filter
            ]
        window, cursor = paginate(rows, params)
        # How far each booking has got, for the list's progress bar: every
        # receipt link of the window in one query, not one a row.
        arrived: dict[uuid.UUID, int] = {}
        for booking_pk, qty, counter in BookingReceiptLink.objects.filter(
            booking_id__in=[b.pk for b in window]
        ).values_list("booking_id", "linked_qty", "counter_of_id"):
            arrived[booking_pk] = arrived.get(booking_pk, 0) + (-qty if counter else qty)
        read: dict[uuid.UUID, tuple[Any, dict[str, Any], list[dict[str, Any]], Any]] = {}
        for booking in window:
            head = booking_head(booking)
            read[booking.document_id] = (head, *booking_lines(booking, head))
        names = _names(window, {key: value[1] for key, value in read.items()})
        items = []
        for booking in window:
            head, header, lines, _root = read[booking.document_id]
            priced = [line for line in lines if line.get("mrp_paise") is not None]
            items.append(
                {
                    "id": str(booking.document_id),
                    "record_contract": "goods-v1",
                    "kind": "booking",
                    "purpose": "booking",
                    "number": booking.document.official_number,
                    "state": booking_state(booking, head),
                    "site_id": (
                        str(booking.document.site_id)
                        if booking.document.site_id is not None
                        else None
                    ),
                    "brand_id": str(booking.brand_id),
                    "created_at": booking.created_at.isoformat(),
                    "updated_at": head.updated_at.isoformat(),
                    "owner_role": "C-BUY",
                    "due_at": header.get("expected_date"),
                    "vendor_id": str(header.get("vendor_id") or ""),
                    "season_id": str(header.get("season_id") or ""),
                    "names": names(booking),
                    "booked_qty": sum(int(line["qty"]) for line in lines),
                    "arrived_qty": arrived.get(booking.pk, 0),
                    "mrp_value_paise": (
                        str(sum(int(line["qty"]) * int(line["mrp_paise"]) for line in priced))
                        if priced
                        else None
                    ),
                }
            )
        return Response(page(items, cursor))

    @extend_schema(request={"application/json": BOOKING_CREATE_REQUEST}, responses=_responses(201, BOOKING_DOCUMENT_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=False)
        body = business_body(
            request.data,
            HEADER_KEYS,
            required=["vendor_id", "brand_id", "season_id", "entity_id", "lines"],
        )
        data = parse_booking(body)
        home = booking_home_site(data.header, [line for _, line in data.lines])
        access.require(
            BOOKING_ACTION, site_id=home, brand_id=data.brand_id, entity_id=data.entity_id
        )

        def handler(run: CommandRun) -> CommandResult:
            booking = create_booking(run, data)
            return CommandResult(
                resource_type="booking", resource_id=str(booking.document_id), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="bookings.create",
            meta=meta,
            business_input=body,
            handler=handler,
            site_id=home,
            subject_key="booking:new",
        )
        booking = _reload(uuid.UUID(str(result.resource_id)))
        return Response(
            _booking_resource(access, booking, progress=False), status=result.status_code
        )


class GoodsBookingDetailView(GoodsAPIView):
    """E093 progress read and E109 draft edit."""

    @extend_schema(
        operation_id="goods_v1_bookings_detail",
        responses=_responses(200, BOOKING_PROGRESS_RESOURCE, _READ_REFUSALS),
    )
    def get(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        params = check_query(request, DETAIL_QUERY_KEYS)
        booking = _readable_booking(access, pk)
        if params.get("version"):
            wanted = inp.legacy_id(params["version"], "version")
            head = booking_head(booking)
            if head.live_version is None or head.live_version.version != wanted:
                raise Refusal(
                    "VERSION_NOT_FOUND", "That booking version does not exist.", status=404
                )
        inp.text(params.get("line_cursor"), "line_cursor", 200)
        return Response(_booking_resource(access, booking, progress=True, params=params))

    @extend_schema(request={"application/json": BOOKING_UPDATE_REQUEST}, responses=_responses(200, BOOKING_DOCUMENT_RESOURCE, _WRITE_REFUSALS))
    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            HEADER_KEYS,
            required=["vendor_id", "brand_id", "season_id", "entity_id", "lines"],
        )
        data = parse_booking(body)
        booking = _writable_booking(access, pk)
        # A booking with no site yet takes the first destination this edit names.
        home = booking.document.site_id or booking_home_site(
            data.header, [line for _, line in data.lines]
        )
        access.require(
            BOOKING_ACTION,
            site_id=home,
            brand_id=data.brand_id,
            entity_id=booking.document.entity_id,
        )

        def handler(run: CommandRun) -> CommandResult:
            update_booking(run, booking, data, expected_revision=meta.expected_revision)
            return CommandResult(resource_type="booking", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="bookings.update",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=booking.document.site_id,
            subject_key=f"document:{pk}",
        )
        return Response(
            _booking_resource(access, _reload(pk), progress=False), status=result.status_code
        )


class GoodsBookingConfirmView(GoodsAPIView):
    """E110: the C-BUY confirmation - numbered BKG, frozen lines, no stock or GL effect."""

    @extend_schema(request={"application/json": BOOKING_CONFIRM_REQUEST}, responses=_responses(200, BOOKING_DOCUMENT_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(request.data, {"reviewed_hash"}, required=["reviewed_hash"])
        booking = _writable_booking(access, pk)

        def handler(run: CommandRun) -> CommandResult:
            number = confirm_booking(
                run,
                booking,
                reviewed_hash=str(body["reviewed_hash"]),
                expected_revision=meta.expected_revision,
            )
            return CommandResult(
                resource_type="booking", resource_id=str(pk), document_number=number, version=1
            )

        result = self.run_command(
            request,
            access=access,
            action="bookings.confirm",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=booking.document.site_id,
            subject_key=f"document:{pk}",
            reviewed_hash=str(body["reviewed_hash"]),
        )
        return Response(
            _booking_resource(access, _reload(pk), progress=False), status=result.status_code
        )


class GoodsBookingCloseView(GoodsAPIView):
    """E111: short-close or cancel with a reason; original quantities retained."""

    @extend_schema(request={"application/json": BOOKING_CLOSE_REQUEST}, responses=_responses(200, BOOKING_DOCUMENT_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data, {"action", "reason_code", "note"}, required=["action", "reason_code"]
        )
        if body["action"] not in ("short_close", "cancel"):
            raise inp.bad("action must be short_close or cancel.", "action")
        reason_code = str(inp.text(body["reason_code"], "reason_code", 60, required=True))
        note = inp.text(body.get("note"), "note", 500)
        booking = _writable_booking(access, pk)

        def handler(run: CommandRun) -> CommandResult:
            close_booking(
                run,
                booking,
                action=str(body["action"]),
                reason_code=reason_code,
                note=note,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="booking", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="bookings.close",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=booking.document.site_id,
            subject_key=f"document:{pk}",
        )
        return Response(
            _booking_resource(access, _reload(pk), progress=False), status=result.status_code
        )


LINK_KEYS = frozenset({"booking_line_key", "grn_line_key", "qty", "counter_of_id"})


class GoodsBookingReceiptLinksView(GoodsAPIView):
    """E112: dated positive receipt links and exact counter links."""

    @extend_schema(request={"application/json": BOOKING_RECEIPT_REQUEST}, responses=_responses(200, BOOKING_DOCUMENT_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        from inbound.goods_services import goods_grn

        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"grn_id", "links", "reason_code", "effective_at"},
            required=["grn_id", "links", "reason_code", "effective_at"],
        )
        grn_id = inp.uuid_value(body["grn_id"], "grn_id")
        reason_code = str(inp.text(body["reason_code"], "reason_code", 60, required=True))
        effective_at = inp.timestamp(body["effective_at"], "effective_at")
        raw_links = inp.object_list(body["links"], "links", limit=1000)
        if not raw_links:
            raise inp.bad("Send at least one link.", "links", "REQUIRED")
        links = []
        for index, raw in enumerate(raw_links):
            field = f"links[{index}]"
            link = inp.closed(
                raw, LINK_KEYS, field, required=["booking_line_key", "grn_line_key", "qty"]
            )
            links.append(
                {
                    "booking_line_key": inp.uuid_value(
                        link["booking_line_key"], f"{field}.booking_line_key"
                    ),
                    "grn_line_key": inp.uuid_value(link["grn_line_key"], f"{field}.grn_line_key"),
                    "qty": inp.quantity(link["qty"], f"{field}.qty"),
                    "counter_of_id": inp.optional_uuid(
                        link.get("counter_of_id"), f"{field}.counter_of_id"
                    ),
                }
            )
        booking = _writable_booking(access, pk)
        grn = goods_grn(grn_id)
        if grn is None or not access.can(
            BOOKING_ACTION, site_id=grn.document.site_id, brand_id=grn.arrival.brand_id
        ):
            raise Refusal("NOT_FOUND", "That GRN was not found.")

        def handler(run: CommandRun) -> CommandResult:
            link_receipts(
                run,
                booking,
                grn_document_id=grn_id,
                links=links,
                reason_code=reason_code,
                effective_at=effective_at,
                expected_revision=meta.expected_revision,
            )
            return CommandResult(resource_type="booking", resource_id=str(pk))

        result = self.run_command(
            request,
            access=access,
            action="bookings.receipt_links",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk), str(grn_id)],
            site_id=booking.document.site_id,
            subject_key=f"document:{pk}",
        )
        return Response(
            _booking_resource(access, _reload(pk), progress=False), status=result.status_code
        )


class GoodsBookingCorrectionsView(GoodsAPIView):
    """E238: append-only corrections to a confirmed booking."""

    @extend_schema(request={"application/json": BOOKING_CORRECTION_REQUEST}, responses=_responses(201, BOOKING_PROGRESS_RESOURCE, _WRITE_REFUSALS))
    def post(self, request: Request, pk: uuid.UUID) -> Response:
        access = self.access(request)
        meta = parse_meta(request.data, revision_bound=True)
        body = business_body(
            request.data,
            {"reason_code", "effective_at", "evidence_ids", "header_changes", "line_changes"},
            required=["reason_code", "effective_at"],
        )
        payload = parse_correction(body)
        booking = _writable_booking(access, pk)
        if booking.document.site_id is None and correction_home(payload) is not None:
            access.require(
                BOOKING_ACTION,
                site_id=correction_home(payload),
                brand_id=booking.brand_id,
                entity_id=booking.document.entity_id,
            )

        def handler(run: CommandRun) -> CommandResult:
            event = correct_booking(run, booking, payload, expected_revision=meta.expected_revision)
            return CommandResult(
                resource_type="booking_correction", resource_id=str(event.pk), status_code=201
            )

        result = self.run_command(
            request,
            access=access,
            action="bookings.correct",
            meta=meta,
            business_input=body,
            handler=handler,
            resource_ids=[str(pk)],
            site_id=booking.document.site_id,
            subject_key=f"document:{pk}",
        )
        return Response(
            _booking_resource(access, _reload(pk), progress=True, params={}),
            status=result.status_code,
        )
