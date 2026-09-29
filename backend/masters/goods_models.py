"""Goods-v1 organisation and configuration tables (design §5.2).

Legacy ``LegalEntity``/``Gstin``/``Store`` rows stay the readable identities.
Their goods lifecycle and capabilities come only from approved versions and the
``SiteGuard`` projection here, never from ``is_active`` or ``is_partner``.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Func
from django.db.models.functions import Now

from core.goods_base import PROJECTION, EvidenceRow, TenantOwned


class TsTzRange(Func):
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Tenant(models.Model):
    """The one business this deployment serves."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    deployment_key = models.UUIDField(unique=True)
    timezone = models.CharField(max_length=60)
    currency = models.CharField(max_length=3)
    locale = models.CharField(max_length=20)
    #: Synthetic tenants exist for development and tests only. Real opening-stock
    #: activation stays blocked by OQ-54 on every non-synthetic tenant.
    synthetic = models.BooleanField(default=False)
    business_profile_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.IntegerField(default=1)
    created_at = models.DateTimeField(db_default=Now())

    def __str__(self) -> str:
        return self.code


class MasterVersion(EvidenceRow):
    class Kind(models.TextChoices):
        TENANT = "tenant"
        ENTITY = "entity"
        REGISTRATION = "registration"
        SITE = "site"
        VENDOR = "vendor"
        BRAND = "brand"
        SUBBRAND = "subbrand"
        SEASON = "season"
        LOCATION = "location"
        SBU = "sbu"
        STAFF = "staff"
        STYLE = "style"
        SKU = "sku"
        ALIAS = "alias"
        CROSSWALK = "crosswalk"
        USER = "user"
        ROLE = "role"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    target_key = models.CharField(max_length=100)
    revision = models.IntegerField()
    payload = models.JSONField()
    retired = models.BooleanField(default=False)
    reason_code = models.CharField(max_length=60, null=True, blank=True)
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    approval = models.ForeignKey(
        "approvals.ApprovalRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "kind", "target_key", "revision"],
                name="uq_masterversion_revision",
            ),
        ]
        indexes = [models.Index(fields=["kind", "target_key", "effective_from"])]


class Sbu(TenantOwned):
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    brand = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    code = models.CharField(max_length=40)
    retired_at = models.DateTimeField(null=True, blank=True)
    #: The subject revision a retirement (E246) must quote; retiring bumps it.
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "brand"],
                condition=models.Q(brand__isnull=False),
                name="uq_sbu_site_brand",
            ),
            models.UniqueConstraint(
                fields=["site"], condition=models.Q(brand__isnull=True), name="uq_sbu_site_fallback"
            ),
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_sbu_code"),
        ]


class Location(TenantOwned):
    class Kind(models.TextChoices):
        FLOOR = "floor"
        BACKSTORE = "backstore"
        RECEIVING = "receiving"
        QUARANTINE = "quarantine"
        EXCESS_HOLD = "excess_hold"
        RTV_HOLD = "rtv_hold"
        TRANSIT_OUT = "transit_out"
        CUSTODY = "custody"
        ZONE = "zone"
        RACK = "rack"
        BIN = "bin"
        FIXTURE = "fixture"

    #: Kinds created once per site as protected system locations (GSA-T02: six).
    SYSTEM_KINDS: ClassVar[tuple[str, ...]] = (
        "receiving",
        "quarantine",
        "excess_hold",
        "rtv_hold",
        "transit_out",
        "custody",
    )

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    system = models.BooleanField(default=False)
    active_version = models.ForeignKey(
        MasterVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    retired_at = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["site", "name"], name="uq_location_site_name"),
            models.UniqueConstraint(
                fields=["site", "kind"],
                condition=models.Q(system=True),
                name="uq_location_system_kind",
            ),
        ]


class SiteCapabilityEvent(EvidenceRow):
    class Operation(models.TextChoices):
        OPENING_SETUP = "opening_setup"
        GOODS = "goods"
        SELL = "sell"
        NON_TRADING = "non_trading"
        CLOSING = "closing"
        CLOSED = "closed"

    class Outcome(models.TextChoices):
        APPROVED = "approved"
        REVOKED = "revoked"

    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    operation = models.CharField(max_length=20, choices=Operation.choices)
    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    site_revision = models.IntegerField()
    checks = models.JSONField()
    reason_code = models.CharField(max_length=60)
    evidence = models.ForeignKey(
        "files.EvidenceObject", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approver = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["site", "operation", "recorded_at"])]


