"""People, sessions and grants for goods-v1 (design §4.2, §5.2).

A login (``User``) is not a person. Segregation of duties compares one stable
``HumanIdentity``, so the same person holding two roles still cannot check their
own work. Sessions are opaque server-side records; the cookie carries a random
token whose hash is all the database keeps.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from core.goods_base import PROJECTION, EvidenceRow, TenantOwned


class HumanIdentity(TenantOwned):
    staff_code = models.CharField(max_length=40)
    display_name = models.CharField(max_length=160)
    active = models.BooleanField(default=True)

    PROTECTION: ClassVar[str] = PROJECTION

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "staff_code"], name="uq_humanidentity_staff_code"
            ),
        ]

    def __str__(self) -> str:
        return self.display_name


class ServerSession(TenantOwned):
    user = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="server_sessions"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    csrf_hash = models.CharField(max_length=64)
    issued_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    step_up_at = models.DateTimeField(null=True, blank=True)
    security_epoch = models.BigIntegerField()

    class Meta:
        indexes = [models.Index(fields=["user", "expires_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    expires_at__lte=models.F("issued_at")
                    + models.Value("12 hours", output_field=models.DurationField())
                ),
                name="ck_serversession_absolute_life",
            ),
        ]


class SecurityGuard(TenantOwned):
    human = models.OneToOneField(
        HumanIdentity, on_delete=models.PROTECT, related_name="security_guard"
    )
    epoch = models.BigIntegerField(default=1)


class AuthenticationFailure(TenantOwned):
    identifier_hash = models.CharField(max_length=64)
    window_start = models.DateTimeField()
    failure_count = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "identifier_hash"], name="uq_authfailure_identifier"
            ),
        ]
        indexes = [models.Index(fields=["locked_until"])]


class Staff(TenantOwned):
    human = models.OneToOneField(HumanIdentity, on_delete=models.PROTECT, related_name="staff")
    mobile = models.CharField(max_length=30, null=True, blank=True)
    salesperson = models.BooleanField(default=False)
    retired_at = models.DateTimeField(null=True, blank=True)
    revision = models.IntegerField(default=1)


class StaffAssignment(EvidenceRow):
    staff = models.ForeignKey(Staff, on_delete=models.PROTECT, related_name="assignments")
    site = models.ForeignKey("masters.Store", on_delete=models.PROTECT, related_name="+")
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    primary = models.BooleanField(default=True)

    class Meta(EvidenceRow.Meta):
        indexes = [models.Index(fields=["site", "effective_from"])]


class RoleGrant(EvidenceRow):
    class ScopeKind(models.TextChoices):
        TENANT = "tenant"
        ENTITY = "entity"
        SITE = "site"
        SBU = "sbu"
        BRAND = "brand"

    human = models.ForeignKey(HumanIdentity, on_delete=models.PROTECT, related_name="grants")
    role = models.ForeignKey("accounts.Role", on_delete=models.PROTECT, related_name="+")
    scope_kind = models.CharField(max_length=10, choices=ScopeKind.choices)
    entity = models.ForeignKey(
        "masters.LegalEntity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    site = models.ForeignKey(
        "masters.Store", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    sbu = models.ForeignKey(
        "masters.Sbu", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    brand = models.ForeignKey(
        "masters.Brand", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    action_set = models.JSONField()
    field_set = models.JSONField()
    config_version = models.ForeignKey(
        "masters.ConfigVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revokes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="revocations"
    )

    class Meta(EvidenceRow.Meta):
        constraints = [
            *EvidenceRow.Meta.constraints,
            models.CheckConstraint(
                condition=(
                    models.Q(
                        scope_kind="tenant",
                        entity__isnull=True,
                        site__isnull=True,
                        sbu__isnull=True,
                        brand__isnull=True,
                    )
                    | models.Q(
                        scope_kind="entity",
                        entity__isnull=False,
                        site__isnull=True,
                        sbu__isnull=True,
                        brand__isnull=True,
                    )
                    | models.Q(
                        scope_kind="site",
                        entity__isnull=True,
                        site__isnull=False,
                        sbu__isnull=True,
                        brand__isnull=True,
                    )
                    | models.Q(
                        scope_kind="sbu",
                        entity__isnull=True,
                        site__isnull=True,
                        sbu__isnull=False,
                        brand__isnull=True,
                    )
                    | models.Q(
                        scope_kind="brand",
                        entity__isnull=True,
                        site__isnull=True,
                        sbu__isnull=True,
                        brand__isnull=False,
                    )
                ),
                name="ck_rolegrant_scope",
            ),
        ]
        indexes = [models.Index(fields=["human", "scope_kind", "effective_from"])]
