"""Outbound serializers — DRF read/write shapes for outbound documents."""

from __future__ import annotations

from typing import Any, TypeVar

from django.db import transaction
from django.db.models import Model
from django.utils import timezone
from rest_framework import serializers

from approvals.models import ApprovalStatus
from approvals.serializers import ApprovalReadSerializer
from approvals.services import display_name
from core.documents import DocStatus
from masters.models import Brand, Store
from outbound.maker_checker import request_document_approval
from outbound.models import (
    AdjustmentReason,
    BillingPolicy,
    CountScope,
    CountSession,
    CountSessionLine,
    GapReason,
    LogisticsRoute,
    MarkDamaged,
    MarkDamagedLine,
    ReturnCreditNote,
    ReturnToVendor,
    ReturnToVendorLine,
    ReturnType,
    StockAdjustment,
    StockAdjustmentLine,
    StockRequest,
    StockRequestLine,
    Stocktake,
    StoreTransfer,
    StoreTransferLine,
    TransferGapClosure,
    TransferGapClosureLine,
    TransferPT,
    TransferReceipt,
    TransferReceiptException,
    VFlip,
    VFlipLine,
    WriteOff,
    WriteOffLine,
)
from outbound.transfer_pt import KDPS_COLUMNS
from vendors.models import Vendor

# ---------------------------------------------------------------------------
# Maker-checker read shape (#70)
# ---------------------------------------------------------------------------

#: The document a read shape is *of* — every maker-checker family shares the base
#: below and names its own model, so the shape stays one class without losing
#: which document each subclass serialises.
_ApprovedT = TypeVar("_ApprovedT", bound=Model)


class ApprovedDocumentSerializer(serializers.ModelSerializer[_ApprovedT]):
    """Base read shape for a document that needs a second person.

    Every such document answers the same three questions on its own page, for
    good: **made by** whom, **approved by** whom, and **when** — plus the live
    approval record (pending / approved / rejected, with the reject reason).

    The approver is read from the approval, not from the document's own column:
    the column is a denormalised copy stamped at post time (for Tally), so on a
    still-unposted draft only the approval knows the answer.
    """

    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    approval = serializers.SerializerMethodField()
    approval_history = serializers.SerializerMethodField()

    def _approval(self, obj: Any) -> Any:
        """The live one. A document that was rejected and asked again holds
        several, newest first — the newest is the one in force."""
        return next(iter(obj.approvals.all()), None)

    def get_created_by_name(self, obj: Any) -> str:
        return display_name(obj.created_by)

    def get_approved_by_name(self, obj: Any) -> str:
        approval = self._approval(obj)
        if approval is None or approval.status != ApprovalStatus.APPROVED:
            return ""
        return display_name(approval.decided_by)

    def get_approval(self, obj: Any) -> dict[str, Any] | None:
        approval = self._approval(obj)
        return ApprovalReadSerializer(approval).data if approval else None

    def get_approval_history(self, obj: Any) -> list[dict[str, Any]]:
        """Everything asked before the live one — the rejections a maker has
        already worked through. Empty for the common case."""
        return [ApprovalReadSerializer(a).data for a in list(obj.approvals.all())[1:]]


def _priced_lines(validated_data: Any) -> list[dict[str, Any]]:
    """Take the lines off a validated create payload, priced from the books (#103).

    A draft is *born* with its cost of record, so the approval band, the posting
    engine and the screen all read the same number — and a line the books cannot
    price never becomes a document at all, rather than posting free stock later.
    The refusal is a 400 on the create call, not a 500: an unpriceable piece is
    something the maker has to fix, and the message says how.

    Takes the whole payload rather than its parts because every caller needs the
    same three (store, lines, the document's season) and the lines must come
    *off* it before the document is created from what is left.
    """
    from outbound.posting import OutboundPostingError, resolve_line_identity

    store = validated_data["store"]
    doc_season = validated_data.get("season", "")
    priced = []
    for ld in validated_data.pop("lines"):
        try:
            identity = resolve_line_identity(
                store.id, ld["sku_code"], ld.get("season") or doc_season
            )
        except OutboundPostingError as exc:
            raise serializers.ValidationError({"lines": [str(exc)]}) from exc
        priced.append({**ld, **identity})
    return priced


def _create_with_approval(
    model: Any, line_model: Any, line_field: str, validated_data: Any, request: Any
) -> Any:
    """Create a draft + its lines, then put it in the approvals inbox.

    One transaction: a draft that needs a checker is never left without one.
    """
    lines_data = _priced_lines(validated_data)
    user = getattr(request, "user", None)
    validated_data["created_by"] = user
    with transaction.atomic():
        doc = model.objects.create(**validated_data)
        for ld in lines_data:
            line_model.objects.create(**{line_field: doc, **ld})
        request_document_approval(doc, requested_by=user)
    return doc


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------


class StoreTransferLineSerializer(serializers.ModelSerializer[StoreTransferLine]):
    """Read shape. Quantities are scan-derived: ``qty_planned`` is the plan,
    ``qty_dispatched``/``qty_received`` are what was scanned, ``qty_resolved``
    is what a posted gap closure accounted for, and ``qty_in_transit`` is derived
    from the three, never stored."""

    qty_in_transit = serializers.IntegerField(read_only=True)

    class Meta:
        model = StoreTransferLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty_planned",
            "qty_dispatched",
            "qty_received",
            "qty_resolved",
            "qty_in_transit",
            "unit_cost_paise",
        ]
        read_only_fields = [
            "id",
            "qty_dispatched",
            "qty_received",
            "qty_resolved",
            "unit_cost_paise",
        ]


