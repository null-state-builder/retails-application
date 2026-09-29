"""Goods-v1 PT documents, opening manifests and label print jobs (design §5.2, §5.5).

A PT is a versioned document with exactly one purpose parent: a GRN (receipt,
primary or supplement), an approved opening manifest version, or a transfer.
Primary identity per GRN is unique for life; supplements add coverage only for
newly authorised uncovered quantity. The legacy PT files were deleted (OPS-18).
"""

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import models

from core.goods_base import EvidenceRow, TenantOwned
from core.goods_fields import PortionField


class GoodsPt(TenantOwned):
    class ReceiptKind(models.TextChoices):
        PRIMARY = "primary"
        SUPPLEMENT = "supplement"

    document = models.OneToOneField(
        "core.DocumentIdentity", on_delete=models.PROTECT, related_name="goods_pt"
    )
    grn = models.ForeignKey(
        "inbound.GoodsGrn", null=True, blank=True, on_delete=models.PROTECT, related_name="pts"
    )
    manifest_version = models.ForeignKey(
        "ptmapper.OpeningManifestVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pts",
    )
    transfer = models.ForeignKey(
        "outbound.GoodsTransfer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="pts",
    )
    receipt_kind = models.CharField(
        max_length=10, choices=ReceiptKind.choices, null=True, blank=True
    )
    preparer = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        grn__isnull=False, manifest_version__isnull=True, transfer__isnull=True
                    )
                    | models.Q(
                        grn__isnull=True, manifest_version__isnull=False, transfer__isnull=True
                    )
                    | models.Q(
                        grn__isnull=True, manifest_version__isnull=True, transfer__isnull=False
                    )
                ),
                name="ck_goodspt_one_purpose",
            ),
            models.CheckConstraint(
                condition=models.Q(grn__isnull=True) | models.Q(receipt_kind__isnull=False),
                name="ck_goodspt_receipt_kind",
            ),
            models.UniqueConstraint(
                fields=["grn"],
                condition=models.Q(receipt_kind="primary"),
                name="uq_goodspt_primary_per_grn",
            ),
            # One opening PT per approved manifest version - the opening analogue of
            # the primary-per-GRN rule. The service locks the manifest first; this is
            # the database's own guard against two concurrent creates.
            models.UniqueConstraint(
                fields=["manifest_version"],
                condition=models.Q(manifest_version__isnull=False),
                name="uq_goodspt_per_manifest_version",
            ),
        ]


class OpeningManifest(TenantOwned):
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    dataset_key = models.CharField(max_length=100)
    batch_key = models.CharField(max_length=100)
    revision = models.IntegerField(default=1)
    current_version = models.ForeignKey(
        "ptmapper.OpeningManifestVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_version = models.ForeignKey(
        "ptmapper.OpeningManifestVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "site", "batch_key"], name="uq_openingmanifest_batch"
            ),
        ]


class OpeningManifestVersion(EvidenceRow):
    manifest = models.ForeignKey(OpeningManifest, on_delete=models.PROTECT, related_name="versions")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    revision = models.IntegerField()
    cutoff_at = models.DateTimeField()
    source_evidence = models.ForeignKey(
        "files.EvidenceObject", on_delete=models.PROTECT, related_name="+"
    )
    profile_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    content_hash = models.CharField(max_length=64)
    maker = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    row_count = models.IntegerField(default=0)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["manifest", "revision"], name="uq_manifestversion_revision"
            ),
        ]


class OpeningManifestRow(EvidenceRow):
    manifest_version = models.ForeignKey(
        OpeningManifestVersion, on_delete=models.PROTECT, related_name="rows"
    )
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_row_key = models.CharField(max_length=100)
    payload = models.JSONField()
    verification = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["manifest_version", "source_row_key"], name="uq_manifestrow_key"
            ),
        ]


class OpeningVariance(EvidenceRow):
    manifest_row = models.ForeignKey(
        OpeningManifestRow, on_delete=models.PROTECT, related_name="variances"
    )
    decision = models.JSONField()
    approved_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    maker = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    profile_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(maker=models.F("approved_by")),
                name="ck_openingvariance_distinct",
            ),
        ]


class OpeningSeasonCorrection(EvidenceRow):
    """A later, governed correction of an opening row's season (store and
    warehouse operations PRD §4, "later correction preserves evidence").

    An append-only fact beside the row, never an edit of it: the manifest row,
    its verification, the opening PT's official version and every bill, label
    or transfer snapshot already issued keep exactly what they said. Stock reads
    overlay the latest correction as the season the goods are in *now* and still
    show the row's original season beside it. ``EvidenceRow`` carries who, when
    and under which command.
    """

    manifest_row = models.ForeignKey(
        OpeningManifestRow, on_delete=models.PROTECT, related_name="season_corrections"
    )
    from_season = models.ForeignKey("masters.Season", on_delete=models.PROTECT, related_name="+")
    to_season = models.ForeignKey("masters.Season", on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=500)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(from_season=models.F("to_season")),
                name="ck_openingseasoncorrection_changes_season",
            ),
        ]
        indexes = [models.Index(fields=["manifest_row", "recorded_at"])]


class OpeningClaim(TenantOwned):
    manifest_row = models.ForeignKey(OpeningManifestRow, on_delete=models.PROTECT, related_name="+")
    dataset_key = models.CharField(max_length=100)
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    source_row_key = models.CharField(max_length=100)
    portion = PortionField()
    pt_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            ExclusionConstraint(
                name="ex_openingclaim_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("dataset_key", RangeOperators.EQUAL),
                    ("site", RangeOperators.EQUAL),
                    ("source_row_key", RangeOperators.EQUAL),
                    ("portion", RangeOperators.OVERLAPS),
                ],
            ),
        ]
        indexes = [models.Index(fields=["pt_version"])]


class PrintJob(TenantOwned):
    class Status(models.TextChoices):
        PREPARED = "prepared"
        ATTEMPTED = "attempted"
        CONFIRMED = "confirmed"
        PARTIAL = "partial"
        FAILED = "failed"
        UNKNOWN = "unknown"

    pt_version = models.ForeignKey(
        "core.OfficialVersion", on_delete=models.PROTECT, related_name="+"
    )
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    lines = models.JSONField()
    template_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    label_profile = models.JSONField()
    reprint_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reprints"
    )
    reason_code = models.CharField(max_length=60, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PREPARED)
    evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    command_key = models.ForeignKey("core.CommandKey", on_delete=models.PROTECT, related_name="+")
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reprint_of__isnull=True) | models.Q(reason_code__isnull=False),
                name="ck_printjob_reprint_reason",
            ),
        ]


class PrintEvent(EvidenceRow):
    class Outcome(models.TextChoices):
        ATTEMPTED = "attempted"
        CONFIRMED = "confirmed"
        PARTIAL = "partial"
        FAILED = "failed"
        UNKNOWN = "unknown"
        SCAN_VERIFIED = "scan_verified"

    job = models.ForeignKey(PrintJob, on_delete=models.PROTECT, related_name="events")
    outcome = models.CharField(max_length=14, choices=Outcome.choices)
    details = models.JSONField()

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["job", "recorded_at"])]
