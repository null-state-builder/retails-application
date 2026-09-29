"""Goods-v1 operational inventory (design §3.2-§3.3, §5.2, §7).

* ``CustodyLot`` - a counted quantity with its source evidence, never a balance.
* ``Origin`` - frozen operational value of one official receipt/opening line.
* ``CoverageEvent``/``LiveCoverage`` - which official line covers which portion.
* ``Position`` - where each portion of a lot is, in what condition, and whether it
  was accepted there. Portions of one lot never overlap.
* Holds and reservations reference portions independently of location.
* ``JournalBatch`` and its paired legs are the append-only operational journal;
  the application role cannot insert them except through the ledger function.

Projections (``LiveCoverage``, ``Position``, ``ActiveHold``, ``ActiveReservation``)
are rebuildable from the evidence and journal. Legacy barcode projections remain
for legacy sites only.
"""

from __future__ import annotations

from typing import ClassVar

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import models

from core.goods_base import JOURNAL, EvidenceRow, TenantOwned
from core.goods_fields import PaiseField, PortionField

CONDITIONS = [
    ("good", "good"),
    ("damaged", "damaged"),
    ("wrong", "wrong"),
    ("unidentified", "unidentified"),
]
BOUNDARIES = [
    ("physical", "physical"),
    ("external", "external"),
    ("disposed", "disposed"),
    ("returned", "returned"),
    ("consumed", "consumed"),
    ("transit", "transit"),
    ("matched_observation", "matched_observation"),
]


class CustodyLot(EvidenceRow):
    class SourceKind(models.TextChoices):
        GRN = "grn"
        OPENING = "opening"
        ADJUSTMENT = "adjustment"
        TRANSFER_EXCESS = "transfer_excess"

    source_line = models.ForeignKey(
        "core.OfficialLine", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source_manifest_row = models.ForeignKey(
        "ptmapper.OpeningManifestRow",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    adjustment_line = models.ForeignKey(
        "core.OfficialLine", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source_observation = models.ForeignKey(
        "inbound.ScanObservation", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    parent_lot = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    issued_qty = models.IntegerField()
    initial_site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    initial_identity = models.JSONField()
    source_time = models.DateTimeField()
    source_kind = models.CharField(max_length=16, choices=SourceKind.choices)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(issued_qty__gte=1, issued_qty__lte=999999),
                name="ck_custodylot_qty",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_kind="grn",
                        source_line__isnull=False,
                        source_manifest_row__isnull=True,
                        adjustment_line__isnull=True,
                        source_observation__isnull=True,
                    )
                    | models.Q(
                        source_kind="opening",
                        source_line__isnull=True,
                        source_manifest_row__isnull=False,
                        adjustment_line__isnull=True,
                        source_observation__isnull=True,
                    )
                    | models.Q(
                        source_kind="adjustment",
                        source_line__isnull=True,
                        source_manifest_row__isnull=True,
                        adjustment_line__isnull=False,
                        source_observation__isnull=True,
                    )
                    | models.Q(
                        source_kind="transfer_excess",
                        source_line__isnull=True,
                        source_manifest_row__isnull=True,
                        adjustment_line__isnull=True,
                        source_observation__isnull=False,
                    )
                ),
                name="ck_custodylot_one_source",
            ),
        ]


class Origin(EvidenceRow):
    class SourceKind(models.TextChoices):
        RECEIPT = "receipt"
        OPENING = "opening"
        VALUE_DAMAGE = "value_damage"

    lineage_key = models.UUIDField()
    official_line = models.OneToOneField(
        "core.OfficialLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="origin",
    )
    value_damage_disposition = models.OneToOneField(
        "inbound.Disposition",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="value_origin",
    )
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_time = models.DateTimeField()
    source_kind = models.CharField(max_length=12, choices=SourceKind.choices)
    sku = models.ForeignKey("masters.ProductSku", on_delete=models.PROTECT, related_name="+")
    unit_cost = PaiseField()
    mrp = PaiseField()
    opening_qty = models.IntegerField()
    frozen_evidence = models.JSONField()
    previous_origin = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=(
                    models.Q(source_kind__in=["receipt", "opening"], official_line__isnull=False)
                    & models.Q(value_damage_disposition__isnull=True)
                )
                | (
                    models.Q(source_kind="value_damage", official_line__isnull=True)
                    & models.Q(value_damage_disposition__isnull=False)
                ),
                name="ck_origin_source_reference",
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost__gt=0, mrp__gt=0, unit_cost__lte=models.F("mrp")),
                name="ck_origin_values",
            ),
        ]
        indexes = [models.Index(fields=["sku", "source_time", "lineage_key"])]


class CoverageEvent(EvidenceRow):
    class Effect(models.TextChoices):
        COVER = "cover"
        COUNTER = "counter"

    pt_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )
    pt_line = models.ForeignKey("core.OfficialLine", on_delete=models.PROTECT, related_name="+")
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="coverage_events")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    origin = models.ForeignKey(Origin, on_delete=models.PROTECT, related_name="coverage_events")
    effect = models.CharField(max_length=8, choices=Effect.choices)
    counter_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["lot"]), models.Index(fields=["pt_version"])]