class ReceiptExceptionSerializer(serializers.ModelSerializer[TransferReceiptException]):
    """One short / extra / damaged outcome, as recorded at receive (#71)."""

    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = TransferReceiptException
        fields = [
            "id",
            "kind",
            "kind_label",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "unit_cost_paise",
            "note",
        ]
        read_only_fields = fields


class TransferReceiptSerializer(serializers.ModelSerializer[TransferReceipt]):
    exceptions = ReceiptExceptionSerializer(many=True, read_only=True)
    received_by_name = serializers.SerializerMethodField()

    def get_received_by_name(self, obj: TransferReceipt) -> str:
        return display_name(obj.received_by)

    class Meta:
        model = TransferReceipt
        fields = [
            "id",
            "received_by",
            "received_by_name",
            "receipt_date",
            "receipt_status",
            "shortfall_notes",
            "exceptions",
        ]
        read_only_fields = ["id", "receipt_date"]


class GapClosureLineSerializer(serializers.ModelSerializer[TransferGapClosureLine]):
    class Meta:
        model = TransferGapClosureLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "unit_cost_paise",
        ]
        read_only_fields = fields


class GapClosureReadSerializer(ApprovedDocumentSerializer[TransferGapClosure]):
    """A gap closure and everything a reviewer needs to judge it — which
    transfer, which pieces, whose reason, and who signed it off."""

    lines = GapClosureLineSerializer(many=True, read_only=True)
    reason_label = serializers.CharField(source="get_reason_display", read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    transfer_doc_number = serializers.CharField(source="transfer.doc_number", read_only=True)
    source_store_code = serializers.CharField(source="transfer.source_store.code", read_only=True)
    destination_store_code = serializers.CharField(
        source="transfer.destination_store.code", read_only=True
    )

    class Meta:
        model = TransferGapClosure
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "transfer",
            "transfer_doc_number",
            "store",
            "store_code",
            "source_store_code",
            "destination_store_code",
            "reason",
            "reason_label",
            "note",
            "approved_by",
            "approved_by_name",
            "approval",
            "approval_history",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
            "lines",
        ]


class GapClosureInputSerializer(serializers.Serializer[dict[str, Any]]):
    """Raising a closure: a reason and (optionally) a sentence. Nothing else —
    the lines are read off the transfer's in-transit remainder, so the person
    closing the gap cannot quietly change how much went missing."""

    reason = serializers.ChoiceField(choices=GapReason.choices)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")


class TransferPTSerializer(serializers.ModelSerializer[TransferPT]):
    """The transfer's PT as the print screen needs it (#72).

    Read-only throughout: the rows are a frozen copy of what the scanned lines
    generated, and the columns come from the KDPS format itself rather than from
    the rows, so a PT with no lines still prints with its proper header.
    """

    columns = serializers.SerializerMethodField()
    doc_number = serializers.CharField(source="transfer.doc_number", read_only=True)
    source_store_code = serializers.CharField(source="transfer.source_store.code", read_only=True)
    source_store_name = serializers.CharField(source="transfer.source_store.name", read_only=True)
    destination_store_code = serializers.CharField(
        source="transfer.destination_store.code", read_only=True
    )
    destination_store_name = serializers.CharField(
        source="transfer.destination_store.name", read_only=True
    )
    dispatch_date = serializers.DateTimeField(source="transfer.dispatch_date", read_only=True)
    generated_by_name = serializers.SerializerMethodField()

    def get_columns(self, obj: TransferPT) -> list[str]:
        return list(KDPS_COLUMNS)

    def get_generated_by_name(self, obj: TransferPT) -> str:
        return display_name(obj.generated_by)

    class Meta:
        model = TransferPT
        fields = [
            "id",
            "transfer",
            "doc_number",
            "source_store_code",
            "source_store_name",
            "destination_store_code",
            "destination_store_name",
            "dispatch_date",
            "generated_at",
            "generated_by",
            "generated_by_name",
            "columns",
            "rows",
        ]
        read_only_fields = fields


