"""Outbound API views — list, create, dispatch, receive, submit.

Every endpoint requires authentication. RBAC:
  - Read (GET list/detail): any authenticated user, narrowed at the queryset to
    the caller's own stores — `masters.scoping` for the documents that carry a
    `store_id`, `outbound.scoping` for the transfer, which belongs to both ends
    of its move (#141). A document out of scope answers 404, never 403: a 403
    would confirm it exists, which is the thing being withheld (ADR-0003)
  - Write (POST create/submit/dispatch/receive): the endpoint group's section
    gate — see the table in ``outbound.permissions``, which is the one place
    the mapping lives
  - Store-scoped roles: may only write against their assigned stores
    (enforced via ``enforce_store_scope`` on every write path)
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Any, cast

import openpyxl
from django.db.models import QuerySet, Sum
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from core.documents import DocStatus
from core.money import paise_to_rupees_str
from core.textsearch import search_term, text_filter
from finledger.models import PartnerLedgerEntry
from finledger.posting import (
    AlreadyReversedError,
    account_for_mode,
    post_partner_settlement,
    reverse_partner_settlement,
    rupees_to_paise,
)
from masters.models import Brand, Store
from masters.scoping import (
    actionable_store_ids,
    scope_by_entitlement,
    scope_by_store,
    scope_by_store_or_brand,
)
from outbound.costing import on_hand_valuation
from outbound.counting import (
    CountError,
    MovedMidCountError,
    RecountRequiredError,
    SameCounterError,
    apply_variance,
    open_session,
    open_stocktake,
    record_recount,
    record_scans,
    recount_tolerance,
    submit_session,
    variance_report,
)
from outbound.maker_checker import ask_again
from outbound.models import (
    BillingPolicy,
    CountSession,
    MarkDamaged,
    ReceiptStatus,
    ReturnToVendor,
    StockAdjustment,
    StockRequest,
    Stocktake,
    StoreTransfer,
    TransferGapClosure,
    TransferPT,
    VFlip,
    WriteOff,
)
from outbound.permissions import (
    CanCloseTransferGap,
    CanCreateReturnToBrand,
    CanExecuteVFlip,
    CanFlipOwnership,
    CanManageBillingPolicy,
    CanManagePartnerSettlements,
    CanReadReturnToBrand,
    CanReadTransferPT,
    CanViewPartnerDues,
    CanWriteReturnToBrand,
    CanWriteStockCount,
    CanWriteTransfer,
    enforce_store_scope,
)
from outbound.posting import (
    OutboundPostingError,
    amend_gap_closure,
    close_stock_request,
    fulfil_stock_request,
    mark_damaged,
    post_adjustment,
    post_gap_closure,
    post_rtv,
    post_transfer_dispatch,
    post_transfer_receipt,
    post_vflip,
    post_writeoff,
    raise_gap_closure,
    raise_return_to_brand,
)
from outbound.returnable import cap_for, returnable_pool
from outbound.scoping import scope_stock_requests, scope_transfers
from outbound.serializers import (
    ApplyVarianceInputSerializer,
    BillingPolicySerializer,
    CountScanInputSerializer,
    CountSessionCreateSerializer,
    CountSessionReadSerializer,
    CreditNoteSerializer,
    GapClosureInputSerializer,
    GapClosureReadSerializer,
    MarkDamagedInputSerializer,
    MarkDamagedReadSerializer,
    RecountInputSerializer,
    ReturnToBrandCreateSerializer,
    ReturnToVendorReadSerializer,
    StockAdjustmentReadSerializer,
    StockAdjustmentWriteSerializer,
    StockRequestCloseInputSerializer,
    StockRequestFulfilInputSerializer,
    StockRequestReadSerializer,
    StockRequestWriteSerializer,
    StocktakeCreateSerializer,
    StocktakeReadSerializer,
    StoreTransferReadSerializer,
    StoreTransferWriteSerializer,
    TransferPTSerializer,
    TransferReceiveInputSerializer,
    TransferScanInputSerializer,
    VFlipReadSerializer,
    VFlipWriteSerializer,
    WriteOffReadSerializer,
    WriteOffWriteSerializer,
)
from outbound.transfer_pt import KDPS_COLUMNS
from stockledger.views import search_on_hand

if TYPE_CHECKING:
    from accounts.models import User

#: The people on a document's approval trail. Every maker-checker document's read
#: shape joins all three, and a response that forgets one N+1s on names.
APPROVAL_JOINS = (
    "approvals__made_by",
    "approvals__requested_by",
    "approvals__decided_by",
)

# These APIViews assemble their output directly, so document the actual wire
# shapes rather than asking spectacular to infer a serializer that never runs.
MERCH_DIM_SCHEMA = {name: {"type": "string"} for name in (
    "design", "color", "size", "brand", "season", "item", "hsn"
)}
ERROR_SCHEMA = {
    "type": "object", "required": ["error"],
    "properties": {"error": {"type": "string"}},
}
SCAN_LOOKUP_SCHEMA = {
    "type": "object",
    "required": ["barcode", "available_qty", *MERCH_DIM_SCHEMA],
    "properties": {
        "barcode": {"type": "string"},
        **MERCH_DIM_SCHEMA,
        "available_qty": {"type": "integer"},
    },
}
COUNT_LOOKUP_SCHEMA = {
    "type": "object",
    "required": ["barcode", *MERCH_DIM_SCHEMA],
    "properties": {"barcode": {"type": "string"}, **MERCH_DIM_SCHEMA},
}
CROSS_LOCATION_SCHEMA = {
    "type": "object",
    "required": ["rows", "truncated"],
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["store_code", "store_name", "sku_code", "qty", "is_own", *MERCH_DIM_SCHEMA],
                "properties": {
                    "store_code": {"type": "string"},
                    "store_name": {"type": "string"},
                    "sku_code": {"type": "string"},
                    **MERCH_DIM_SCHEMA,
                    "qty": {"type": "integer"},
                    "is_own": {"type": "boolean"},
                    "unit_cost_paise": {"type": "integer"},
                    "landed_value_paise": {"type": "integer"},
                    "margin_paise": {"type": "integer", "nullable": True},
                },
            },
        },
        "truncated": {"type": "boolean"},
    },
}
DISTRIBUTION_SPLIT_SCHEMA = {
    "type": "object",
    "required": ["weights"],
    "properties": {
        "weights": {
            "type": "object",
            "description": "Size or _default to destination store id and fractional weight.",
            "additionalProperties": {
                "type": "object", "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1}
            },
        }
    },
}
PARTNER_DUES_SCHEMA = {
    "type": "object",
    "required": ["stores", "total_owed_paise", "total_paid_paise", "net_outstanding_paise", "billing_mode"],
    "properties": {
        "stores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "store_id": {"type": "integer"},
                    "store_code": {"type": "string"},
                    "store_name": {"type": "string"},
                    "total_owed_paise": {"type": "integer"},
                    "total_paid_paise": {"type": "integer"},
                    "net_outstanding_paise": {"type": "integer"},
                    "transfer_count": {"type": "integer"},
                    "last_dispatch_date": {"type": "string", "format": "date-time", "nullable": True},
                    "transfers": {
                        "type": "array", "items": {
                            "type": "object", "properties": {
                                "id": {"type": "integer"},
                                "doc_number": {"type": "string", "nullable": True},
                                "source_store_code": {"type": "string"},
                                "dispatch_date": {"type": "string", "format": "date-time"},
                                "partner_billing_value_paise": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
        "total_owed_paise": {"type": "integer"},
        "total_paid_paise": {"type": "integer"},
        "net_outstanding_paise": {"type": "integer"},
        "billing_mode": {"type": "string", "enum": list(BillingPolicy.Mode.values)},
    },
}
PARTNER_SETTLEMENT_LIST_SCHEMA = {
    "type": "object", "required": ["rows"],
    "properties": {"rows": {"type": "array", "items": {
        "type": "object", "properties": {
            "id": {"type": "integer"},
            "created_at": {"type": "string", "format": "date-time"},
            "doc_number": {"type": "string", "nullable": True},
            "kind": {"type": "string"},
            "store_id": {"type": "integer"},
            "store_code": {"type": "string"},
            "store_name": {"type": "string"},
            "amount_paise": {"type": "integer"},
            "amount_rupees": {"type": "string"},
            "description": {"type": "string"},
            "reference": {"type": "string"},
            "mode": {"type": "string"},
            "posted_by_name": {"type": "string"},
        },
    }}},
}
PARTNER_SETTLEMENT_REQUEST = {
    "type": "object", "required": ["store_id", "amount"],
    "properties": {
        "store_id": {"type": "integer"},
        "amount": {
            "anyOf": [{"type": "string"}, {"type": "number"}],
            "description": "Positive rupee amount; converted to paise by the server.",
        },
        "mode": {"type": "string"},
        "description": {"type": "string"},
        "reference": {"type": "string"},
    },
}
PARTNER_SETTLEMENT_CREATED_SCHEMA = {
    "type": "object", "required": ["id", "doc_number", "amount_paise", "amount_rupees"],
    "properties": {
        "id": {"type": "integer"}, "doc_number": {"type": "string"},
        "amount_paise": {"type": "integer"}, "amount_rupees": {"type": "string"},
    },
}
PARTNER_SETTLEMENT_REVERSED_SCHEMA = {
    "type": "object", "required": ["id", "doc_number"],
    "properties": {"id": {"type": "integer"}, "doc_number": {"type": "string"}},
}
RETURNABLE_POOL_SCHEMA = {
    "type": "object", "required": ["brand", "excluded_reason", "cap", "rows"],
    "properties": {
        "brand": {"type": "object", "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"},
            "commercial_label": {"type": "string"}, "takes_returns": {"type": "boolean"},
            "return_window_days": {"type": "integer", "nullable": True},
        }},
        "excluded_reason": {"type": "string"},
        "cap": {"type": "object", "properties": {
            "applies": {"type": "boolean"}, "percent": {"type": "string"},
            **{name: {"type": "integer"} for name in (
                "delivered_paise", "allowance_paise", "used_paise", "remaining_paise",
                "warn_at_paise", "this_return_paise", "exceeded_by_paise"
            )},
            "warn": {"type": "boolean"},
        }},
        "rows": {"type": "array", "items": {"type": "object", "properties": {
            "source": {"type": "string"}, "store": {"type": "integer"},
            "store_code": {"type": "string"}, "store_name": {"type": "string"},
            "sku_code": {"type": "string"}, **MERCH_DIM_SCHEMA,
            "qty": {"type": "integer"}, "unit_cost_paise": {"type": "integer"},
            "value_paise": {"type": "integer"},
            "arrived_on": {"type": "string", "format": "date", "nullable": True},
            "window_date": {"type": "string", "format": "date", "nullable": True},
            "days_left": {"type": "integer", "nullable": True}, "expired": {"type": "boolean"},
        }}},
    },
}
VARIANCE_LINE_SCHEMA = {
    "type": "object", "properties": {
        "sku_code": {"type": "string"}, **MERCH_DIM_SCHEMA,
        **{name: {"type": "integer"} for name in (
            "book_qty", "first_counted_qty", "counted_qty", "adj_qty",
            "live_book_qty", "unit_cost_paise", "variance_paise"
        )},
        **{name: {"type": "boolean"} for name in (
            "moved", "cost_known", "above_tolerance", "needs_recount", "may_recount"
        )},
        "recount": {"type": "object", "nullable": True, "properties": {
            "counted_qty": {"type": "integer"}, "first_counted_qty": {"type": "integer"},
            "reason": {"type": "string"}, "reason_label": {"type": "string"},
            "recounted_by_name": {"type": "string"},
            "recounted_at": {"type": "string", "format": "date-time"},
            "unit_cost_paise": {"type": "integer"}, "stale": {"type": "boolean"},
        }},
    },
}
VARIANCE_SCHEMA = {
    "type": "object", "required": ["stocktake", "store_code", "status", "lines"],
    "properties": {
        "stocktake": {"type": "integer"}, "store_code": {"type": "string"},
        "status": {"type": "string"}, "recount_tolerance_paise": {"type": "integer"},
        "lines": {"type": "array", "items": VARIANCE_LINE_SCHEMA},
        "net_pieces": {"type": "integer"}, "net_variance_paise": {"type": "integer"},
        "unpriced": {"type": "array", "items": {"type": "string"}},
        "awaiting_recount": {"type": "array", "items": {"type": "string"}},
    },
}


def _filter_docstatus[QS: QuerySet[Any]](qs: QS, request: Request) -> QS:
    """Apply the optional ``?docstatus=`` filter to a list queryset.

    A non-integer value is a client error, not a server one — return a
    controlled 400 instead of letting ``int()`` raise an uncaught 500.
    """
    ds = request.query_params.get("docstatus")
    if ds is None:
        return qs
    try:
        code = int(ds)
    except (TypeError, ValueError):
        raise ValidationError({"docstatus": "must be an integer"}) from None
    return qs.filter(docstatus=code)


# ---------------------------------------------------------------------------
# Transfer views
# ---------------------------------------------------------------------------


#: What a typed term looks through on the Transfers screen — the voucher number,
#: and either end of the movement by code or by name (a store person says
#: "Deoghar", the document says "DEO").
TRANSFER_SEARCH_FIELDS = (
    "doc_number",
    "source_store__code",
    "source_store__name",
    "destination_store__code",
    "destination_store__name",
)


def _transfers(user: Any) -> QuerySet[StoreTransfer]:
    """Transfers the caller is an end of (#141), in the read shape.

    Scoped here rather than at each view, so a new transfer read cannot be added
    ungated — the only way to a `StoreTransfer` on a read path is through this.
    """
    qs = StoreTransfer.objects.select_related(
        "source_store", "destination_store", "created_by"
    ).prefetch_related("lines", *APPROVAL_JOINS)
    return scope_transfers(qs, user)


class TransferListCreateView(generics.ListCreateAPIView[StoreTransfer]):
    def get_permissions(self) -> list[BasePermission]:
        if self.request.method == "POST":
            return [CanWriteTransfer()]
        return [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[StoreTransfer]:
        # Scoped before anything else narrows it, so the typed term filters the
        # caller's own transfers rather than the network's (#141, #102).
        qs = _transfers(self.request.user)
        ttype = self.request.query_params.get("type")
        if ttype:
            qs = qs.filter(transfer_type=ttype)
        qs = _filter_docstatus(qs, self.request)
        return text_filter(qs, search_term(self.request), TRANSFER_SEARCH_FIELDS)

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.request.method == "POST":
            return StoreTransferWriteSerializer
        return StoreTransferReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = StoreTransferWriteSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        enforce_store_scope(request.user, ser.validated_data["source_store"].id)
        instance = ser.save()
        return Response(
            StoreTransferReadSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


class TransferDetailView(generics.RetrieveAPIView[StoreTransfer]):
    #: Scoped, so a transfer between two other stores is a 404 — knowing the id
    #: is not a way in, and the answer must not tell you the document is real.
    permission_classes = [IsAuthenticated]
    serializer_class = StoreTransferReadSerializer

    def get_queryset(self) -> QuerySet[StoreTransfer]:
        return _transfers(self.request.user)


class TransferDispatchView(APIView):
    """POST: Dispatch a draft transfer from scanned lines only (#68).

    Payload: ``{"scans": [{"barcode": ..., "qty": ...}, ...]}`` — the scanned
    quantities are the only quantities; typed dispatch is gone. Stock moves
    source → in-transit bucket under this transfer.
    """

    permission_classes = [CanWriteTransfer]

    @extend_schema(request=TransferScanInputSerializer, responses={200: StoreTransferReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            transfer = (
                StoreTransfer.objects.select_related("source_store", "destination_store")
                .prefetch_related("lines")
                .get(pk=pk)
            )
        except StoreTransfer.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, transfer.source_store_id)

        if transfer.docstatus != DocStatus.DRAFT:
            return Response(
                {"error": "Only drafts can be dispatched"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = TransferScanInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            post_transfer_dispatch(transfer, ser.scans_by_barcode(), user=request.user)
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        transfer.refresh_from_db()
        return Response(StoreTransferReadSerializer(transfer).data)


class TransferReceiveView(APIView):
    """POST: Receive a dispatched transfer from scanned lines only (#68, #71).

    Payload: ``{"scans": [...], "damaged": [...], "extras": [...], "notes": ""}``
    — each list of ``{"barcode", "qty"}``. Everything that turned up moves
    in-transit → destination, broken pieces included, and a damage document is
    raised for those: quarantined on the spot if the receiver holds the
    confirming rung, otherwise flagged and left in stock (#138). Extras (not on
    the transfer) are accepted in with a flag; a short receive leaves the
    remainder in-transit and opens a gap. The notes reach the server and are
    stored.
    """

    permission_classes = [CanWriteTransfer]

    @extend_schema(request=TransferReceiveInputSerializer, responses={200: StoreTransferReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            transfer = (
                StoreTransfer.objects.select_related("source_store", "destination_store")
                .prefetch_related("lines")
                .get(pk=pk)
            )
        except StoreTransfer.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, transfer.destination_store_id)

        if transfer.docstatus != DocStatus.SUBMITTED:
            return Response(
                {"error": "Only dispatched transfers can be received"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = TransferReceiveInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            post_transfer_receipt(
                transfer,
                ser.scans_by_barcode(),
                user=request.user,
                damaged=ser.damaged_by_barcode(),
                extras=ser.extras_by_barcode(),
                notes=ser.validated_data["notes"],
            )
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(StoreTransferReadSerializer(_transfer_for_read(pk)).data)


# ---------------------------------------------------------------------------
# The transfer's PT — read, download, print (#72)
# ---------------------------------------------------------------------------


class TransferPTBaseView(APIView):
    """Everything the three PT endpoints share: find it, or say there is none.

    Read-only by construction. The PT is regenerated from the scanned lines or
    it does not change (#72), so there is no PUT, PATCH or POST on any of these;
    anything but GET is a 405.

    Gated on the transfer section's lowest rung, because the file is priced and
    people forward it. That is a *transfer* right and nothing else: no inbound-PT
    right is consulted here, so the rulings on who may make (#119) or inward
    (#124) a brand's PT cannot reach this document. Making a transfer's own
    packing list is part of transferring.

    Store-scoped on top of that right, through the transfer itself (#141). The
    right says *this kind of document*; the scope says *this one*. Without the
    second, the PT would be the way round the gate on the transfer it belongs to
    — and the worse way, since these rows carry the unit cost the detail does not.
    """

    permission_classes = [CanReadTransferPT]

    #: File extension for the download views; the JSON view has none.
    extension = ""

    @extend_schema(responses={200: TransferPTSerializer})
    def get(self, request: Request, pk: int) -> HttpResponse:
        # Reached through the scoped transfer, so another store's PT is a 404 —
        # the same answer as a transfer that does not exist, and as a dispatched
        # transfer with no PT yet.
        if not _transfers(request.user).filter(pk=pk).exists():
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        pt = (
            TransferPT.objects.select_related(
                "transfer__source_store", "transfer__destination_store"
            )
            .filter(transfer_id=pk)
            .first()
        )
        # A draft has scanned nothing, so it has no carton and no document —
        # that is a 404, not an empty file.
        if pt is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        return self.render(pt)

    def render(self, pt: TransferPT) -> HttpResponse:  # pragma: no cover - subclassed
        raise NotImplementedError

    def rows_in_column_order(self, pt: TransferPT) -> list[list[Any]]:
        """The stored rows as plain lists, in the KDPS column order — the shape
        both file formats write."""
        return [[row.get(column, "") for column in KDPS_COLUMNS] for row in pt.rows]

    def as_attachment(self, resp: HttpResponse, pt: TransferPT) -> HttpResponse:
        """Name the download after the voucher, with the slashes a voucher series
        uses flattened out of the filename: ``KDPS-PT-PT-A-STO-26-27-0001.csv``."""
        stem = (pt.transfer.doc_number or f"transfer-{pt.transfer_id}").replace("/", "-")
        resp["Content-Disposition"] = f'attachment; filename="KDPS-PT-{stem}.{self.extension}"'
        return resp


class TransferPTView(TransferPTBaseView):
    """GET: the transfer's PT, whole — the shape the print screen renders."""

    def render(self, pt: TransferPT) -> HttpResponse:
        return Response(TransferPTSerializer(pt).data)


@extend_schema_view(get=extend_schema(responses={(200, "text/csv"): OpenApiTypes.BINARY}))
class TransferPTCsvView(TransferPTBaseView):
    """GET: the PT as CSV, in KDPS column order."""

    extension = "csv"

    def render(self, pt: TransferPT) -> HttpResponse:
        resp = HttpResponse(content_type="text/csv")
        writer = csv.writer(resp)
        writer.writerow(KDPS_COLUMNS)
        writer.writerows(self.rows_in_column_order(pt))
        return self.as_attachment(resp, pt)


@extend_schema_view(
    get=extend_schema(
        responses={
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"): OpenApiTypes.BINARY
        }
    )
)
class TransferPTXlsxView(TransferPTBaseView):
    """GET: the PT as a real .xlsx — the file a brand or a store opens."""

    extension = "xlsx"

    def render(self, pt: TransferPT) -> HttpResponse:
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "KDPS PT"
        ws.append(KDPS_COLUMNS)
        for row in self.rows_in_column_order(pt):
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return self.as_attachment(resp, pt)


# ---------------------------------------------------------------------------
# Gaps — transfers where sent ≠ received (#71)
# ---------------------------------------------------------------------------


def _transfer_for_read(pk: int) -> StoreTransfer:
    """Re-read a transfer with everything the read shape joins on.

    Re-read rather than ``refresh_from_db``: the response carries the fresh
    receipt, its exceptions and the gap closure, and a refreshed instance keeps
    the stale prefetch caches from before the post.
    """
    return (
        StoreTransfer.objects.select_related("source_store", "destination_store", "created_by")
        .prefetch_related("lines", "receipt__exceptions", "gap_closure__lines")
        .get(pk=pk)
    )


def _open_gap_transfers() -> QuerySet[StoreTransfer]:
    """Every transfer where what was sent and what was received do not agree,
    and nobody has yet said why.

    A shortfall receipt is the flag; the absence of a *posted* closure is what
    keeps it on the list. A transfer merely on the road is not a gap — it is
    in transit, which the in-transit view already reports.
    """
    return (
        StoreTransfer.objects.filter(
            docstatus=DocStatus.SUBMITTED,
            receipt__receipt_status=ReceiptStatus.SHORTFALL,
        )
        .exclude(gap_closure__docstatus=DocStatus.SUBMITTED)
        .select_related("source_store", "destination_store", "created_by")
        .prefetch_related("lines", "receipt__exceptions", "gap_closure__lines")
    )


class TransferGapListView(generics.ListAPIView[StoreTransfer]):
    """GET: the gaps list — every open gap, for the warehouse/HO screen.

    Scoped on the *source* store: the sender is answerable for the pieces until
    the receiver scans them in, so a gap is the sender's to explain. That also
    means the receiving store does not find its own gap on this list.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = StoreTransferReadSerializer
    pagination_class = None

    def get_queryset(self) -> QuerySet[StoreTransfer]:
        gaps: QuerySet[StoreTransfer] = scope_by_store(
            _open_gap_transfers(), self.request.user, "source_store_id"
        )
        return gaps


class TransferGapClosureCreateView(APIView):
    """POST: raise the closure for a transfer's gap — reason + optional note.

    Creating it does not close anything: the draft goes straight into the
    approvals inbox, and only a senior's approval lets it post. The lines are
    read off the transfer's in-transit remainder, so the person raising it
    cannot restate how much went missing.
    """

    permission_classes = [CanCloseTransferGap]

    @extend_schema(request=GapClosureInputSerializer, responses={201: GapClosureReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            transfer = _transfer_for_read(pk)
        except StoreTransfer.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        # The gap belongs to the sender, so this is a write against the sender's
        # store; the receiving store's own bar is in ``_refuse_self_closure``.
        enforce_store_scope(request.user, transfer.source_store_id)

        ser = GapClosureInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            closure = raise_gap_closure(
                transfer,
                reason=ser.validated_data["reason"],
                note=ser.validated_data["note"],
                user=request.user,
            )
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            GapClosureReadSerializer(_gap_closure_for_read(closure.pk)).data,
            status=status.HTTP_201_CREATED,
        )


def _gap_closure_for_read(pk: int) -> TransferGapClosure:
    return (
        TransferGapClosure.objects.select_related(
            "store",
            "created_by",
            "approved_by",
            "transfer__source_store",
            "transfer__destination_store",
        )
        .prefetch_related("lines", *APPROVAL_JOINS)
        .get(pk=pk)
    )


class GapClosureDetailView(generics.RetrieveAPIView[TransferGapClosure]):
    #: Reading a closure is store-scoped like outbound's other reads (#141);
    #: correcting one is a write, and carries the same senior gate as raising and
    #: posting it.
    permission_classes = [IsAuthenticated]
    serializer_class = GapClosureReadSerializer

    def get_permissions(self) -> list[BasePermission]:
        return [CanCloseTransferGap()] if self.request.method == "PATCH" else [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[TransferGapClosure]:
        # Scoped by *entitlement*, not by the switcher, because this is the one
        # outbound detail that is also fetched in order to act on it: the PATCH
        # below corrects the draft. The top bar chooses what you are looking at
        # and must never decide what you may do, so a senior entitled to the
        # sending store may still correct its closure with another unit on
        # screen. Everything the correction itself refuses — a posted closure, a
        # senior at the receiving store — stays in `amend_gap_closure`, and still
        # answers 400 with its reason rather than a silent 404.
        closures: QuerySet[TransferGapClosure] = scope_by_entitlement(
            TransferGapClosure.objects.select_related(
                "store",
                "created_by",
                "approved_by",
                "transfer__source_store",
                "transfer__destination_store",
            ).prefetch_related("lines", *APPROVAL_JOINS),
            self.request.user,
            "store_id",
        )
        return closures

    def patch(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Correct a draft closure — a new reason, a new note, lines re-read.

        The way back from a rejection or a stale draft, so one wrong reason code
        cannot strand the pieces in transit for good.
        """
        closure = self.get_object()
        enforce_store_scope(request.user, closure.store_id)

        ser = GapClosureInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            amend_gap_closure(
                closure,
                reason=ser.validated_data["reason"],
                note=ser.validated_data["note"],
                user=cast("User", request.user),
            )
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(GapClosureReadSerializer(_gap_closure_for_read(closure.pk)).data)


class GapClosureSubmitView(APIView):
    """POST: post an approved gap closure — the resolving entries land here.

    The rules that matter (approved by a second, senior person; never anybody
    entitled to the receiving store; the bucket still holds what the draft says)
    all live in ``posting.post_gap_closure``, so a shell or a management command
    hits the same wall this endpoint does.
    """

    permission_classes = [CanCloseTransferGap]

    @extend_schema(request=None, responses={200: GapClosureReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            closure = _gap_closure_for_read(pk)
        except TransferGapClosure.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, closure.store_id)

        # No draft check here: ``post_gap_closure`` makes it, and a second copy
        # would only be a second place for the two to disagree.
        try:
            post_gap_closure(closure, user=request.user)
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(GapClosureReadSerializer(_gap_closure_for_read(pk)).data)


class ScanLookupView(APIView):
    """GET ?store=&barcode= — per-scan validation for the scan screen.

    Returns the piece's identity + available qty at the location (for the
    right-piece beep and scan-to-build), 404 when the location holds no such
    stock (the wrong-piece beep).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[OpenApiParameter("store", int, required=True), OpenApiParameter("barcode", str, required=True)],
        responses={200: SCAN_LOOKUP_SCHEMA, 400: ERROR_SCHEMA, 404: ERROR_SCHEMA},
    )
    def get(self, request: Request) -> Response:
        from stockledger.models import StockOnHand, merch_dims

        store_id = request.query_params.get("store")
        barcode = (request.query_params.get("barcode") or "").strip()
        if not store_id or not barcode:
            return Response(
                {"error": "Pass store= and barcode="}, status=status.HTTP_400_BAD_REQUEST
            )

        # Fail-closed (ADR-0003): a store-scoped user can only probe stock at
        # their own stores — an out-of-scope store looks identical to no stock.
        # Scoped by entitlement, not by the switcher: the scan screen names the
        # store it is standing in, so the top bar must not veto scanning there.
        visible = scope_by_entitlement(StockOnHand.objects.all(), request.user, "store_id")
        try:
            on_hand = visible.get(store_id=int(store_id), sku_code=barcode)
        except (StockOnHand.DoesNotExist, ValueError):
            return Response(
                {"error": f"No stock for {barcode} at this location"},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "barcode": on_hand.sku_code,
                **merch_dims(on_hand),
                "available_qty": on_hand.net_qty,
            }
        )


class CrossLocationStockSearchView(APIView):
    """GET ?q=&store= — stock across *every* location, for a store building a
    pull request (#74).

    The one place in outbound a person sees stock that is not theirs: identity
    and quantity are shown for every active store and warehouse — Anand's
    ruling of 26 July says the search is the whole point — but cost, landed
    value and margin show only for the caller's own location(s). Deliberately
    not ``scope_by_entitlement``/``scope_by_store``, which would hide other
    locations' rows entirely; only the money fields are gated here, per-row.
    """

    permission_classes = [IsAuthenticated]
    MAX_LINES = 500

    @extend_schema(
        parameters=[OpenApiParameter("q", str), OpenApiParameter("store", str)],
        responses={200: CROSS_LOCATION_SCHEMA},
    )
    def get(self, request: Request) -> Response:
        from masters.models import Sku
        from stockledger.models import StockOnHand, merch_dims

        qs = StockOnHand.objects.filter(net_qty__gt=0, store__is_active=True).select_related(
            "store"
        )
        if store_code := request.query_params.get("store"):
            qs = qs.filter(store__code=store_code)
        qs = search_on_hand(qs, search_term(request))
        qs = qs.order_by("store__code", "sku_code")

        rows = list(qs[: self.MAX_LINES])
        mrps = dict(
            Sku.objects.filter(barcode__in={r.sku_code for r in rows}).values_list(
                "barcode", "mrp_paise"
            )
        )
        # None (not a list) reads as "unrestricted" — a network role sees cost
        # everywhere, same as it already does on every other stock screen.
        own_ids = actionable_store_ids(request.user)

        data = []
        for row in rows:
            is_own = own_ids is None or row.store_id in own_ids
            entry = {
                "store_code": row.store.code,
                "store_name": row.store.name,
                "sku_code": row.sku_code,
                **merch_dims(row),
                "qty": row.net_qty,
                "is_own": is_own,
            }
            if is_own:
                entry.update(on_hand_valuation(row, mrps.get(row.sku_code) or 0))
            data.append(entry)

        return Response(
            {
                "rows": data,
                "truncated": qs.count() > len(rows),
            }
        )


class DistributionSuggestedSplitView(APIView):
    """GET ?warehouse=&brand=&stores=<comma-separated ids> — the Distribution
    grid's pre-fill (#73): a per-size weight for each destination store,
    scoped the same as raising a transfer out of ``warehouse`` (the grid
    creates one draft per store, so seeing the suggestion needs no more than
    creating the drafts already does).
    """

    permission_classes = [CanWriteTransfer]

    @extend_schema(
        parameters=[
            OpenApiParameter("warehouse", int, required=True),
            OpenApiParameter("brand", str),
            OpenApiParameter("stores", str, description="Comma-separated destination store ids"),
        ],
        responses={200: DISTRIBUTION_SPLIT_SCHEMA},
    )
    def get(self, request: Request) -> Response:
        from outbound.distribution import suggest_split

        warehouse_raw = request.query_params.get("warehouse")
        if not warehouse_raw:
            raise ValidationError({"warehouse": "Required."})
        try:
            warehouse_id = int(warehouse_raw)
        except ValueError as exc:
            raise ValidationError({"warehouse": "Must be a store id."}) from exc
        enforce_store_scope(request.user, warehouse_id)

        store_ids = []
        for raw in request.query_params.get("stores", "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                store_ids.append(int(raw))
            except ValueError as exc:
                raise ValidationError(
                    {"stores": "Must be a comma-separated list of store ids."}
                ) from exc

        weights = suggest_split(
            warehouse_id=warehouse_id,
            brand=request.query_params.get("brand", ""),
            store_ids=store_ids,
        )
        return Response({"weights": weights})


class BillingPolicyView(APIView):
    """The one partner-billing dial: informational-only, or also a receivable
    at Purchase Price. Read at `money: view`, changed at `money: manage` —
    see `CanManageBillingPolicy`."""

    permission_classes = [IsAuthenticated, CanManageBillingPolicy]

    @extend_schema(responses={200: BillingPolicySerializer})
    def get(self, request: Request) -> Response:
        return Response(BillingPolicySerializer(BillingPolicy.current()).data)

    @extend_schema(
        request={"application/json": {
            "type": "object", "required": ["mode"], "properties": {
                "mode": {"type": "string", "enum": list(BillingPolicy.Mode.values)}
            }
        }},
        responses={200: BillingPolicySerializer},
    )
    def put(self, request: Request) -> Response:
        policy = BillingPolicy.current()
        mode = request.data.get("mode")
        if mode not in BillingPolicy.Mode.values:
            raise ValidationError({"mode": f"must be one of {BillingPolicy.Mode.values}"})
        policy.mode = mode
        # Gated by `CanManageBillingPolicy` above, so this is a real user —
        # the anonymous one the request type still allows never reaches here,
        # and could not be stored on the column if it did. Same everywhere below.
        policy.set_by = cast("User", request.user)
        policy.save(update_fields=["mode", "set_by", "updated_at"])
        return Response(BillingPolicySerializer(policy).data)


class PartnerDuesView(APIView):
    """What each partner store owes, summed off the figure every transfer to
    it already carries (`partner_billing_value_paise`) — a read-only report
    over data that exists regardless of `BillingPolicy.mode`, so it means the
    same thing whether or not that figure ever reached the ledger. A dispatch
    still in DRAFT owes nothing yet: this only counts SUBMITTED transfers,
    the same rung `posting._bill_partner_store` only ever sets the figure at.

    Net Outstanding offsets that billed total against `PartnerLedgerEntry` —
    what Accounts has recorded as received (`PartnerSettlementsView`). There
    is no BILL kind on that ledger to double-count: the billed side stays this
    view's own aggregation, exactly as before settlements existed."""

    permission_classes = [IsAuthenticated, CanViewPartnerDues]

    @extend_schema(responses={200: PARTNER_DUES_SCHEMA})
    def get(self, request: Request) -> Response:
        transfers = (
            StoreTransfer.objects.filter(
                destination_store__is_partner=True,
                docstatus=DocStatus.SUBMITTED,
                partner_billing_value_paise__isnull=False,
            )
            .select_related("source_store", "destination_store")
            .order_by("-dispatch_date")
        )

        by_store: dict[int, dict[str, Any]] = {}
        for t in transfers:
            store = t.destination_store
            row = by_store.setdefault(
                store.id,
                {
                    "store_id": store.id,
                    "store_code": store.code,
                    "store_name": store.name,
                    "total_owed_paise": 0,
                    "transfer_count": 0,
                    "last_dispatch_date": None,
                    "transfers": [],
                },
            )
            row["total_owed_paise"] += t.partner_billing_value_paise
            row["transfer_count"] += 1
            if row["last_dispatch_date"] is None:
                row["last_dispatch_date"] = t.dispatch_date
            row["transfers"].append(
                {
                    "id": t.id,
                    "doc_number": t.doc_number,
                    "source_store_code": t.source_store.code,
                    "dispatch_date": t.dispatch_date,
                    "partner_billing_value_paise": t.partner_billing_value_paise,
                }
            )

        paid_by_store = {
            r["store_id"]: r["paid"] or 0
            for r in PartnerLedgerEntry.objects.filter(store_id__in=by_store.keys())
            .values("store_id")
            .annotate(paid=Sum("amount"))
        }
        for store_id, row in by_store.items():
            paid = paid_by_store.get(store_id, 0)
            row["total_paid_paise"] = paid
            row["net_outstanding_paise"] = row["total_owed_paise"] - paid

        stores = sorted(by_store.values(), key=lambda r: r["net_outstanding_paise"], reverse=True)
        total_paid = sum(paid_by_store.values())
        total_owed = sum(r["total_owed_paise"] for r in stores)
        return Response(
            {
                "stores": stores,
                "total_owed_paise": total_owed,
                "total_paid_paise": total_paid,
                "net_outstanding_paise": total_owed - total_paid,
                "billing_mode": BillingPolicy.current().mode,
            }
        )


class PartnerSettlementsView(APIView):
    """Record a payment against a partner store's dues (`money: manage`), or
    list its settlement history (`money: view`) — the other half of
    `PartnerDuesView`'s Total Owed, offset there rather than double-kept here."""

    permission_classes = [IsAuthenticated, CanManagePartnerSettlements]

    @extend_schema(
        parameters=[OpenApiParameter("store", int)],
        responses={200: PARTNER_SETTLEMENT_LIST_SCHEMA},
    )
    def get(self, request: Request) -> Response:
        qs = PartnerLedgerEntry.objects.select_related("store", "posted_by").order_by(
            "-created_at", "-id"
        )
        store_id = request.query_params.get("store")
        if store_id:
            qs = qs.filter(store_id=store_id)
        return Response(
            {
                "rows": [
                    {
                        "id": e.id,
                        "created_at": e.created_at,
                        "doc_number": e.doc_number,
                        "kind": e.kind,
                        "store_id": e.store_id,
                        "store_code": e.store.code,
                        "store_name": e.store.name,
                        "amount_paise": e.amount,
                        "amount_rupees": paise_to_rupees_str(e.amount),
                        "description": e.description,
                        "reference": e.reference,
                        "mode": e.mode,
                        "posted_by_name": (
                            (e.posted_by.full_name or e.posted_by.username) if e.posted_by else ""
                        ),
                    }
                    for e in qs[:200]
                ]
            }
        )

    @extend_schema(
        request={"application/json": PARTNER_SETTLEMENT_REQUEST},
        responses={201: PARTNER_SETTLEMENT_CREATED_SCHEMA, 400: {"type": "object", "properties": {"detail": {"type": "string"}}}},
    )
    def post(self, request: Request) -> Response:
        store_id: Any = request.data.get("store_id")
        store = Store.objects.filter(pk=store_id, is_partner=True).first()
        if not store:
            return Response(
                {"detail": "store_id is required / must be a partner store."}, status=400
            )
        amount = rupees_to_paise(request.data.get("amount"))
        if amount <= 0:
            return Response({"detail": "A positive amount is required."}, status=400)
        mode = request.data.get("mode", "cash")
        entry = post_partner_settlement(
            store,
            amount,
            request.data.get("description", ""),
            request.user,
            mode=mode,
            # Which cash-ledger bucket (and so, later, whether Bank
            # Reconciliation can even see this row) — derived from `mode`
            # itself rather than trusted from the request, so a "bank"/"upi"
            # payment doesn't silently land under CASH.
            account=account_for_mode(mode),
            reference=request.data.get("reference", ""),
        )
        return Response(
            {
                "id": entry.id,
                "doc_number": entry.doc_number,
                "amount_paise": entry.amount,
                "amount_rupees": paise_to_rupees_str(entry.amount),
            },
            status=201,
        )


class PartnerSettlementReverseView(APIView):
    permission_classes = [IsAuthenticated, CanManagePartnerSettlements]

    @extend_schema(request=None, responses={201: PARTNER_SETTLEMENT_REVERSED_SCHEMA})
    def post(self, request: Request, pk: int) -> Response:
        entry = PartnerLedgerEntry.objects.filter(pk=pk).first()
        if not entry:
            return Response({"detail": "Not found."}, status=404)
        try:
            rev = reverse_partner_settlement(entry, request.user)
        except AlreadyReversedError as exc:
            return Response({"detail": str(exc)}, status=409)
        return Response({"id": rev.id, "doc_number": rev.doc_number}, status=201)


# ---------------------------------------------------------------------------
# Stock requests — the pull side of a transfer (#74)
# ---------------------------------------------------------------------------


#: What a typed term looks through — the voucher number, and either end of the
#: ask by code or by name (mirrors ``TRANSFER_SEARCH_FIELDS``).
STOCK_REQUEST_SEARCH_FIELDS = (
    "doc_number",
    "requesting_store__code",
    "requesting_store__name",
    "fulfilling_store__code",
    "fulfilling_store__name",
)


def _stock_requests(user: Any) -> QuerySet[StockRequest]:
    """Stock requests the caller is an end of (#74), in the read shape."""
    qs = StockRequest.objects.select_related(
        "requesting_store", "fulfilling_store", "created_by"
    ).prefetch_related(
        "lines",
        "fulfilling_transfers__source_store",
        "fulfilling_transfers__destination_store",
        "fulfilling_transfers__lines",
        *APPROVAL_JOINS,
    )
    return scope_stock_requests(qs, user)


class StockRequestListCreateView(generics.ListCreateAPIView[StockRequest]):
    def get_permissions(self) -> list[BasePermission]:
        if self.request.method == "POST":
            return [CanWriteTransfer()]
        return [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[StockRequest]:
        qs = _stock_requests(self.request.user)
        return text_filter(qs, search_term(self.request), STOCK_REQUEST_SEARCH_FIELDS)

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.request.method == "POST":
            return StockRequestWriteSerializer
        return StockRequestReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = StockRequestWriteSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        # A store may only ask on its own behalf — the same rule that gates
        # every other outbound write.
        enforce_store_scope(request.user, ser.validated_data["requesting_store"].id)
        instance = ser.save()
        return Response(
            StockRequestReadSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


class StockRequestDetailView(generics.RetrieveAPIView[StockRequest]):
    permission_classes = [IsAuthenticated]
    serializer_class = StockRequestReadSerializer

    def get_queryset(self) -> QuerySet[StockRequest]:
        return _stock_requests(self.request.user)


class StockRequestFulfilView(APIView):
    """POST: build the draft transfer a request pre-fills.

    Only the fulfilling store may act — the location whose stock this commits.
    The transfer itself is a fresh draft; it still needs the Operations Head
    before dispatch (#137), unchanged.
    """

    permission_classes = [CanWriteTransfer]

    @extend_schema(request=StockRequestFulfilInputSerializer, responses={201: StoreTransferReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            stock_request = StockRequest.objects.select_related(
                "requesting_store", "fulfilling_store"
            ).get(pk=pk)
        except StockRequest.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, stock_request.fulfilling_store_id)

        ser = StockRequestFulfilInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            transfer = fulfil_stock_request(
                stock_request, ser.validated_data["lines"], user=request.user
            )
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(StoreTransferReadSerializer(transfer).data, status=status.HTTP_201_CREATED)


class StockRequestCloseView(APIView):
    """POST: the fulfilling store says no more is coming — the request settles
    as partly fulfilled rather than sitting "being fulfilled" forever."""

    permission_classes = [CanWriteTransfer]

    @extend_schema(request=StockRequestCloseInputSerializer, responses={200: StockRequestReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            stock_request = StockRequest.objects.select_related("fulfilling_store").get(pk=pk)
        except StockRequest.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, stock_request.fulfilling_store_id)

        ser = StockRequestCloseInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            close_stock_request(stock_request, user=request.user, note=ser.validated_data["note"])
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        stock_request = _stock_requests(request.user).get(pk=pk)
        return Response(StockRequestReadSerializer(stock_request).data)


# ---------------------------------------------------------------------------
# Mark damaged (global action → a flag, and on confirmation → quarantine)
# ---------------------------------------------------------------------------


class MarkDamagedView(generics.ListCreateAPIView[MarkDamaged]):
    """GET: list mark-damaged documents — ``?docstatus=0`` for the reports still
    waiting on someone. POST: the global mark-damaged action — create a DMG
    document from scanned pieces.

    Whether that document *posts* depends on who is asking (#138). A store
    person is reporting damage: it stays a draft in the approvals inbox and the
    pieces stay sellable until a warehouse or HO person confirms it. Someone who
    holds the confirming rung reports and confirms in the one call, so the pieces
    move from free-to-sell into quarantine here. ``flag_status`` on the response
    says which happened.

    Any outbound writer (including store-level roles) may mark damaged from any
    stock view — damage is caught everywhere. store_person is read-only.
    """

    def get_permissions(self) -> list[BasePermission]:
        if self.request.method == "POST":
            return [CanWriteReturnToBrand()]
        return [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[MarkDamaged]:
        qs = MarkDamaged.objects.select_related(
            "store", "created_by", "confirmed_by"
        ).prefetch_related("lines", *APPROVAL_JOINS)
        qs = _filter_docstatus(qs, self.request)
        marks: QuerySet[MarkDamaged] = scope_by_store(qs, self.request.user, "store_id")
        return marks

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.request.method == "POST":
            return MarkDamagedInputSerializer
        return MarkDamagedReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = MarkDamagedInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        store = ser.validated_data["store"]
        enforce_store_scope(request.user, store.id)

        try:
            mark = mark_damaged(
                store,
                ser.scans_by_barcode(),
                user=request.user,
                note=ser.validated_data.get("note", ""),
            )
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(MarkDamagedReadSerializer(mark).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# RTV views
# ---------------------------------------------------------------------------


def _rtvs(user: Any) -> QuerySet[ReturnToVendor]:
    """Returns to brand the caller may read, in the read shape.

    By store *or* brand (#75): a return is a store's document and a brand
    manager's decision, and a brand manager is scoped to brands — the store gate
    alone would show them nothing, including their own brands' returns.
    """
    qs = ReturnToVendor.objects.select_related(
        "store", "vendor", "brand", "created_by", "approved_by", "via_transfer", "credit_note"
    ).prefetch_related("lines", *APPROVAL_JOINS)
    scoped: QuerySet[ReturnToVendor] = scope_by_store_or_brand(qs, user, "store_id", "brand__name")
    return scoped


#: Return to Brand (#106) — the return's own number, and who it's going back to.
RTV_SEARCH_FIELDS = ("doc_number", "brand__name", "vendor__name")


class ReturnablePoolView(APIView):
    """GET: what this brand will take back, and how much allowance is left (#75).

    The one place the screen learns which pieces may be scanned onto a return —
    and the same call the create endpoint validates against, so the beep on the
    scanner and the refusal from the server can never disagree.
    """

    permission_classes = [CanReadReturnToBrand]

    @extend_schema(
        parameters=[OpenApiParameter("brand", int, required=True), OpenApiParameter("store", int)],
        responses={200: RETURNABLE_POOL_SCHEMA, 400: ERROR_SCHEMA, 404: ERROR_SCHEMA},
    )
    def get(self, request: Request) -> Response:
        brand_id = request.query_params.get("brand")
        if not brand_id:
            return Response(
                {"error": "Pick a brand first — the pool is a brand's stock."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            brand = Brand.objects.get(pk=brand_id)
        except (Brand.DoesNotExist, ValueError):
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        store_id = request.query_params.get("store")
        try:
            store_pk = int(store_id) if store_id else None
        except ValueError:
            return Response(
                {"error": f"'{store_id}' is not a store."}, status=status.HTTP_400_BAD_REQUEST
            )
        rows = returnable_pool(request.user, brand, store_id=store_pk)
        cap = cap_for(brand)
        return Response(
            {
                "brand": {
                    "id": brand.id,
                    "name": brand.name,
                    "commercial_label": brand.commercial_label,
                    "takes_returns": brand.takes_returns,
                    "return_window_days": brand.return_window_days,
                },
                # Said out loud rather than left as an empty list: "nothing is
                # returnable today" and "nothing from this brand is ever
                # returnable" are different answers, and the screen must not
                # make the reader guess which one it is looking at.
                "excluded_reason": (
                    ""
                    if brand.takes_returns
                    else f"{brand.name} is {brand.commercial_label} — KDPS bought this "
                    "stock outright, so none of it goes back to the brand."
                ),
                "cap": cap.as_json(),
                "rows": [row.as_json() for row in rows],
            }
        )


class RTVListCreateView(generics.ListCreateAPIView[ReturnToVendor]):
    """The returns list, and the scan-built create behind it (#75)."""

    def get_permissions(self) -> list[BasePermission]:
        if self.request.method == "POST":
            # Two gates, both data: the section says who reaches Return to
            # Brand, the stored actor policy says which of them may send stock
            # back rather than only flag it damaged.
            return [CanWriteReturnToBrand(), CanCreateReturnToBrand()]
        return [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[ReturnToVendor]:
        qs = _filter_docstatus(_rtvs(self.request.user), self.request)
        rt = self.request.query_params.get("return_type")
        if rt:
            qs = qs.filter(return_type=rt)
        # The screen's own search box (#102), applied last so it can only
        # narrow what the scope + filters above already allow.
        return text_filter(qs, search_term(self.request), RTV_SEARCH_FIELDS)

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.request.method == "POST":
            return ReturnToBrandCreateSerializer
        return ReturnToVendorReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = ReturnToBrandCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        enforce_store_scope(request.user, data["store"].id)
        try:
            rtv = raise_return_to_brand(
                store=data["store"],
                vendor=data["vendor"],
                brand=data["brand"],
                return_type=data["return_type"],
                route=data["logistics_route"],
                via_transfer=data.get("via_transfer"),
                scans=ser.scans_by_barcode(),
                user=cast("User", request.user),
                notes=data.get("notes", ""),
            )
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            ReturnToVendorReadSerializer(rtv).data,
            status=status.HTTP_201_CREATED,
        )


class RTVDetailView(generics.RetrieveAPIView[ReturnToVendor]):
    permission_classes = [IsAuthenticated]
    serializer_class = ReturnToVendorReadSerializer

    def get_queryset(self) -> QuerySet[ReturnToVendor]:
        return _rtvs(self.request.user)


class RTVSubmitView(APIView):
    """POST: Submit (post) an approved RTV — stock exits its bucket, GL posts."""

    permission_classes = [CanWriteReturnToBrand, CanCreateReturnToBrand]

    @extend_schema(request=None, responses={200: ReturnToVendorReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            rtv = (
                ReturnToVendor.objects.select_related("store", "vendor", "brand")
                .prefetch_related("lines")
                .get(pk=pk)
            )
        except ReturnToVendor.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, rtv.store_id)

        if rtv.docstatus != DocStatus.DRAFT:
            return Response(
                {"error": "Only drafts can be submitted"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            post_rtv(rtv, user=request.user)
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        rtv.refresh_from_db()
        return Response(ReturnToVendorReadSerializer(rtv).data)


class RTVCreditNoteView(APIView):
    """POST: record the brand's credit note against a posted return (#75).

    Status, not money: the payable already moved when the return posted, and
    this is the acknowledgement arriving days or weeks later. It writes the
    companion record, never the document — a posted document is immutable at the
    kernel, which is exactly why the credit note is not a column on it.

    Re-postable, so a mistyped number or the wrong date can be corrected: the
    document underneath stays frozen either way.
    """

    permission_classes = [CanWriteReturnToBrand, CanCreateReturnToBrand]

    @extend_schema(request=CreditNoteSerializer, responses={200: ReturnToVendorReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            rtv = _rtvs(request.user).get(pk=pk)
        except ReturnToVendor.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, rtv.store_id)
        if rtv.docstatus != DocStatus.SUBMITTED:
            return Response(
                {"error": "A credit note answers a return that has actually gone back."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = CreditNoteSerializer(
            getattr(rtv, "credit_note", None), data=request.data, partial=True
        )
        ser.is_valid(raise_exception=True)
        ser.save(rtv=rtv, recorded_by=request.user)
        rtv.refresh_from_db()
        return Response(ReturnToVendorReadSerializer(rtv).data)


# ---------------------------------------------------------------------------
# Stock Adjustment views
# ---------------------------------------------------------------------------


def _adjustments(user: Any) -> QuerySet[StockAdjustment]:
    """Stock adjustments at the caller's own stores (#141), in the read shape."""
    qs = StockAdjustment.objects.select_related(
        "store", "approved_by", "created_by"
    ).prefetch_related("lines", *APPROVAL_JOINS)
    scoped: QuerySet[StockAdjustment] = scope_by_store(qs, user, "store_id")
    return scoped


#: Adjustments (#106) — the document number, the reason code, and the store.
ADJUSTMENT_SEARCH_FIELDS = ("doc_number", "reason", "store__name", "store__code")


class AdjustmentListCreateView(generics.ListCreateAPIView[StockAdjustment]):
    def get_permissions(self) -> list[BasePermission]:
        if self.request.method == "POST":
            return [CanWriteStockCount()]
        return [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[StockAdjustment]:
        qs = _filter_docstatus(_adjustments(self.request.user), self.request)
        # The screen's own search box (#102), applied last.
        return text_filter(qs, search_term(self.request), ADJUSTMENT_SEARCH_FIELDS)

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.request.method == "POST":
            return StockAdjustmentWriteSerializer
        return StockAdjustmentReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = StockAdjustmentWriteSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        enforce_store_scope(request.user, ser.validated_data["store"].id)
        instance = ser.save()
        return Response(
            StockAdjustmentReadSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


class AdjustmentDetailView(generics.RetrieveAPIView[StockAdjustment]):
    permission_classes = [IsAuthenticated]
    serializer_class = StockAdjustmentReadSerializer

    def get_queryset(self) -> QuerySet[StockAdjustment]:
        return _adjustments(self.request.user)


class AdjustmentSubmitView(APIView):
    """POST: Submit (post) a draft stock adjustment."""

    permission_classes = [CanWriteStockCount]

    @extend_schema(request=None, responses={200: StockAdjustmentReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            adj = (
                StockAdjustment.objects.select_related("store").prefetch_related("lines").get(pk=pk)
            )
        except StockAdjustment.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, adj.store_id)

        if adj.docstatus != DocStatus.DRAFT:
            return Response(
                {"error": "Only drafts can be submitted"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            post_adjustment(adj, user=request.user)
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        adj.refresh_from_db()
        return Response(StockAdjustmentReadSerializer(adj).data)


# ---------------------------------------------------------------------------
# Write-off views  (admin-only write)
# ---------------------------------------------------------------------------


def _writeoffs(user: Any) -> QuerySet[WriteOff]:
    """Write-offs at the caller's own stores (#141), in the read shape."""
    qs = WriteOff.objects.select_related("store", "approved_by", "created_by").prefetch_related(
        "lines", *APPROVAL_JOINS
    )
    scoped: QuerySet[WriteOff] = scope_by_store(qs, user, "store_id")
    return scoped


#: Write-offs (#106) — the document number, the reason, and the store.
WRITEOFF_SEARCH_FIELDS = ("doc_number", "reason", "store__name", "store__code")


class WriteOffListCreateView(generics.ListCreateAPIView[WriteOff]):
    def get_permissions(self) -> list[BasePermission]:
        if self.request.method == "POST":
            return [CanWriteStockCount()]
        return [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[WriteOff]:
        qs = _filter_docstatus(_writeoffs(self.request.user), self.request)
        # The screen's own search box (#102), applied last.
        return text_filter(qs, search_term(self.request), WRITEOFF_SEARCH_FIELDS)

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.request.method == "POST":
            return WriteOffWriteSerializer
        return WriteOffReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = WriteOffWriteSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        enforce_store_scope(request.user, ser.validated_data["store"].id)
        instance = ser.save()
        return Response(
            WriteOffReadSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


class WriteOffDetailView(generics.RetrieveAPIView[WriteOff]):
    permission_classes = [IsAuthenticated]
    serializer_class = WriteOffReadSerializer

    def get_queryset(self) -> QuerySet[WriteOff]:
        return _writeoffs(self.request.user)


class WriteOffSubmitView(APIView):
    """POST: Submit (post) a draft write-off."""

    permission_classes = [CanWriteStockCount]

    @extend_schema(request=None, responses={200: WriteOffReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            wo = WriteOff.objects.select_related("store").prefetch_related("lines").get(pk=pk)
        except WriteOff.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, wo.store_id)

        if wo.docstatus != DocStatus.DRAFT:
            return Response(
                {"error": "Only drafts can be submitted"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            post_writeoff(wo, user=request.user)
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        wo.refresh_from_db()
        return Response(WriteOffReadSerializer(wo).data)


# ---------------------------------------------------------------------------
# V-flip views  (admin-only write)
# ---------------------------------------------------------------------------


def _vflips(user: Any) -> QuerySet[VFlip]:
    """V-flips at the caller's own stores (#141), in the read shape.

    Scoped by store like the rest, not by the brand being flipped: the document
    is a change of ownership *at a location*, and its brand names the stock's old
    owner rather than a caller's boundary.
    """
    qs = VFlip.objects.select_related(
        "store", "original_brand", "authorized_by", "created_by"
    ).prefetch_related("lines", *APPROVAL_JOINS)
    scoped: QuerySet[VFlip] = scope_by_store(qs, user, "store_id")
    return scoped


#: V-Flips (#106) — the document number, the brand being flipped, and the store.
VFLIP_SEARCH_FIELDS = ("doc_number", "original_brand__name", "store__name", "store__code")


class VFlipListCreateView(generics.ListCreateAPIView[VFlip]):
    def get_permissions(self) -> list[BasePermission]:
        if self.request.method == "POST":
            return [CanFlipOwnership()]
        return [IsAuthenticated()]

    def get_queryset(self) -> QuerySet[VFlip]:
        qs = _filter_docstatus(_vflips(self.request.user), self.request)
        # The screen's own search box (#102), applied last.
        return text_filter(qs, search_term(self.request), VFLIP_SEARCH_FIELDS)

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.request.method == "POST":
            return VFlipWriteSerializer
        return VFlipReadSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        ser = VFlipWriteSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        enforce_store_scope(request.user, ser.validated_data["store"].id)
        instance = ser.save()
        return Response(
            VFlipReadSerializer(instance).data,
            status=status.HTTP_201_CREATED,
        )


class VFlipDetailView(generics.RetrieveAPIView[VFlip]):
    permission_classes = [IsAuthenticated]
    serializer_class = VFlipReadSerializer

    def get_queryset(self) -> QuerySet[VFlip]:
        return _vflips(self.request.user)


class VFlipSubmitView(APIView):
    """POST: Submit (post) a draft V-flip."""

    permission_classes = [CanExecuteVFlip]

    @extend_schema(request=None, responses={200: VFlipReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            vflip = (
                VFlip.objects.select_related("store", "original_brand")
                .prefetch_related("lines")
                .get(pk=pk)
            )
        except VFlip.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, vflip.store_id)

        if vflip.docstatus != DocStatus.DRAFT:
            return Response(
                {"error": "Only drafts can be submitted"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            post_vflip(vflip, user=request.user)
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        vflip.refresh_from_db()
        return Response(VFlipReadSerializer(vflip).data)


# ---------------------------------------------------------------------------
# Ask again after a rejection (#70)
# ---------------------------------------------------------------------------


class RequestApprovalView(generics.GenericAPIView[Any]):
    """POST: send a rejected draft back for approval.

    One view for every wired family — the only things that differ are which
    model to load, who may ask, and which of the document's own columns names
    the location it answers to, all supplied by the URL conf. The rules (draft
    only, rejected only, and who stays the maker) live in
    ``maker_checker.ask_again``, not here.
    """

    model: Any = None
    read_serializer: Any = None
    #: Joins the read shape needs beyond the approval trail. A transfer has two
    #: ends and no ``store`` column, so this cannot be one fixed list.
    related: tuple[str, ...] = ("store", "created_by")
    #: The column holding the location the caller must be entitled to — the
    #: *source* for a transfer, the store for everything else.
    scope_field: str = "store_id"

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        # Each URL supplies its document's concrete read serializer. Spectacular
        # inspects the bound view instance, so its response matches that family.
        return cast(type[BaseSerializer[Any]], self.read_serializer)

    def _load(self, pk: int) -> Any:
        return (
            self.model.objects.select_related(*self.related)
            .prefetch_related("lines", *APPROVAL_JOINS)
            .get(pk=pk)
        )

    @extend_schema(request=None)
    def post(self, request: Request, pk: int) -> Response:
        try:
            doc = self._load(pk)
        except self.model.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        enforce_store_scope(request.user, getattr(doc, self.scope_field))

        try:
            ask_again(doc, requested_by=request.user)
        except OutboundPostingError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # Re-read rather than refresh: the fresh approval must come back with
        # its people already joined, or the response N+1s on names.
        doc = self._load(pk)
        return Response(self.read_serializer(doc).data)


# ---------------------------------------------------------------------------
# Stock counting — blind sessions, the merged variance, its correction (#76)
# ---------------------------------------------------------------------------


def _stocktakes(user: Any) -> QuerySet[Stocktake]:
    """Counts this user may see — scoped at the queryset, fail-closed (ADR-0003).

    Scope belongs here rather than on each view because a count carries per-line
    cost and value: a store-scoped person reading another store's variance would
    read that location's book cost, which is the one thing #76's "value is shown
    to the person counting **their own** location" rule withholds. Out of scope
    is therefore indistinguishable from not existing.
    """
    qs = Stocktake.objects.select_related("store", "opened_by", "adjustment").prefetch_related(
        "sessions__lines", "sessions__counted_by"
    )
    scoped: QuerySet[Stocktake] = scope_by_store(qs, user, "store_id")
    return scoped


def _load_stocktake(pk: int, user: Any) -> Stocktake:
    return _stocktakes(user).get(pk=pk)


class StocktakeListCreateView(APIView):
    """GET: counts at the stores this user can see. POST: open a new one."""

    def get_permissions(self) -> list[BasePermission]:
        return [CanWriteStockCount()] if self.request.method == "POST" else [IsAuthenticated()]

    @extend_schema(responses={200: StocktakeReadSerializer(many=True)})
    def get(self, request: Request) -> Response:
        return Response(StocktakeReadSerializer(_stocktakes(request.user), many=True).data)

    @extend_schema(request=StocktakeCreateSerializer, responses={201: StocktakeReadSerializer})
    def post(self, request: Request) -> Response:
        ser = StocktakeCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        store = ser.validated_data["store"]
        enforce_store_scope(request.user, store.id)
        stocktake = open_stocktake(
            store, user=cast("User", request.user), note=ser.validated_data["note"]
        )
        return Response(
            StocktakeReadSerializer(_load_stocktake(stocktake.pk, request.user)).data,
            status=status.HTTP_201_CREATED,
        )


class StocktakeDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: StocktakeReadSerializer})
    def get(self, request: Request, pk: int) -> Response:
        try:
            return Response(StocktakeReadSerializer(_load_stocktake(pk, request.user)).data)
        except Stocktake.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)


class CountSessionCreateView(APIView):
    """POST: add one counter's scoped pass to an open count."""

    permission_classes = [CanWriteStockCount]

    @extend_schema(request=CountSessionCreateSerializer, responses={201: CountSessionReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            stocktake = Stocktake.objects.select_related("store").get(pk=pk)
        except Stocktake.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        enforce_store_scope(request.user, stocktake.store_id)

        ser = CountSessionCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            session = open_session(
                stocktake,
                scope=ser.validated_data["scope"],
                scope_value=ser.validated_data["scope_value"],
                user=cast("User", request.user),
            )
        except CountError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CountSessionReadSerializer(session).data, status=status.HTTP_201_CREATED)


class CountLookupView(APIView):
    """GET ?store=&barcode= — what a scanned piece *is*, during a blind count.

    Deliberately not ``ScanLookupView``: that one answers with the location's
    available quantity, which is the very number a blind count may not show, and
    it 404s on a piece the books hold none of — which in a count is not a wrong
    piece at all but the surplus the count exists to find. So this returns dims
    only, and finds the piece in the SKU master when the location holds none.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[OpenApiParameter("store", int, required=True), OpenApiParameter("barcode", str, required=True)],
        responses={200: COUNT_LOOKUP_SCHEMA, 400: ERROR_SCHEMA, 404: ERROR_SCHEMA},
    )
    def get(self, request: Request) -> Response:
        from outbound.counting import identity_dims

        store_id = request.query_params.get("store")
        barcode = (request.query_params.get("barcode") or "").strip()
        if not store_id or not barcode:
            return Response(
                {"error": "Pass store= and barcode="}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            enforce_store_scope(request.user, int(store_id))
        except ValueError:
            return Response({"error": "store must be an id"}, status=status.HTTP_400_BAD_REQUEST)

        dims = identity_dims(int(store_id), barcode)
        if not any(dims.values()):
            return Response(
                {"error": f"{barcode} is not a piece this system knows."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"barcode": barcode, **dims})


class CountSessionScanView(APIView):
    """POST: record scanned pieces on an open session.

    The response is the session as it stands — counted pieces only. No book
    quantity exists to return yet, which is what makes the count blind (#76).
    """

    permission_classes = [CanWriteStockCount]

    @extend_schema(request=CountScanInputSerializer, responses={200: CountSessionReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        session = _load_session(pk)
        if session is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        enforce_store_scope(request.user, session.stocktake.store_id)

        ser = CountScanInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            record_scans(session, ser.scans_by_barcode())
        except CountError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CountSessionReadSerializer(_load_session(pk)).data)


class CountSessionSubmitView(APIView):
    """POST: close a session and take its book snapshot."""

    permission_classes = [CanWriteStockCount]

    @extend_schema(request=None, responses={200: CountSessionReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        session = _load_session(pk)
        if session is None:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        enforce_store_scope(request.user, session.stocktake.store_id)

        try:
            submit_session(session)
        except CountError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CountSessionReadSerializer(_load_session(pk)).data)


def _load_session(pk: int) -> CountSession | None:
    return (
        CountSession.objects.select_related("stocktake__store", "counted_by")
        .prefetch_related("lines")
        .filter(pk=pk)
        .first()
    )


def _variance_payload(stocktake: Stocktake, user: Any) -> dict[str, Any]:
    """The merged variance as the screen reads it.

    Shared by the report and the recount, because a recount *changes* the
    variance: returning anything less would leave the screen showing one half of
    a number that has moved, and a second round-trip to fetch the other.
    """
    lines = [v.as_dict(for_user=user) for v in variance_report(stocktake)]
    return {
        "stocktake": stocktake.pk,
        "store_code": stocktake.store.code,
        "status": stocktake.status,
        "recount_tolerance_paise": recount_tolerance(),
        "lines": lines,
        "net_pieces": sum(v["adj_qty"] for v in lines),
        "net_variance_paise": sum(v["variance_paise"] for v in lines),
        "unpriced": [v["sku_code"] for v in lines if not v["cost_known"] and v["adj_qty"]],
        "awaiting_recount": [v["sku_code"] for v in lines if v["needs_recount"]],
    }


class StocktakeVarianceView(APIView):
    """GET: book against counted for the whole count, in pieces and in value.

    Value is not masked. Whoever counted is counting their own location's stock
    and can already read the PT that carries the rate, so withholding the number
    only stops them sizing their own problem (#76).
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: VARIANCE_SCHEMA, 404: ERROR_SCHEMA})
    def get(self, request: Request, pk: int) -> Response:
        try:
            stocktake = _load_stocktake(pk, request.user)
        except Stocktake.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(_variance_payload(stocktake, request.user))


class StocktakeRecountView(APIView):
    """POST: a second person's count of one piece, and why it is out (#78).

    403 rather than 400 when the caller counted the piece themselves: the request
    is well formed and this person may never make it, whatever the tolerance and
    the bands are retuned to.
    """

    permission_classes = [CanWriteStockCount]

    @extend_schema(request=RecountInputSerializer, responses={200: VARIANCE_SCHEMA})
    def post(self, request: Request, pk: int) -> Response:
        try:
            stocktake = _load_stocktake(pk, request.user)
        except Stocktake.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        enforce_store_scope(request.user, stocktake.store_id)

        ser = RecountInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            record_recount(
                stocktake,
                sku_code=ser.validated_data["sku_code"],
                counted_qty=ser.validated_data["counted_qty"],
                reason=ser.validated_data["reason"],
                user=cast("User", request.user),
            )
        except SameCounterError as e:
            return Response({"error": str(e)}, status=status.HTTP_403_FORBIDDEN)
        except (CountError, OutboundPostingError) as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_variance_payload(stocktake, request.user))


class StocktakeApplyView(APIView):
    """POST: apply the variance as one stock adjustment.

    Two 409s, both meaning "not refused forever, refused until a named person
    does a named thing": stock moved between the count and now (confirm those
    barcodes and post again), or a difference is too big for one person's count
    (a second person recounts it). Never a blind overwrite, and never a big
    variance on one pair of eyes.
    """

    permission_classes = [CanWriteStockCount]

    @extend_schema(request=ApplyVarianceInputSerializer, responses={201: StockAdjustmentReadSerializer})
    def post(self, request: Request, pk: int) -> Response:
        try:
            stocktake = _load_stocktake(pk, request.user)
        except Stocktake.DoesNotExist:
            return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        enforce_store_scope(request.user, stocktake.store_id)

        ser = ApplyVarianceInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            adjustment = apply_variance(
                stocktake,
                user=cast("User", request.user),
                confirm_skus=frozenset(ser.validated_data["confirm"]),
            )
        except RecountRequiredError as e:
            return Response(
                {
                    "error": str(e),
                    "needs_recount": [v.as_dict(for_user=request.user) for v in e.lines],
                },
                status=status.HTTP_409_CONFLICT,
            )
        except MovedMidCountError as e:
            return Response(
                {"error": str(e), "moved": [v.as_dict(for_user=request.user) for v in e.lines]},
                status=status.HTTP_409_CONFLICT,
            )
        except (CountError, OutboundPostingError) as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            StockAdjustmentReadSerializer(adjustment).data, status=status.HTTP_201_CREATED
        )
