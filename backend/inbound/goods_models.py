"""Goods-v1 arrival, durable counting, quantity-only GRN and dispositions (design §5.2, §5.6).

What physically arrived is recorded before anyone knows its identity or value:
an arrival, one or more counting sessions of immutable scan observations, then a
GRN issued once from those observations. Invoice claims are kept apart from
counts. Corrections append counter-GRNs and dispositions; nothing edits a count.
The legacy GRN was deleted (OPS-18).
"""

from __future__ import annotations

from django.db import models
from django.db.models.functions import Lower, Trim

from core.goods_base import EvidenceRow, TenantOwned
from core.goods_fields import PortionField

CONDITIONS = [
    ("good", "good"),
    ("damaged", "damaged"),
    ("wrong", "wrong"),
    ("unidentified", "unidentified"),
]


class Arrival(EvidenceRow):
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    vendor = models.ForeignKey("vendors.Vendor", on_delete=models.PROTECT, related_name="+")
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    subbrand_key = models.CharField(max_length=100, null=True, blank=True)
    actual_arrival_at = models.DateTimeField()
    transporter_ref = models.CharField(max_length=160, null=True, blank=True)
    booking = models.ForeignKey(
        "vendors.GoodsBooking", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    invoice_evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    invoice_number = models.CharField(max_length=80, null=True, blank=True)
    invoice_date = models.DateField(null=True, blank=True)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(actual_arrival_at__lte=models.F("recorded_at")),
                name="ck_arrival_not_in_future",
            ),
        ]
        indexes = [
            models.Index(fields=["site", "actual_arrival_at"]),
            # The duplicate-invoice warning asks "this vendor, this invoice
            # number" of every arrival in the receiving legal entity, and asks it
            # on the write path of every arrival recorded with an invoice number
            # (GSA-T05). It compares the number the way people type it, so the
            # index is over that comparison rather than the raw column -
            # `inbound.goods_services.INVOICE_MATCH` is the same expression.
            models.Index(
                Lower(Trim("invoice_number")),
                "vendor",
                name="ix_arrival_invoice_match",
            ),
        ]


class ArrivalHead(TenantOwned):
    """The mutable subject revision of an arrival (sessions, claims, decisions)."""

    arrival = models.OneToOneField(Arrival, on_delete=models.PROTECT, related_name="head")
    revision = models.IntegerField(default=1)
    grn_count = models.IntegerField(default=0)


class CountSession(TenantOwned):
    class State(models.TextChoices):
        OPEN = "open"
        ISSUED = "issued"
        CANCELLED = "cancelled"

    arrival = models.ForeignKey(Arrival, on_delete=models.PROTECT, related_name="sessions")
    counter = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    entry_user = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    revision = models.IntegerField(default=1)
    grn = models.ForeignKey(
        "core.DocumentIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    last_activity_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["arrival", "state"])]


class CountHandover(EvidenceRow):
    """One unfinished count passed from the person holding it to another (E240).

    The session's ``counter``/``entry_user`` are only the current owner - a
    projection this chain is the truth behind. Who counted before a handover is
    read here and from each observation's own actor, never from the session row,
    which is why the handover is evidence rather than a field that is overwritten
    and forgotten (GSA-T05).
    """

    count_session = models.ForeignKey(
        CountSession, on_delete=models.PROTECT, related_name="handovers"
    )
    from_human = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    to_human = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    reason_code = models.CharField(max_length=60)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(from_human=models.F("to_human")),
                name="ck_counthandover_to_another_person",
            ),
        ]
        indexes = [models.Index(fields=["count_session", "recorded_at"])]


class DuplicateArrivalAcknowledgement(EvidenceRow):
    """Why a second arrival was recorded for a vendor and invoice already seen.

    The warning itself is not stored: it is the server's reading of the arrivals
    the recorder could see at that moment, so it is recomputed rather than
    remembered. What is kept is the decision made against it - which warning
    (``warning_hash`` over exactly the matched records), which records those
    were, who said carry on, when, and why.

    ``matched_arrivals`` holds only records the recorder could already read: a
    warning never names one they could not have opened for themselves, and this
    row never becomes the door to one (GSA-T05).
    """

    arrival = models.OneToOneField(
        Arrival, on_delete=models.PROTECT, related_name="duplicate_acknowledgement"
    )
    warning_hash = models.CharField(max_length=64)
    reason = models.CharField(max_length=500)
    matched_arrivals = models.JSONField(default=list)

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["warning_hash"])]