class StoreTransferReadSerializer(ApprovedDocumentSerializer[StoreTransfer]):
    lines = StoreTransferLineSerializer(many=True, read_only=True)
    receipt = TransferReceiptSerializer(read_only=True)
    gap_closure = GapClosureReadSerializer(read_only=True)
    source_store_code = serializers.CharField(source="source_store.code", read_only=True)
    source_store_name = serializers.CharField(source="source_store.name", read_only=True)
    destination_store_code = serializers.CharField(source="destination_store.code", read_only=True)
    destination_store_name = serializers.CharField(source="destination_store.name", read_only=True)
    dispatch_mismatch = serializers.SerializerMethodField()
    qty_in_transit = serializers.SerializerMethodField()
    gap_state = serializers.SerializerMethodField()
    pt_generated_at = serializers.SerializerMethodField()
    dispatched_by_name = serializers.SerializerMethodField()

    def get_dispatched_by_name(self, obj: StoreTransfer) -> str:
        """Who actually sent the pieces. Distinct from ``dispatcher_name``,
        which is the free-text person carrying the carton — the trail wants the
        user who pressed the button (#137)."""

        return display_name(obj.dispatched_by)

    def get_pt_generated_at(self, obj: StoreTransfer) -> str | None:
        """When this transfer's PT was cut — the screen's cue that there is a
        document to print. A draft has none: nothing has been scanned (#72)."""

        pt = getattr(obj, "pt", None)
        return pt.generated_at.isoformat() if pt else None

    def get_dispatch_mismatch(self, obj: StoreTransfer) -> bool:
        """Derived, never stored: a dispatched transfer whose scanned
        quantities differ from its plan (Rule 5 — flagged, not blocked)."""

        if obj.docstatus == DocStatus.DRAFT:  # nothing scanned yet
            return False
        return any(
            line.qty_planned is not None and line.qty_planned != line.qty_dispatched
            for line in obj.lines.all()
        )

    def get_qty_in_transit(self, obj: StoreTransfer) -> int:
        """Pieces still on the road (or missing) under this transfer."""

        if obj.docstatus != DocStatus.SUBMITTED:
            return 0
        return sum(line.qty_in_transit for line in obj.lines.all())

    def get_gap_state(self, obj: StoreTransfer) -> str:
        """Where this transfer stands, derived from its documents — never stored
        (the architecture's rule: a lifecycle is read off the record, not set).

        ``in_transit`` nothing received yet · ``received`` all of it landed ·
        ``gap`` received short, still open · ``closed`` a senior said what became
        of the shortfall.
        """

        if obj.docstatus != DocStatus.SUBMITTED:
            return ""
        receipt = getattr(obj, "receipt", None)
        if receipt is None:
            return "in_transit"
        closure = getattr(obj, "gap_closure", None)
        if closure is not None and closure.docstatus == DocStatus.SUBMITTED:
            return "closed"
        return "gap" if self.get_qty_in_transit(obj) > 0 else "received"

    class Meta:
        model = StoreTransfer
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "transfer_type",
            "is_cross_state",
            "source_store",
            "source_store_code",
            "source_store_name",
            "destination_store",
            "destination_store_code",
            "destination_store_name",
            "reason",
            "transport_mode",
            "transport_ref",
            "dispatcher_name",
            "expected_arrival_note",
            "eway_bill_number",
            "dispatch_date",
            "dispatched_by",
            "dispatched_by_name",
            "created_by",
            "created_by_name",
            "approved_by",
            "approved_by_name",
            "approval",
            "approval_history",
            "created_at",
            "updated_at",
            "dispatch_mismatch",
            "qty_in_transit",
            "gap_state",
            "pt_generated_at",
            "lines",
            "receipt",
            "gap_closure",
            "partner_billing_value_paise",
        ]


class StoreTransferPlanLineSerializer(serializers.ModelSerializer[StoreTransferLine]):
    """Write shape for a draft's *plan* line. Only the plan quantity is
    accepted — dispatched/received quantities come from scanning, never typing
    (#68). Dims and cost are enriched from the source stock at dispatch."""

    qty_planned = serializers.IntegerField(min_value=1)

    class Meta:
        model = StoreTransferLine
        fields = ["sku_code", "qty_planned"]


class BillingPolicySerializer(serializers.ModelSerializer[BillingPolicy]):
    """The one partner-billing dial, read and written whole — mirrors
    `sell.SellPolicyView`'s shape (#271's pattern, applied here)."""

    mode_label = serializers.CharField(source="get_mode_display", read_only=True)
    set_by_name = serializers.SerializerMethodField()

    def get_set_by_name(self, obj: BillingPolicy) -> str:
        return display_name(obj.set_by)

    class Meta:
        model = BillingPolicy
        fields = ["mode", "mode_label", "set_by_name", "updated_at"]


class StoreTransferWriteSerializer(serializers.ModelSerializer[StoreTransfer]):
    """Creates a draft transfer. ``lines`` (the plan) is optional — a
    store→store transfer builds its lines by scanning at dispatch."""

    lines = StoreTransferPlanLineSerializer(many=True, required=False)

    class Meta:
        model = StoreTransfer
        fields = [
            "source_store",
            "destination_store",
            "transfer_type",
            "reason",
            "transport_mode",
            "transport_ref",
            "dispatcher_name",
            "expected_arrival_note",
            "eway_bill_number",
            "lines",
        ]

    def validate(self, data: Any) -> Any:
        src = data.get("source_store")
        dst = data.get("destination_store")
        if src and dst and src == dst:
            raise serializers.ValidationError("Source and destination must differ.")
        # Bihar ↔ Jharkhand is a supply between two distinct persons, so the
        # carton cannot legally travel without an e-way bill. The screen has
        # always said so; saying it here too means the API refuses as well, and
        # a transfer created any other way cannot skip the number (#137).
        if (
            StoreTransfer.crosses_a_state_line(src, dst)
            and not (data.get("eway_bill_number") or "").strip()
        ):
            raise serializers.ValidationError(
                {"eway_bill_number": "E-way bill number is required for cross-state transfers."}
            )
        return data

    def create(self, validated_data: Any) -> StoreTransfer:
        """Create the draft and put it straight in the Operations Head's inbox.

        One transaction, at creation time, exactly as the other maker-checker
        families do: a transfer is *born* waiting, so a maker can neither forget
        to ask nor dispatch in the gap before asking (#137).

        Not ``_create_with_approval``, which prices every line onto the draft as
        it is made: a transfer's lines deliberately carry no cost until the
        pieces are scanned out (#68), so freezing one here would be inventing a
        number the dispatch is then free to contradict. The plan is stored bare
        and sized for the inbox off the source store's books at the moment the
        request is raised; a plan-less scan-to-build draft sizes to nothing,
        which the policy reads as "unknown" and escalates.
        """
        lines_data = validated_data.pop("lines", [])
        request = self.context.get("request", None)
        user = request.user if request else None
        validated_data["created_by"] = user
        with transaction.atomic():
            transfer = StoreTransfer.objects.create(**validated_data)
            for ld in lines_data:
                StoreTransferLine.objects.create(transfer=transfer, **ld)
            request_document_approval(transfer, requested_by=user)
        return transfer


