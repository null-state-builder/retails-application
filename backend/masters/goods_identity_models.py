"""Stable merchandise identity for goods-v1 (design §3.4, §5.2, §5.5).

A barcode is an alias, never the identity. ``ProductSku`` is keyed by the hash of
its distinguishing attribute tuple under a pinned identity profile; aliases are
scoped by issuer, type, site and effective period and may point several SKUs at
one value. An ambiguity is resolved by a recorded pick against a document
revision or scan - never by merging SKUs. Legacy ``Sku``/``Cohort`` barcode rows
remain history.
"""

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import models
from django.db.models.functions import Coalesce

from core.goods_base import EvidenceRow, TenantOwned
from masters.goods_models import TsTzRange


class GovernanceState(models.TextChoices):
    PENDING = "pending"
    EFFECTIVE = "effective"
    RETIRED = "retired"


class Style(TenantOwned):
    brand = models.ForeignKey("masters.Brand", on_delete=models.PROTECT, related_name="+")
    style_code = models.CharField(max_length=120)
    profile_family = models.CharField(max_length=60)
    attrs = models.JSONField(default=list)
    governance_state = models.CharField(max_length=10, choices=GovernanceState.choices)
    originating_revision = models.ForeignKey(
        "core.DraftRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    retired_at = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "brand", "profile_family", "style_code"], name="uq_style_code"
            ),
        ]


class ProductSku(TenantOwned):
    style = models.ForeignKey(Style, on_delete=models.PROTECT, related_name="skus")
    identity_key = models.CharField(max_length=64)
    identity_profile = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    attrs = models.JSONField(default=list)
    no_discount = models.BooleanField(default=False)
    governance_state = models.CharField(max_length=10, choices=GovernanceState.choices)
    originating_revision = models.ForeignKey(
        "core.DraftRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    retired_at = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "identity_key"], name="uq_productsku_identity"
            ),
        ]
        indexes = [models.Index(fields=["style"])]


class SkuAlias(TenantOwned):
    class AliasType(models.TextChoices):
        BARCODE = "barcode"
        VENDOR_CODE = "vendor_code"
        GENERATED = "generated"

    sku = models.ForeignKey(ProductSku, on_delete=models.PROTECT, related_name="aliases")
    issuer_key = models.CharField(max_length=100)
    alias_type = models.CharField(max_length=12, choices=AliasType.choices)
    value = models.CharField(max_length=128)
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    config_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    governance_state = models.CharField(max_length=10, choices=GovernanceState.choices)
    originating_revision = models.ForeignKey(
        "core.DraftRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.IntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "issuer_key", "alias_type", "value", "effective_from"])
        ]
        constraints = [
            ExclusionConstraint(
                name="ex_skualias_same_sku_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("sku", RangeOperators.EQUAL),
                    ("issuer_key", RangeOperators.EQUAL),
                    ("alias_type", RangeOperators.EQUAL),
                    ("value", RangeOperators.EQUAL),
                    (Coalesce("site", models.Value(0)), RangeOperators.EQUAL),
                    (TsTzRange("effective_from", "effective_to"), RangeOperators.OVERLAPS),
                ],
                condition=~models.Q(governance_state="retired"),
            ),
        ]


class IdentityPick(EvidenceRow):
    subject_revision = models.ForeignKey(
        "core.DraftRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    scan_event = models.ForeignKey(
        "inbound.ScanObservation", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    value = models.CharField(max_length=128)
    candidates = models.JSONField()
    candidate_hash = models.CharField(max_length=64)
    chosen_sku = models.ForeignKey(ProductSku, on_delete=models.PROTECT, related_name="+")
    context = models.JSONField()

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=(
                    models.Q(subject_revision__isnull=False, scan_event__isnull=True)
                    | models.Q(subject_revision__isnull=True, scan_event__isnull=False)
                ),
                name="ck_identitypick_one_subject",
            ),
        ]
        indexes = [models.Index(fields=["chosen_sku", "recorded_at"])]


class SourceCrosswalk(TenantOwned):
    """One governed mapping from a source's own words to a tenant value.

    ``kind`` is either a master kind (vendor, brand, subbrand: ``target_key`` is
    that master's ID) or the name of a governed vocabulary dimension (an
    attribute rule for a PT column such as colour or size: ``target_key`` is the
    stable vocabulary value ID). An attribute rule's ``issuer_key`` says whose
    words it reads - see ``masters.goods_identity_services.parse_rule_issuer``.
    Store and warehouse operations PRD §5.4 ("one rulebook"); OPS-14.
    """

    class Kind(models.TextChoices):
        VENDOR = "vendor"
        BRAND = "brand"
        SUBBRAND = "subbrand"

    #: A master kind from ``Kind``, or a governed vocabulary dimension (open set,
    #: validated against the tenant's approved vocabulary when a rule is written).
    kind = models.CharField(max_length=60)
    issuer_key = models.CharField(max_length=100)
    source_key = models.CharField(max_length=240)
    target_key = models.CharField(max_length=100)
    config_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    governance_state = models.CharField(
        max_length=10, choices=GovernanceState.choices, default=GovernanceState.EFFECTIVE
    )
    retired_at = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "kind", "issuer_key", "source_key", "config_version"],
                name="uq_sourcecrosswalk",
            ),
        ]
        indexes = [models.Index(fields=["kind", "issuer_key", "source_key"])]