class ScanObservation(EvidenceRow):
    count_session = models.ForeignKey(
        CountSession, null=True, blank=True, on_delete=models.PROTECT, related_name="observations"
    )
    stocktake_pass = models.ForeignKey(
        "outbound.GoodsCountPass",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    acceptance = models.ForeignKey(
        "stockledger.AcceptanceSession",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    scan_key = models.UUIDField()
    sku = models.ForeignKey(
        "masters.ProductSku", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    description = models.CharField(max_length=240)
    alias_value = models.CharField(max_length=128, null=True, blank=True)
    #: The AliasContext the code was read under: whose issuer, which alias type,
    #: which governed identity profile. Without it an observation is not evidence
    #: of what was scanned - a code that is unique under this vendor's issuer key
    #: and shared tenant-wide would be judged twice, under two different scopes,
    #: and the second judgement could refuse goods the first one settled.
    alias_context = models.JSONField(null=True, blank=True)
    attrs = models.JSONField(default=list)
    condition = models.CharField(max_length=12, choices=CONDITIONS)
    qty = models.IntegerField()
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    correction_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="corrections"
    )
    #: Goods ticket 16: an excess observation at an internal transfer's
    #: destination count - goods nobody sent, or wrong goods that came in place
    #: of expected ones - bound to the shipment whose count recorded it. It is
    #: the identity evidence of the unvalued ``transfer_excess`` custody lot.
    transfer_dispatch = models.ForeignKey(
        "outbound.TransferDispatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="observations",
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=(
                    models.Q(
                        count_session__isnull=False,
                        stocktake_pass__isnull=True,
                        acceptance__isnull=True,
                        transfer_dispatch__isnull=True,
                    )
                    | models.Q(
                        count_session__isnull=True,
                        stocktake_pass__isnull=False,
                        acceptance__isnull=True,
                        transfer_dispatch__isnull=True,
                    )
                    | models.Q(
                        count_session__isnull=True,
                        stocktake_pass__isnull=True,
                        acceptance__isnull=False,
                        transfer_dispatch__isnull=True,
                    )
                    | models.Q(
                        count_session__isnull=True,
                        stocktake_pass__isnull=True,
                        acceptance__isnull=True,
                        transfer_dispatch__isnull=False,
                    )
                ),
                name="ck_scanobservation_one_session",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(correction_of__isnull=True, qty__gte=1, qty__lte=999999)
                    | models.Q(correction_of__isnull=False, qty__gte=-999999, qty__lte=999999)
                ),
                name="ck_scanobservation_qty",
            ),
            models.UniqueConstraint(
                fields=["count_session", "scan_key"],
                condition=models.Q(count_session__isnull=False),
                name="uq_scan_count_session",
            ),
            models.UniqueConstraint(
                fields=["stocktake_pass", "scan_key"],
                condition=models.Q(stocktake_pass__isnull=False),
                name="uq_scan_count_pass",
            ),
            models.UniqueConstraint(
                fields=["acceptance", "scan_key"],
                condition=models.Q(acceptance__isnull=False),
                name="uq_scan_acceptance",
            ),
            models.UniqueConstraint(
                fields=["transfer_dispatch", "scan_key"],
                condition=models.Q(transfer_dispatch__isnull=False),
                name="uq_scan_transfer_dispatch",
            ),
        ]
        indexes = [models.Index(fields=["count_session", "recorded_at"])]


class InvoiceClaimVersion(EvidenceRow):
    arrival = models.ForeignKey(Arrival, on_delete=models.PROTECT, related_name="claims")
    revision = models.IntegerField()
    evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    lines = models.JSONField()
    content_hash = models.CharField(max_length=64)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["arrival", "revision"], name="uq_invoiceclaim_revision"
            ),
        ]


class GoodsGrn(TenantOwned):
    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="goods_grn"
    )
    arrival = models.ForeignKey(Arrival, on_delete=models.PROTECT, related_name="grns")
    count_session = models.OneToOneField(
        CountSession, on_delete=models.PROTECT, related_name="goods_grn"
    )
    booking = models.ForeignKey(
        "vendors.GoodsBooking", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    counter_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="counters"
    )


class CounterGrnDraft(TenantOwned):
    """A counter-GRN awaiting its distinct approval; the approved result is a GRN version."""

    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="counter_grn"
    )
    grn = models.ForeignKey(GoodsGrn, on_delete=models.PROTECT, related_name="counter_drafts")


class Disposition(EvidenceRow):
    class Kind(models.TextChoices):
        ACCEPT_SHORTAGE = "accept_shortage"
        HOLD_EXCESS = "hold_excess"
        ACCEPT_EXCESS = "accept_excess"
        HOLD_DAMAGE = "hold_damage"
        VALUE_DAMAGE = "value_damage"
        RETURN = "return"
        DISPOSE = "dispose"
        RESOLVE_IDENTITY = "resolve_identity"
        ACCEPT_WRONG = "accept_wrong"

    document = models.ForeignKey(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="+"
    )
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_line_key = models.UUIDField()
    lot = models.ForeignKey(
        "stockledger.CustodyLot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    portion = PortionField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    qty = models.IntegerField()
    grn_revision = models.IntegerField()
    pt_revision = models.ForeignKey(
        "core.DraftRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    authority_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decision = models.JSONField()
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    journal_batch = models.ForeignKey(
        "stockledger.JournalBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(condition=models.Q(qty__gt=0), name="ck_disposition_qty"),
            models.CheckConstraint(
                condition=models.Q(lot__isnull=True, portion__isnull=True)
                | models.Q(lot__isnull=False, portion__isnull=False),
                name="ck_disposition_lot_portion",
            ),
        ]
        indexes = [models.Index(fields=["source_line_key", "recorded_at"])]


class ArrivalDecision(EvidenceRow):
    class Kind(models.TextChoices):
        NO_BOOKING_CONFIRMED = "no_booking_confirmed"

    arrival = models.ForeignKey(Arrival, on_delete=models.PROTECT, related_name="decisions")
    kind = models.CharField(max_length=30, choices=Kind.choices)
    reason_code = models.CharField(max_length=60)
    evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["arrival", "kind"], name="uq_arrivaldecision_kind"),
        ]
        indexes = [models.Index(fields=["arrival", "recorded_at"])]