class ScanLineSerializer(serializers.Serializer[dict[str, Any]]):
    """One scanned (barcode × count) pair from the scan screen."""

    barcode = serializers.CharField(max_length=64)
    qty = serializers.IntegerField(min_value=1)


def _totals(scans: list[dict[str, Any]] | None) -> dict[str, int]:
    """Aggregate a scan list into barcode → total qty (a barcode may repeat)."""
    totals: dict[str, int] = {}
    for scan in scans or []:
        totals[scan["barcode"]] = totals.get(scan["barcode"], 0) + scan["qty"]
    return totals


class TransferScanInputSerializer(serializers.Serializer[dict[str, Any]]):
    """For dispatch: the scanned lines are the only quantities."""

    scans = ScanLineSerializer(many=True, allow_empty=False)

    def scans_by_barcode(self) -> dict[str, int]:
        return _totals(self.validated_data["scans"])


class TransferReceiveInputSerializer(serializers.Serializer[dict[str, Any]]):
    """Receiving, with the three exceptions the carton actually produces (#71).

    ``scans`` are the pieces that arrived intact, ``damaged`` the ones that
    arrived broken (a damage document is raised for them — quarantine if the
    receiver may confirm it, otherwise a flag, #138), ``extras`` the ones that
    arrived without being sent. ``notes`` is the receiver's sentence about the
    shortfall — the field the old payload dropped.

    Any one bucket may be empty but not all three: a receipt records something
    turning up. Which barcodes belong in which bucket is the server's call, in
    ``posting._validate_receive``.
    """

    scans = ScanLineSerializer(many=True, required=False, default=list)
    damaged = ScanLineSerializer(many=True, required=False, default=list)
    extras = ScanLineSerializer(many=True, required=False, default=list)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")

    def validate(self, data: Any) -> Any:
        if not (data.get("scans") or data.get("damaged") or data.get("extras")):
            raise serializers.ValidationError(
                "Scan at least one piece — as received, damaged, or an extra."
            )
        return data

    def scans_by_barcode(self) -> dict[str, int]:
        return _totals(self.validated_data["scans"])

    def damaged_by_barcode(self) -> dict[str, int]:
        return _totals(self.validated_data["damaged"])

    def extras_by_barcode(self) -> dict[str, int]:
        return _totals(self.validated_data["extras"])


# ---------------------------------------------------------------------------
# Mark damaged (global action → quarantine)
# ---------------------------------------------------------------------------


class MarkDamagedLineSerializer(serializers.ModelSerializer[MarkDamagedLine]):
    class Meta:
        model = MarkDamagedLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "unit_cost_paise",
        ]
        read_only_fields = fields


class MarkDamagedReadSerializer(ApprovedDocumentSerializer[MarkDamaged]):
    """A damage report and where it has got to (#138).

    The maker-checker half — who reported it, who confirmed it, the live
    approval and its reason — is the base's, read off one prefetch. ``flag_status``
    is the word only this family needs: the store that reported the damage has
    to see its report is still waiting, and the person who can confirm it needs
    the same row to say what they are being asked.
    """

    lines = MarkDamagedLineSerializer(many=True, read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    confirmed_by_name = serializers.SerializerMethodField()
    flag_status = serializers.SerializerMethodField()

    def get_confirmed_by_name(self, obj: MarkDamaged) -> str:
        return display_name(obj.confirmed_by)

    def get_flag_status(self, obj: MarkDamaged) -> str:
        """Confirmed once it has posted; otherwise whatever the inbox says."""
        if obj.docstatus == DocStatus.SUBMITTED:
            return "confirmed"
        approval = self._approval(obj)
        if approval is not None and approval.status == ApprovalStatus.REJECTED:
            return "rejected"
        return "flagged"

    class Meta:
        model = MarkDamaged
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "flag_status",
            "store",
            "store_code",
            "store_name",
            "note",
            "created_by",
            "created_by_name",
            "confirmed_by",
            "confirmed_by_name",
            "approved_by_name",
            "approval",
            "approval_history",
            "created_at",
            "lines",
        ]


