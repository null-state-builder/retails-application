"""Vendor master + Booking API.

Booking capture is a two-step, human-in-the-loop flow:
  POST /api/bookings/draft   → Gemini reads the uploaded receiving doc, returns a
                               DRAFT (nothing saved). The handler edits it.
  POST /api/bookings/        → saves the booking + lines as a draft, for the Owner.

Every Booking endpoint answers on the ``booking`` section of the ratified access
table (#130): reading a booking needs ``view``, placing one needs ``operate``.
Until then these endpoints were open to any authenticated user, so the table's
Booking column decided nothing - which is why a store person's ratified
``view`` cell had to arrive with a gate behind it. *Which* bookings a caller
sees is the separate record-scope axis (``vendors.scoping``, #234) - a store
login sees a booking only if one of its lines lands at a store in their scope.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import require_section
from accounts.sections import CAP_APPROVE, CAP_OPERATE, CAP_VIEW
from approvals.services import AlreadyPendingError, ApprovalError
from core.documents import VoucherSeries
from core.textsearch import search_term, text_filter
from files.models import StoredFile
from masters.models import Brand, Season, Store
from masters.permissions import IsMasterSteward
from stockledger.contracts import goods_v1_site_ids
from vendors.agents import read_booking_receipt
from vendors.approval import ask_for_approval
from vendors.models import Booking, BookingLine, Vendor
from vendors.scoping import scope_bookings
from vendors.serializers import (
    BookingCreateSerializer,
    BookingSerializer,
    VendorSerializer,
    shows_booking_cost,
)

# The Booking column of the access table, as three DRF permissions. `view` is the
# screen and the list - the rung the store person now holds; `operate` is placing
# one, which stays HO's, the warehouse's and the brand manager's.
CanReadBooking = require_section("booking", CAP_VIEW)
CanPlaceBooking = require_section("booking", CAP_OPERATE)
CanReadOrPlaceBooking = require_section("booking", CAP_VIEW, write_minimum=CAP_OPERATE)
# Ending a commitment early is the table's Booking *Approve* cell — the rung the
# matrix has always granted (owner, HO ops) and no endpoint read until now. The
# brand manager who drafts an order does not get to write off its balance.
CanCloseBooking = require_section("booking", CAP_APPROVE)

#: What a typed term looks through on the vendor master — who they are, where
#: they are, and the number the accountant quotes.
VENDOR_SEARCH_FIELDS = ("code", "name", "city", "gstin")


def _rupees_to_paise(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _financial_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def _allocate_booking_number(season: Season) -> str:
    """Gap-free, collision-free booking number per (FY, season) — replaces the racy
    `count()+1` (which double-allocates under concurrent booking creation)."""
    fy = _financial_year(date.today())
    scope = season.code[:16]
    VoucherSeries.objects.get_or_create(fy=fy, store_code=scope, doc_type="BK")
    _, number = VoucherSeries.allocate(fy=fy, store_code=scope, doc_type="BK")
    return number


class VendorListCreateView(generics.ListCreateAPIView[Vendor]):
    """The vendor master. Reads stay open (every booking form needs the list);
    writes are the master-data steward's, the same gate stores, brands, seasons
    and GSTINs have carried since D8.

    Until this review the whole endpoint sat on a bare ``IsAuthenticated``: a
    store cashier could mint the supplier that every future booking, GRN and
    payable then hangs off, and nothing could correct one afterwards because
    there was no detail route at all.
    """

    serializer_class = VendorSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]

    def get_queryset(self) -> Any:
        qs = Vendor.objects.prefetch_related("brands")
        # Inactive vendors stay out of the pickers, and stay visible to the
        # steward who has to look after them (`?include_inactive=1`).
        if self.request.query_params.get("include_inactive") not in ("1", "true"):
            qs = qs.filter(is_active=True)
        return text_filter(qs, search_term(self.request), VENDOR_SEARCH_FIELDS)


class VendorDetailView(generics.RetrieveUpdateAPIView[Vendor]):
    """Correct a vendor, or retire one. Never deleted — bookings and payables
    point at it (masters are referenced by append-only rows)."""

    serializer_class = VendorSerializer
    permission_classes = [IsAuthenticated, IsMasterSteward]
    queryset = Vendor.objects.prefetch_related("brands")


class BookingDraftView(APIView):
    """Read an uploaded receiving doc into a draft booking (not saved).

    Nothing is saved, but the draft is the first half of placing a booking and it
    burns an AI read on an upload - so it sits at the same rung as the create,
    not at ``view``.
    """

    permission_classes = [IsAuthenticated, CanPlaceBooking]

    @extend_schema(
        request={"multipart/form-data": {
            "type": "object",
            "required": ["file"],
            "properties": {"file": {"type": "string", "format": "binary"}},
        }},
        responses={
            200: {
                "type": "object",
                "required": ["source_file_id", "lines"],
                "properties": {
                    "source_file_id": {"type": "integer"},
                    "lines": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                },
                "additionalProperties": True,
            },
            400: OpenApiResponse(description="No document uploaded"),
            422: OpenApiResponse(description="Document could not be read"),
        },
    )
    def post(self, request: Request) -> Response:
        upload = request.FILES.get("file")
        if not upload:
            return Response({"detail": "A file is required."}, status=400)
        stored = StoredFile.from_upload(upload, StoredFile.Kind.BOOKING_RECEIPT, request.user)
        try:
            result = read_booking_receipt(bytes(stored.content), stored.content_type)
        except Exception as exc:  # noqa: BLE001 - surface a clean message to the UI
            return Response(
                {
                    "detail": "Could not read the document. Please upload a clearer "
                    f"photo or the Excel/PDF. ({exc})",
                    "source_file_id": stored.id,
                },
                status=422,
            )
        # normalise lines for the UI; a login that may not see cost is not
        # shown the one the document printed either.
        costs = shows_booking_cost(request.user)
        for line in result.get("lines", []):
            line["mrp_paise"] = _rupees_to_paise(line.get("mrp"))
            if costs:
                line["cost_paise"] = _rupees_to_paise(line.get("cost"))
            else:
                line.pop("cost", None)
        result["source_file_id"] = stored.id
        return Response(result)


#: What a typed term looks through on the Bookings screen — the four things a
#: person half-remembers about an order: its number, who it was placed with,
#: whose goods they are, and which season it belongs to.
BOOKING_SEARCH_FIELDS = (
    "number",
    "vendor__name",
    "brand__name",
    "season__code",
    "season__name",
)


class BookingListCreateView(generics.ListCreateAPIView[Booking]):
    permission_classes = [IsAuthenticated, CanReadOrPlaceBooking]
    serializer_class = BookingSerializer

    def get_queryset(self) -> Any:
        qs = Booking.objects.select_related(
            "vendor", "brand", "season", "destination_store"
        ).prefetch_related("lines", "lines__store")
        qs = scope_bookings(qs, self.request.user)
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("brand"):
            qs = qs.filter(brand_id=params["brand"])
        if params.get("season"):
            qs = qs.filter(season_id=params["season"])
        # The screen's own search box (#102), applied last so it can only narrow
        # what the filters above already allow.
        return text_filter(qs, search_term(self.request), BOOKING_SEARCH_FIELDS)

    @transaction.atomic
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = BookingCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        brand: Brand = data["brand"]
        season: Season = data["season"]
        vendor: Vendor = data["vendor"]

        dest_id = data.get("destination_store")
        # A store on the goods-v1 stock system is received only by goods-v1
        # receiving, which refuses a legacy booking (`stockledger.contracts`).
        # Booked here, its goods could never be received anywhere - so it is
        # refused before anything is saved, the header and every line alike.
        named = {dest_id} if dest_id else set()
        named |= {_safe_int(r.get("store")) for r in data["lines"] if _safe_int(r.get("store"))}
        fenced = goods_v1_site_ids(named)
        if fenced:
            codes = ", ".join(
                sorted(Store.objects.filter(pk__in=fenced).values_list("code", flat=True))
            )
            return Response(
                {
                    "detail": (
                        f"{codes} runs on the new stock system, so this form cannot book "
                        "goods for it. Start the booking again and pick that store first."
                    )
                },
                status=400,
            )
        number = _allocate_booking_number(season)
        booking = Booking.objects.create(
            number=number,
            vendor=vendor,
            brand=brand,
            season=season,
            destination_store_id=dest_id,
            # A commitment starts as a draft and reaches "booked" through the
            # second person the access table always promised (D11 §3). It used to
            # be created `booked` outright, which meant the Owner's `booking:
            # approve` cell decided nothing at all.
            status=Booking.Status.DRAFT,
            vendor_ref=data.get("vendor_ref", ""),
            notes=data.get("notes", ""),
            ownership=brand.ownership,
            return_terms=brand.return_terms,
            source_file_id=data.get("source_file_id"),
            # IsAuthenticated (via CanReadOrPlaceBooking) guarantees a real
            # user, never AnonymousUser.
            created_by=cast(User, request.user),
        )
        est = 0
        costs = shows_booking_cost(request.user)
        # A booking can span several stores: each line may name its own destination
        # store; a line with none inherits the booking's default (destination_store).
        # Validate requested per-line stores once (fail-safe: unknown id → default).
        requested = {_safe_int(r.get("store")) for r in data["lines"] if _safe_int(r.get("store"))}
        valid_store_ids = (
            set(Store.objects.filter(pk__in=requested).values_list("id", flat=True))
            if requested
            else set()
        )
        for raw in data["lines"]:
            qty = int(raw.get("booked_qty") or raw.get("quantity") or 0)
            mrp = raw.get("mrp_paise")
            if mrp is None:
                mrp = _rupees_to_paise(raw.get("mrp"))
            cost = raw.get("cost_paise")
            if cost is None:
                cost = _rupees_to_paise(raw.get("cost"))
            if not costs:
                # Nobody records a figure they are not allowed to read back.
                cost = None
            line_store = _safe_int(raw.get("store"))
            line_store_id = line_store if line_store in valid_store_ids else None
            BookingLine.objects.create(
                booking=booking,
                store_id=line_store_id,
                style_code=str(raw.get("style_code", "")).strip(),
                size=str(raw.get("size") or "").strip(),
                description=str(raw.get("description") or "").strip(),
                booked_qty=qty,
                mrp_paise=mrp,
                cost_paise=cost,
            )
            if mrp:
                est += mrp * qty
        if est:
            booking.estimated_value_paise = est
            booking.save(update_fields=["estimated_value_paise", "updated_at"])
        return Response(
            BookingSerializer(booking, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class BookingDetailView(generics.RetrieveAPIView[Booking]):
    """Knowing a booking's id is not a way round the list's scope gate: out of
    scope must read exactly like not existing (404, never 403 - ADR-0003)."""

    permission_classes = [IsAuthenticated, CanReadBooking]
    serializer_class = BookingSerializer

    def get_queryset(self) -> Any:
        qs = Booking.objects.select_related(
            "vendor", "brand", "season", "destination_store"
        ).prefetch_related("lines", "lines__store")
        return scope_bookings(qs, self.request.user)


class BookingCloseView(APIView):
    """End a booking: short-close what will not arrive, or cancel one nothing came against.

    The reason is mandatory. An open order report is only worth reading if every
    row on it is expected — and "why did the other 60 pieces never come?" is a
    question the vendor conversation needs answered six months later.
    """

    permission_classes = [IsAuthenticated, CanCloseBooking]

    @extend_schema(
        request={"application/json": {
            "type": "object",
            "required": ["reason"],
            "properties": {
                "reason": {"type": "string", "minLength": 3},
                "action": {"type": "string", "enum": ["close", "cancel"]},
            },
        }},
        responses={200: BookingSerializer, 400: OpenApiResponse(description="Invalid reason"),
                   404: OpenApiResponse(description="Booking not found"),
                   409: OpenApiResponse(description="Booking cannot be ended")},
    )
    def post(self, request: Request, pk: int) -> Response:
        booking = (
            Booking.objects.select_related("vendor", "brand", "season", "destination_store")
            .prefetch_related("lines", "lines__store")
            .filter(pk=pk)
            .first()
        )
        if booking is None:
            return Response({"detail": "Booking not found."}, status=404)
        reason = str(request.data.get("reason") or "").strip()
        if len(reason) < 3:
            return Response(
                {"detail": "Say why this booking is ending — the reason is part of the record."},
                status=400,
            )
        cancel = str(request.data.get("action") or "close").lower() == "cancel"
        try:
            booking.end(user=request.user, reason=reason, cancel=cancel)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=409)
        return Response(BookingSerializer(booking, context={"request": request}).data)


class BookingSubmitView(APIView):
    """Send a drafted booking for the Owner's signature (D11 §3).

    The maker's last act on their own commitment. Approval places it - see
    `vendors.approval` - so there is no second step for anybody to forget, and no
    path from draft to booked that does not pass a second person.
    """

    permission_classes = [IsAuthenticated, CanPlaceBooking]

    @extend_schema(
        request=None,
        responses={201: BookingSerializer, 400: OpenApiResponse(description="Booking cannot be submitted"),
                   404: OpenApiResponse(description="Booking not found"),
                   409: OpenApiResponse(description="Approval already pending")},
    )
    def post(self, request: Request, pk: int) -> Response:
        booking = (
            Booking.objects.select_related("vendor", "brand", "season", "destination_store")
            .prefetch_related("lines", "lines__store")
            .filter(pk=pk)
            .first()
        )
        if booking is None:
            return Response({"detail": "Booking not found."}, status=404)
        if booking.status != Booking.Status.DRAFT:
            return Response(
                {
                    "detail": (
                        "Only a draft is sent for approval; this one is "
                        f"{booking.get_status_display().lower()}."
                    )
                },
                status=400,
            )
        if not booking.lines.exists():
            return Response(
                {"detail": "A booking with no lines commits nothing - add its styles first."},
                status=400,
            )
        try:
            ask_for_approval(booking, requested_by=request.user)
        except AlreadyPendingError as exc:
            return Response({"detail": str(exc)}, status=409)
        except ApprovalError as exc:
            return Response({"detail": str(exc)}, status=400)
        booking.status = Booking.Status.SUBMITTED
        booking.save(update_fields=["status", "updated_at"])
        return Response(BookingSerializer(booking, context={"request": request}).data, status=201)