class LiveCoverage(TenantOwned):
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    cover_event = models.ForeignKey(CoverageEvent, on_delete=models.PROTECT, related_name="+")
    origin = models.ForeignKey(Origin, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            ExclusionConstraint(
                name="ex_livecoverage_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("lot", RangeOperators.EQUAL),
                    ("portion", RangeOperators.OVERLAPS),
                ],
            ),
        ]


class LiveValueBasis(TenantOwned):
    """Current non-coverage memo value for an exact custody portion.

    P03 ``value_damage`` adds operational value without a physical reclassification,
    so this projection must not be smuggled into Position through a quantity pair.
    The immutable ValueLeg pair is the evidence; this row is only its live lookup.

    A value_damage P03 leg is append-only and never reversed: unlike ``LiveCoverage``,
    which nets through ``counter_coverage``, this row has no counter path. A future
    flow that needs to reverse a value leg must add that counter path first.
    """

    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    origin = models.ForeignKey(Origin, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            ExclusionConstraint(
                name="ex_livevaluebasis_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("lot", RangeOperators.EQUAL),
                    ("portion", RangeOperators.OVERLAPS),
                ],
            ),
        ]
        indexes = [models.Index(fields=["lot"])]


class Position(TenantOwned):
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    boundary = models.CharField(max_length=20, choices=BOUNDARIES)
    transfer = models.ForeignKey(
        "outbound.GoodsTransfer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    sku = models.ForeignKey(
        "masters.ProductSku", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    origin = models.ForeignKey(
        Origin, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    accepted_event = models.ForeignKey(
        "stockledger.AcceptanceEvent",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    condition = models.CharField(max_length=12, choices=CONDITIONS)
    value_basis_origin = models.ForeignKey(
        Origin, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    description = models.CharField(max_length=240, default="")

    class Meta:
        constraints = [
            ExclusionConstraint(
                name="ex_position_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("lot", RangeOperators.EQUAL),
                    ("portion", RangeOperators.OVERLAPS),
                ],
            ),
            models.CheckConstraint(
                condition=~models.Q(boundary="physical")
                | models.Q(site__isnull=False, location__isnull=False),
                name="ck_position_physical_address",
            ),
            models.CheckConstraint(
                condition=~models.Q(boundary="transit") | models.Q(transfer__isnull=False),
                name="ck_position_transit_transfer",
            ),
        ]
        indexes = [models.Index(fields=["site", "sku", "origin", "boundary", "condition"])]


class HoldEvent(EvidenceRow):
    class Effect(models.TextChoices):
        PLACE = "place"
        RELEASE = "release"

    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    hold_key = models.UUIDField()
    kind = models.CharField(max_length=60)
    effect = models.CharField(max_length=8, choices=Effect.choices)
    source_version = models.ForeignKey(
        "core.OfficialVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    journal_batch = models.ForeignKey(
        "stockledger.JournalBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["lot", "hold_key"])]


class ActiveHold(TenantOwned):
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    hold_key = models.UUIDField()
    kind = models.CharField(max_length=60)
    event = models.ForeignKey(HoldEvent, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            ExclusionConstraint(
                name="ex_activehold_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("lot", RangeOperators.EQUAL),
                    ("hold_key", RangeOperators.EQUAL),
                    ("portion", RangeOperators.OVERLAPS),
                ],
            ),
        ]
        indexes = [models.Index(fields=["lot"])]


class ReservationEvent(EvidenceRow):
    class Effect(models.TextChoices):
        RESERVE = "reserve"
        RELEASE = "release"
        CONSUME = "consume"

    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    transfer_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )
    effect = models.CharField(max_length=8, choices=Effect.choices)
    prior_event = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    journal_batch = models.ForeignKey(
        "stockledger.JournalBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["transfer_version"]), models.Index(fields=["lot"])]


class ActiveReservation(TenantOwned):
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    event = models.ForeignKey(ReservationEvent, on_delete=models.PROTECT, related_name="+")
    transfer_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            ExclusionConstraint(
                name="ex_activereservation_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("lot", RangeOperators.EQUAL),
                    ("portion", RangeOperators.OVERLAPS),
                ],
            ),
        ]
        indexes = [models.Index(fields=["transfer_version"])]


class AllocationGuard(TenantOwned):
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    sku = models.ForeignKey("masters.ProductSku", on_delete=models.PROTECT, related_name="+")
    epoch = models.BigIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["site", "sku"], name="uq_allocationguard")]


class JournalBatch(EvidenceRow):
    version = models.ForeignKey("core.OfficialVersion", on_delete=models.PROTECT, related_name="+")
    event_key = models.UUIDField()
    posting_kind = models.CharField(max_length=60)
    content_hash = models.CharField(max_length=64)

    PROTECTION: ClassVar[str] = JOURNAL

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["tenant", "event_key"], name="uq_journalbatch_event"),
        ]


SIDES = [("source", "source"), ("destination", "destination")]