class MarkDamagedInputSerializer(serializers.Serializer[dict[str, Any]]):
    """The global mark-damaged action: a store + scanned pieces (+ optional
    note). Quantities are scanned, dims/cost enriched from the store's stock —
    never typed."""

    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    scans = ScanLineSerializer(many=True, allow_empty=False)
    note = serializers.CharField(max_length=240, required=False, allow_blank=True, default="")

    def scans_by_barcode(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for scan in self.validated_data["scans"]:
            totals[scan["barcode"]] = totals.get(scan["barcode"], 0) + scan["qty"]
        return totals


# ---------------------------------------------------------------------------
# RTV
# ---------------------------------------------------------------------------


class ReturnToVendorLineSerializer(serializers.ModelSerializer[ReturnToVendorLine]):
    """Read-only, whole. A return line is built by the server from the scanned
    barcode and the pool row it matched (#75): the source bucket decides which
    ledger the posting drains, and the unit cost comes from the books, never from
    the payload (#103) — so neither is a client's to send."""

    class Meta:
        model = ReturnToVendorLine
        fields = [
            "id",
            "source",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "unit_cost_paise",
        ]
        read_only_fields = fields


class ReturnToVendorReadSerializer(ApprovedDocumentSerializer[ReturnToVendor]):
    lines = ReturnToVendorLineSerializer(many=True, read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    brand_name = serializers.CharField(source="brand.name", read_only=True, default="")
    commercial_label = serializers.CharField(
        source="brand.commercial_label", read_only=True, default=""
    )
    logistics_route_label = serializers.CharField(
        source="get_logistics_route_display", read_only=True, default=""
    )
    via_transfer_number = serializers.CharField(
        source="via_transfer.doc_number", read_only=True, default=""
    )
    credit_note = serializers.SerializerMethodField()
    value_paise = serializers.SerializerMethodField()
    days_to_window = serializers.SerializerMethodField()

    def get_value_paise(self, obj: Any) -> int:
        """What this return is worth at the cost frozen on its lines — the number
        the allowance was measured against, so it is derived from the same place
        rather than recomputed from today's books."""
        return sum(line.qty * line.unit_cost_paise for line in obj.lines.all())

    def get_credit_note(self, obj: Any) -> dict[str, Any] | None:
        """The brand's acknowledgement, if it has arrived. None until it does —
        an absent credit note is a fact the returns list is chased on."""
        note = getattr(obj, "credit_note", None)
        return CreditNoteSerializer(note).data if note else None

    def get_days_to_window(self, obj: Any) -> int | None:
        """The countdown, as of now. None when no deadline applies — a defect
        claim has none, and neither does a brand with no negotiated window."""
        if obj.return_window_date is None:
            return None
        days: int = (obj.return_window_date - timezone.localdate()).days
        return days

    class Meta:
        model = ReturnToVendor
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "store",
            "store_code",
            "store_name",
            "vendor",
            "brand",
            "brand_name",
            "commercial_label",
            "return_type",
            "logistics_route",
            "logistics_route_label",
            "via_transfer",
            "via_transfer_number",
            "season",
            "return_window_date",
            "days_to_window",
            "value_paise",
            "cap_percent",
            "cap_allowance_paise",
            "cap_used_before_paise",
            "cap_exceeded_by_paise",
            "credit_note",
            "notes",
            "created_by",
            "created_by_name",
            "approved_by",
            "approved_by_name",
            "approval",
            "approval_history",
            "created_at",
            "updated_at",
            "lines",
        ]


class ReturnToBrandCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """The scan payload behind a new return (#75).

    Barcodes and quantities, and nothing else that decides money. Which bucket
    each piece comes out of, what it is worth and whether the brand will take it
    at all are all the server's answers, read off the returnable pool.
    """

    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    vendor = serializers.PrimaryKeyRelatedField(queryset=Vendor.objects.all())
    brand = serializers.PrimaryKeyRelatedField(queryset=Brand.objects.all())
    return_type = serializers.ChoiceField(choices=ReturnType.choices)
    logistics_route = serializers.ChoiceField(choices=LogisticsRoute.choices)
    via_transfer = serializers.PrimaryKeyRelatedField(
        queryset=StoreTransfer.objects.all(), required=False, allow_null=True
    )
    scans = ScanLineSerializer(many=True, allow_empty=False)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def scans_by_barcode(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for scan in self.validated_data["scans"]:
            totals[scan["barcode"]] = totals.get(scan["barcode"], 0) + scan["qty"]
        return totals


class CreditNoteSerializer(serializers.ModelSerializer[ReturnCreditNote]):
    """Recording the brand's acknowledgement against a posted return.

    Status, not money: the payable already moved when the return posted. Written
    to the companion record rather than the document, because a posted document
    is immutable and a credit note arrives after the fact.
    """

    recorded_by_name = serializers.SerializerMethodField()

    def get_recorded_by_name(self, obj: ReturnCreditNote) -> str:
        return display_name(obj.recorded_by)

    class Meta:
        model = ReturnCreditNote
        fields = ["id", "received_on", "reference", "recorded_by_name"]
        read_only_fields = ["id", "recorded_by_name"]


# ---------------------------------------------------------------------------
# Stock Adjustment
# ---------------------------------------------------------------------------


class StockAdjustmentLineSerializer(serializers.ModelSerializer[StockAdjustmentLine]):
    """``unit_cost_paise`` is read-only: a money posting reads its cost from the
    books, never from the payload (#103) — same rule as a transfer line.

    So are ``book_qty`` and ``adj_qty`` (#76). The variance is the *books*
    against what was counted, and only the count comes from outside: a client
    that could send the book number, or the subtraction, could post a correction
    the books never agreed to. Send ``counted_qty``; the server does the rest.

    ``reason`` is read-only for the same family of reasons (#78): where a line
    carries one, it came from a recount by a second person, and a typed
    adjustment says why on the document rather than piece by piece.
    """

    class Meta:
        model = StockAdjustmentLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "book_qty",
            "counted_qty",
            "adj_qty",
            "unit_cost_paise",
            "reason",
        ]
        read_only_fields = ["id", "book_qty", "adj_qty", "unit_cost_paise", "reason"]


class StockAdjustmentReadSerializer(ApprovedDocumentSerializer[StockAdjustment]):
    lines = StockAdjustmentLineSerializer(many=True, read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)

    class Meta:
        model = StockAdjustment
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "store",
            "store_code",
            "store_name",
            "reason",
            "approved_by",
            "approved_by_name",
            "approval",
            "approval_history",
            "notes",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
            "lines",
        ]