class SiteGuard(TenantOwned):
    class Lifecycle(models.TextChoices):
        PLANNED = "planned"
        OPENING = "opening"
        ACTIVE = "active"
        CLOSING = "closing"
        CLOSED = "closed"

    class StockContract(models.TextChoices):
        LEGACY = "legacy"
        GOODS_V1 = "goods_v1"

    site = models.OneToOneField(
        "masters.Store", on_delete=models.PROTECT, related_name="goods_guard"
    )
    opening_setup_ready = models.BooleanField(default=False)
    goods_ready = models.BooleanField(default=False)
    sell_ready = models.BooleanField(default=False)
    non_trading_confirmed = models.BooleanField(default=False)
    lifecycle = models.CharField(
        max_length=10, choices=Lifecycle.choices, default=Lifecycle.PLANNED
    )
    stock_contract = models.CharField(
        max_length=10, choices=StockContract.choices, default=StockContract.LEGACY
    )
    capability_event = models.ForeignKey(
        SiteCapabilityEvent, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    freeze_id = models.UUIDField(null=True, blank=True)
    closure_date = models.DateField(null=True, blank=True)
    revision = models.IntegerField(default=1)

    PROTECTION: ClassVar[str] = PROJECTION


class ConfigDraft(TenantOwned):
    class State(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"
        APPROVED = "approved"

    kind = models.CharField(max_length=60)
    scope = models.JSONField()
    scope_key = models.CharField(max_length=500)
    revision = models.IntegerField(default=1)
    payload = models.JSONField()
    effective_from = models.DateTimeField()
    #: The approved version's exclusive end; null is open-ended (change PRD §14.4).
    effective_to = models.DateTimeField(null=True, blank=True)
    maker = models.ForeignKey("accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=10, choices=State.choices, default=State.DRAFT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["kind", "state"])]


class ConfigVersion(EvidenceRow):
    draft = models.ForeignKey(ConfigDraft, on_delete=models.PROTECT, related_name="versions")
    kind = models.CharField(max_length=60)
    version = models.IntegerField()
    scope = models.JSONField()
    scope_key = models.CharField(max_length=500)
    payload = models.JSONField()
    effective_from = models.DateTimeField()
    approved_by = models.ForeignKey(
        "accounts.HumanIdentity", on_delete=models.PROTECT, related_name="+"
    )
    source_revision = models.IntegerField()
    source_hash = models.CharField(max_length=64)
    backdate_impact = models.JSONField(default=list)

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.UniqueConstraint(
                fields=["tenant", "kind", "scope_key", "version"], name="uq_configversion_version"
            ),
        ]
        indexes = [models.Index(fields=["kind", "effective_from"])]


class EffectiveVersionPeriod(TenantOwned):
    class TargetKind(models.TextChoices):
        MASTER = "master"
        CONFIGURATION = "configuration"
        ASSIGNMENT = "assignment"
        GRANT = "grant"
        ALIAS = "alias"

    target_kind = models.CharField(max_length=20, choices=TargetKind.choices)
    target_id = models.UUIDField()
    scope_key = models.CharField(max_length=500)
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    #: A configuration version withdrawn for invalidity stops every use, pinned or not.
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_reason = models.CharField(max_length=60, null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "target_kind", "target_id"], name="uq_effectiveperiod_target"
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="ck_effectiveperiod_order",
            ),
            ExclusionConstraint(
                name="ex_effectiveperiod_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("target_kind", RangeOperators.EQUAL),
                    ("scope_key", RangeOperators.EQUAL),
                    (TsTzRange("effective_from", "effective_to"), RangeOperators.OVERLAPS),
                ],
                condition=models.Q(withdrawn_at__isnull=True),
            ),
        ]


class AliasRangeCounter(TenantOwned):
    range_version = models.OneToOneField(ConfigVersion, on_delete=models.PROTECT, related_name="+")
    next_value = models.BigIntegerField()