class QuantityLeg(EvidenceRow):
    batch = models.ForeignKey(JournalBatch, on_delete=models.PROTECT, related_name="quantity_legs")
    pair_key = models.UUIDField()
    side = models.CharField(max_length=12, choices=SIDES)
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    position = models.JSONField()
    qty = models.BigIntegerField()

    PROTECTION: ClassVar[str] = JOURNAL

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["batch", "pair_key", "side"], name="uq_quantityleg_side"
            ),
            models.CheckConstraint(
                condition=models.Q(side="source", qty__lt=0)
                | models.Q(side="destination", qty__gt=0),
                name="ck_quantityleg_sign",
            ),
        ]


class ValueLeg(EvidenceRow):
    class Bucket(models.TextChoices):
        STOCK = "stock"
        TRANSIT = "transit"
        ORIGIN_EVIDENCE = "origin_evidence"
        ADJUSTMENT_EVIDENCE = "adjustment_evidence"
        EXTERNAL = "external"

    batch = models.ForeignKey(JournalBatch, on_delete=models.PROTECT, related_name="value_legs")
    pair_key = models.UUIDField()
    side = models.CharField(max_length=12, choices=SIDES)
    origin = models.ForeignKey(Origin, on_delete=models.PROTECT, related_name="+")
    lot = models.ForeignKey(
        CustodyLot, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    portion = PortionField(null=True, blank=True)
    bucket = models.CharField(max_length=20, choices=Bucket.choices)
    leg_site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    amount = PaiseField()

    PROTECTION: ClassVar[str] = JOURNAL

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(fields=["batch", "pair_key", "side"], name="uq_valueleg_side"),
            models.CheckConstraint(
                condition=models.Q(side="source", amount__lt=0)
                | models.Q(side="destination", amount__gt=0),
                name="ck_valueleg_sign",
            ),
        ]


class EncumbranceLeg(EvidenceRow):
    class Axis(models.TextChoices):
        HOLD = "hold"
        RESERVATION = "reservation"

    class Gate(models.TextChoices):
        INACTIVE = "inactive"
        ACTIVE = "active"

    batch = models.ForeignKey(
        JournalBatch, on_delete=models.PROTECT, related_name="encumbrance_legs"
    )
    pair_key = models.UUIDField()
    side = models.CharField(max_length=12, choices=SIDES)
    axis = models.CharField(max_length=12, choices=Axis.choices)
    control_key = models.UUIDField()
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    gate = models.CharField(max_length=8, choices=Gate.choices)
    qty = models.BigIntegerField()

    PROTECTION: ClassVar[str] = JOURNAL

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["batch", "pair_key", "side"], name="uq_encumbranceleg_side"
            ),
            models.CheckConstraint(
                condition=models.Q(side="source", qty__lt=0)
                | models.Q(side="destination", qty__gt=0),
                name="ck_encumbranceleg_sign",
            ),
        ]


class AcceptanceSession(TenantOwned):
    class State(models.TextChoices):
        OPEN = "open"
        COMPLETED = "completed"
        CANCELLED = "cancelled"

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=10, choices=State.choices, default=State.OPEN)
    revision = models.IntegerField(default=1)
    opened_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    last_activity_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["site", "state"])]


class AcceptanceEvent(EvidenceRow):
    class Outcome(models.TextChoices):
        CHECKED_GOOD = "checked_good"
        ACCEPTED_GOOD = "accepted_good"
        DAMAGED = "damaged"
        WRONG = "wrong"

    session = models.ForeignKey(AcceptanceSession, on_delete=models.PROTECT, related_name="events")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    scan_key = models.UUIDField()
    official_line = models.ForeignKey(
        "core.OfficialLine", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    portion = PortionField()
    destination_location = models.ForeignKey(
        "masters.Location", on_delete=models.PROTECT, related_name="+"
    )
    observed_alias = models.CharField(max_length=128)
    observed_ticket_mrp = PaiseField(null=True, blank=True)
    label_evidence_ref = models.CharField(max_length=100, null=True, blank=True)
    tag_verdict = models.CharField(
        max_length=10, choices=[("matched", "matched"), ("mismatch", "mismatch")]
    )
    outcome = models.CharField(max_length=14, choices=Outcome.choices)
    journal_batch = models.ForeignKey(
        JournalBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["session", "recorded_at"])]


class CustodyMatch(EvidenceRow):
    observed_lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    observed_portion = PortionField()
    source_lot = models.ForeignKey(CustodyLot, on_delete=models.PROTECT, related_name="+")
    source_portion = PortionField()
    corrective_transfer = models.ForeignKey(
        "outbound.GoodsTransfer", on_delete=models.PROTECT, related_name="+"
    )
    evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["corrective_transfer"])]
        constraints = [
            *EvidenceRow.Meta.constraints,
            # Goods ticket 16: two corrective transfers can never match the same
            # observed pieces - the last line of defence behind the claim and
            # position checks the confirmation makes under the lot's lock.
            ExclusionConstraint(
                name="ex_custodymatch_observed",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("observed_lot", RangeOperators.EQUAL),
                    ("observed_portion", RangeOperators.OVERLAPS),
                ],
            ),
        ]