class StockAdjustmentWriteSerializer(serializers.ModelSerializer[StockAdjustment]):
    """``approved_by`` is not accepted: the approver is stamped by whoever
    clears the approvals inbox, and can never be the maker (#70)."""

    lines = StockAdjustmentLineSerializer(many=True)

    class Meta:
        model = StockAdjustment
        fields = ["store", "reason", "notes", "lines"]

    def validate(self, data: Any) -> Any:
        if not data.get("lines"):
            raise serializers.ValidationError("At least one line is required.")
        return data

    def create(self, validated_data: Any) -> StockAdjustment:
        validated_data["lines"] = _against_the_book(
            validated_data["store"].id, validated_data["lines"]
        )
        adjustment: StockAdjustment = _create_with_approval(
            StockAdjustment,
            StockAdjustmentLine,
            "adjustment",
            validated_data,
            self.context.get("request"),
        )
        return adjustment


def _against_the_book(store_id: int, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill each adjustment line's book quantity and difference from the books.

    The counted quantity is the only number a correction takes from outside;
    everything it is measured against is read here (#76). A piece the location
    holds none of books as zero — that is a surplus, not a missing number.
    """
    from outbound.counting import book_quantities

    held = book_quantities(store_id, [ld["sku_code"] for ld in lines])
    return [
        {
            **ld,
            "book_qty": (book := held.get(ld["sku_code"], 0)),
            "adj_qty": ld["counted_qty"] - book,
        }
        for ld in lines
    ]


# ---------------------------------------------------------------------------
# Write-off
# ---------------------------------------------------------------------------


class WriteOffLineSerializer(serializers.ModelSerializer[WriteOffLine]):
    """``unit_cost_paise`` is read-only: a money posting reads its cost from the
    books, never from the payload (#103) — same rule as a transfer line."""

    class Meta:
        model = WriteOffLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "unit_cost_paise",
        ]
        read_only_fields = ["id", "unit_cost_paise"]


class WriteOffReadSerializer(ApprovedDocumentSerializer[WriteOff]):
    lines = WriteOffLineSerializer(many=True, read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)

    class Meta:
        model = WriteOff
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "store",
            "store_code",
            "store_name",
            "reason",
            "approved_by",
            "approved_by_name",
            "approval",
            "approval_history",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
            "lines",
        ]


class WriteOffWriteSerializer(serializers.ModelSerializer[WriteOff]):
    """``approved_by`` is not accepted: the approver is stamped by whoever
    clears the approvals inbox, and can never be the maker (#70)."""

    lines = WriteOffLineSerializer(many=True)

    class Meta:
        model = WriteOff
        fields = ["store", "reason", "lines"]

    def validate(self, data: Any) -> Any:
        if not data.get("lines"):
            raise serializers.ValidationError("At least one line is required.")
        return data

    def create(self, validated_data: Any) -> WriteOff:
        writeoff: WriteOff = _create_with_approval(
            WriteOff, WriteOffLine, "writeoff", validated_data, self.context.get("request")
        )
        return writeoff


# ---------------------------------------------------------------------------
# V-flip
# ---------------------------------------------------------------------------


class VFlipLineSerializer(serializers.ModelSerializer[VFlipLine]):
    """``unit_cost_paise`` is read-only: a money posting reads its cost from the
    books, never from the payload (#103) — same rule as a transfer line."""

    class Meta:
        model = VFlipLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "unit_cost_paise",
        ]
        read_only_fields = ["id", "unit_cost_paise"]


class VFlipReadSerializer(ApprovedDocumentSerializer[VFlip]):
    """V-flip's own approver column is ``authorized_by``; it still answers the
    common "approved by whom" question through ``approved_by_name``."""

    lines = VFlipLineSerializer(many=True, read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    original_brand_name = serializers.CharField(source="original_brand.name", read_only=True)

    class Meta:
        model = VFlip
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "store",
            "store_code",
            "store_name",
            "original_brand",
            "original_brand_name",
            "season",
            "authorized_by",
            "approved_by_name",
            "approval",
            "approval_history",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
            "lines",
        ]


