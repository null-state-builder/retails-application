"""People, sessions and grants for goods-v1 (design §4.2, §5.2).

A login (``User``) is not a person. Segregation of duties compares one stable
``HumanIdentity``, so the same person holding two roles still cannot check their
own work. Sessions are opaque server-side records; the cookie carries a random
token whose hash is all the database keeps.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
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


class RoleAssignment(TenantOwned):
    """One person's role over one explicit site/brand scope tuple.

    ``all_sites`` and ``all_brands`` deliberately differ from a selected list:
    they include sites or brands created later. An empty selected list confers
    no access. Legacy source links are retained so historical authority can be
    traced without consulting old grants when evaluating new requests.
    """

    human = models.ForeignKey(HumanIdentity, on_delete=models.PROTECT, related_name="role_assignments")
    role = models.ForeignKey("accounts.Role", on_delete=models.PROTECT, related_name="assignments")
    all_sites = models.BooleanField(default=False)
    site_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    all_brands = models.BooleanField(default=False)
    brand_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    legacy_grant = models.OneToOneField(
        RoleGrant, null=True, blank=True, on_delete=models.PROTECT, related_name="replacement_assignment"
    )
    legacy_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT,
        related_name="migrated_role_assignments",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gt=models.F("effective_from")),
                name="ck_roleassignment_period",
            ),
            models.CheckConstraint(
                condition=models.Q(all_sites=False) | models.Q(site_ids=[]),
                name="ck_roleassignment_site_flag",
            ),
            models.CheckConstraint(
                condition=models.Q(all_brands=False) | models.Q(brand_ids=[]),
                name="ck_roleassignment_brand_flag",
            ),
            models.UniqueConstraint(
                fields=["tenant", "legacy_user"],
                condition=models.Q(legacy_grant__isnull=True, legacy_user__isnull=False),
                name="uq_roleassignment_user_fallback",
            ),
        ]
        indexes = [models.Index(fields=["human", "effective_from", "effective_to"])]

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.tenant_id is not None:
            if self.human_id and not HumanIdentity.objects.filter(
                pk=self.human_id, tenant_id=self.tenant_id
            ).exists():
                errors["human"] = "Human identity must belong to the assignment tenant."
            if self.role_id and not RoleAssignment._role_in_tenant(self.role_id, self.tenant_id):
                errors["role"] = "Role must belong to the assignment tenant."
            if self.legacy_grant_id and not RoleGrant.objects.filter(
                pk=self.legacy_grant_id, tenant_id=self.tenant_id
            ).exists():
                errors["legacy_grant"] = "Source grant must belong to the assignment tenant."
            if self.legacy_user_id and not RoleAssignment._user_in_tenant(
                self.legacy_user_id, self.tenant_id
            ):
                errors["legacy_user"] = "Source login must belong to the assignment tenant."
            for key, ids, model in (
                ("site_ids", self.site_ids, "site"),
                ("brand_ids", self.brand_ids, "brand"),
            ):
                if len(ids) != len(set(ids)) or any(item <= 0 for item in ids):
                    errors[key] = "Selected IDs must be positive and unique."
                elif ids:
                    from masters.models import Brand, Store

                    target = Store if model == "site" else Brand
                    if target.objects.filter(tenant_id=self.tenant_id, pk__in=ids).count() != len(ids):
                        errors[key] = "Every selected ID must belong to the assignment tenant."
        if errors:
            raise ValidationError(errors)

    @staticmethod
    def _role_in_tenant(role_id: int, tenant_id: uuid.UUID) -> bool:
        from accounts.models import Role

        return Role.objects.filter(pk=role_id, tenant_id=tenant_id).exists()

    @staticmethod
    def _user_in_tenant(user_id: int, tenant_id: uuid.UUID) -> bool:
        from accounts.models import User

        return User.objects.filter(pk=user_id, tenant_id=tenant_id).exists()

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.full_clean()
        super().save(*args, **kwargs)