class VFlipWriteSerializer(serializers.ModelSerializer[VFlip]):
    """``authorized_by`` is not accepted: the authoriser is stamped by whoever
    clears the approvals inbox, and can never be the maker (#70)."""

    lines = VFlipLineSerializer(many=True)

    class Meta:
        model = VFlip
        fields = ["store", "original_brand", "season", "lines"]

    def validate(self, data: Any) -> Any:
        if not data.get("lines"):
            raise serializers.ValidationError("At least one line is required.")
        return data

    def create(self, validated_data: Any) -> VFlip:
        vflip: VFlip = _create_with_approval(
            VFlip, VFlipLine, "vflip", validated_data, self.context.get("request")
        )
        return vflip


# ---------------------------------------------------------------------------
# Stock counting (#76)
# ---------------------------------------------------------------------------


class CountSessionLineSerializer(serializers.ModelSerializer[CountSessionLine]):
    """One barcode on one session. ``book_qty`` is null while the session is
    open — that null *is* the blindness, and it is the model's state rather than
    a field this serializer hides."""

    class Meta:
        model = CountSessionLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "counted_qty",
            "book_qty",
        ]
        read_only_fields = fields


class CountSessionReadSerializer(serializers.ModelSerializer[CountSession]):
    lines = CountSessionLineSerializer(many=True, read_only=True)
    scope_label = serializers.CharField(read_only=True)
    counted_by_name = serializers.SerializerMethodField()
    counted_pieces = serializers.SerializerMethodField()

    class Meta:
        model = CountSession
        fields = [
            "id",
            "stocktake",
            "scope",
            "scope_value",
            "scope_label",
            "status",
            "counted_by",
            "counted_by_name",
            "counted_pieces",
            "submitted_at",
            "created_at",
            "lines",
        ]

    def get_counted_by_name(self, obj: Any) -> str:
        return display_name(obj.counted_by)

    def get_counted_pieces(self, obj: Any) -> int:
        return sum(line.counted_qty for line in obj.lines.all())


class StocktakeReadSerializer(serializers.ModelSerializer[Stocktake]):
    sessions = CountSessionReadSerializer(many=True, read_only=True)
    store_code = serializers.CharField(source="store.code", read_only=True)
    store_name = serializers.CharField(source="store.name", read_only=True)
    opened_by_name = serializers.SerializerMethodField()
    adjustment_doc_number = serializers.SerializerMethodField()
    adjustment_docstatus = serializers.SerializerMethodField()

    class Meta:
        model = Stocktake
        fields = [
            "id",
            "store",
            "store_code",
            "store_name",
            "status",
            "note",
            "opened_by",
            "opened_by_name",
            "adjustment",
            "adjustment_doc_number",
            "adjustment_docstatus",
            "created_at",
            "updated_at",
            "sessions",
        ]

    def get_opened_by_name(self, obj: Any) -> str:
        return display_name(obj.opened_by)

    def get_adjustment_doc_number(self, obj: Any) -> str:
        return (obj.adjustment.doc_number or "") if obj.adjustment else ""

    def get_adjustment_docstatus(self, obj: Any) -> int | None:
        return obj.adjustment.docstatus if obj.adjustment else None


class StocktakeCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """Opening a count: which location, and an optional word about why."""

    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    note = serializers.CharField(max_length=240, required=False, allow_blank=True, default="")


class CountSessionCreateSerializer(serializers.Serializer[dict[str, Any]]):
    """One counter's pass: how much of the location they are taking on."""

    scope = serializers.ChoiceField(choices=CountScope.choices)
    scope_value = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )


class CountScanInputSerializer(serializers.Serializer[dict[str, Any]]):
    """Scanned pieces, additive. Nothing comes back but what was scanned."""

    scans = ScanLineSerializer(many=True, allow_empty=False)

    def scans_by_barcode(self) -> dict[str, int]:
        return _totals(self.validated_data["scans"])


class ApplyVarianceInputSerializer(serializers.Serializer[dict[str, Any]]):
    """Which moved lines the person has looked at and still wants applied.

    Confirmation is per barcode rather than a blanket "yes": the whole rule is
    that a piece which moved mid-count is never overwritten without someone
    seeing that particular piece.
    """

    confirm = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, default=list
    )


class RecountInputSerializer(serializers.Serializer[dict[str, Any]]):
    """A second person's count of one piece, and why it is out (#78).

    Three fields and no fourth. There is deliberately no cost and no variance
    here: both are read from the books, and the value that picks the approval
    band is the value that posts (#103). A recount that could price itself could
    route itself away from the Operations Head.
    """

    sku_code = serializers.CharField(max_length=64)
    counted_qty = serializers.IntegerField(min_value=0)
    reason = serializers.ChoiceField(choices=AdjustmentReason.choices)


# ---------------------------------------------------------------------------
# Stock request — the pull side of a transfer (#74)
# ---------------------------------------------------------------------------


class StockRequestLineReadSerializer(serializers.ModelSerializer[StockRequestLine]):
    qty_fulfilled = serializers.IntegerField(read_only=True)
    qty_committed = serializers.IntegerField(read_only=True)

    class Meta:
        model = StockRequestLine
        fields = [
            "id",
            "sku_code",
            "design",
            "color",
            "size",
            "brand",
            "season",
            "item",
            "hsn",
            "qty",
            "qty_fulfilled",
            "qty_committed",
        ]


class StockRequestLineWriteSerializer(serializers.ModelSerializer[StockRequestLine]):
    """Dims come straight off the cross-location search that built this line —
    there is no local stock to re-derive them from, unlike a transfer's plan:
    the whole point of a request is asking for stock the requesting store does
    not hold (#74)."""

    qty = serializers.IntegerField(min_value=1)

    class Meta:
        model = StockRequestLine
        fields = ["sku_code", "design", "color", "size", "brand", "season", "item", "hsn", "qty"]


class FulfillingTransferSummarySerializer(serializers.ModelSerializer[StoreTransfer]):
    """What a request's page shows about each transfer answering it — enough to
    link through, not the whole transfer."""

    source_store_code = serializers.CharField(source="source_store.code", read_only=True)
    destination_store_code = serializers.CharField(source="destination_store.code", read_only=True)

    class Meta:
        model = StoreTransfer
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "source_store_code",
            "destination_store_code",
            "dispatch_date",
            "created_at",
        ]


class StockRequestReadSerializer(ApprovedDocumentSerializer[StockRequest]):
    lines = StockRequestLineReadSerializer(many=True, read_only=True)
    requesting_store_code = serializers.CharField(source="requesting_store.code", read_only=True)
    requesting_store_name = serializers.CharField(source="requesting_store.name", read_only=True)
    fulfilling_store_code = serializers.CharField(source="fulfilling_store.code", read_only=True)
    fulfilling_store_name = serializers.CharField(source="fulfilling_store.name", read_only=True)
    status = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    decline_reason = serializers.SerializerMethodField()
    fulfilling_transfers = FulfillingTransferSummarySerializer(many=True, read_only=True)

    def get_status(self, obj: StockRequest) -> str:
        return obj.status.value

    def get_status_display(self, obj: StockRequest) -> str:
        return obj.status.label

    def get_decline_reason(self, obj: StockRequest) -> str:
        """Why the fulfilling store said no — the same reason the approvals
        inbox required on reject, read back onto the request (#74)."""
        approval = self._approval(obj)
        if approval is None or approval.status != ApprovalStatus.REJECTED:
            return ""
        reason: str = approval.reason
        return reason

    class Meta:
        model = StockRequest
        fields = [
            "id",
            "doc_number",
            "docstatus",
            "requesting_store",
            "requesting_store_code",
            "requesting_store_name",
            "fulfilling_store",
            "fulfilling_store_code",
            "fulfilling_store_name",
            "notes",
            "source",
            "expected_arrival_at",
            "status",
            "status_display",
            "decline_reason",
            "approved_by",
            "approved_by_name",
            "approval",
            "approval_history",
            "created_by",
            "created_by_name",
            "created_at",
            "updated_at",
            "lines",
            "fulfilling_transfers",
        ]


class StockRequestWriteSerializer(serializers.ModelSerializer[StockRequest]):
    """Raises the ask and puts it straight in the asking store's inbox — born
    waiting, same as a transfer (#137) and every other maker-checker family: a
    maker cannot forget to ask.

    What it deliberately does **not** validate is whether the fulfilling store
    can actually supply this (#175, D10 §3 locked): the named store is a
    suggestion the humans made, and a system that refused the ask because the
    projection says nought would be refusing on a number the shop floor is
    routinely ahead of. Possibility is the approvers' call — they have the phone
    call and the shelf; the route in front of this document is where it is made.
    """

    lines = StockRequestLineWriteSerializer(many=True)

    class Meta:
        model = StockRequest
        fields = [
            "requesting_store",
            "fulfilling_store",
            "notes",
            "source",
            "expected_arrival_at",
            "lines",
        ]

    def validate(self, data: Any) -> Any:
        if not data.get("lines"):
            raise serializers.ValidationError("At least one line is required.")
        if data.get("requesting_store") and data.get("fulfilling_store"):
            if data["requesting_store"] == data["fulfilling_store"]:
                raise serializers.ValidationError("Requesting and fulfilling store must differ.")
        return data

    def create(self, validated_data: Any) -> StockRequest:
        lines_data = validated_data.pop("lines")
        request = self.context.get("request")
        user = getattr(request, "user", None)
        validated_data["created_by"] = user
        with transaction.atomic():
            doc = StockRequest.objects.create(**validated_data)
            for ld in lines_data:
                StockRequestLine.objects.create(request=doc, **ld)
            request_document_approval(doc, requested_by=user)
        return doc


class StockRequestFulfilLineInputSerializer(serializers.Serializer[dict[str, Any]]):
    """How much of one request line this pass is committing to send."""

    line_id = serializers.IntegerField()
    qty = serializers.IntegerField(min_value=1)


class StockRequestFulfilInputSerializer(serializers.Serializer[dict[str, Any]]):
    """Which lines, and how much of each, the fulfilling store is sending now —
    may cover less than the full ask (a partial fulfilment)."""

    lines = StockRequestFulfilLineInputSerializer(many=True, allow_empty=False)


class StockRequestCloseInputSerializer(serializers.Serializer[dict[str, Any]]):
    """The fulfilling store saying no more is coming."""

    note = serializers.CharField(max_length=240, required=False, allow_blank=True, default="")
