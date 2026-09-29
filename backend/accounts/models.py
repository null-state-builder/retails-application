"""Users, configurable roles, and the data-scope dimension (RBAC foundation).

Access is configurable *data*, not code (Rule 12): a `Role` row carries which
sidebar groups and which page-actions it may use, so a trained admin can add or
retune a role with no release. The deep permission matrix and second-eye spine
land with their slice; this is the working set the foundation needs.

Scope is a separate dimension from role (ADR-0003): a user is scoped `all /
entity / region / store_group / store` against the `LegalEntity → GSTIN → Store`
hierarchy, never by minting a per-store role.
"""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.managers import UserManager
from accounts.role_lists import HEAD_OFFICE_VALUE_ACTORS
from core.base import TimeStampedModel
from core.goods_base import InheritedTenantMaster

# Legacy nav groups — the five architecture layers that named the old sidebar.
# The shell navigates by `accounts.sections` since #87; this list survives only
# so existing Role rows and the role-admin API stay readable.
NAV_GROUPS = [
    "home",
    "master_data",
    "documents",
    "ledgers",
    "controls",
    "intelligence",
    "edges_admin",
    "store_ops",
    "outbound",
]


class Role(TimeStampedModel, InheritedTenantMaster):
    """A configurable role → what a user assigned to it can see and do.

    Tenant-owned (change PRD §14.2): ``code`` is unique within the tenant; the
    name is not identity and may repeat."""

    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=240, blank=True, default="")
    landing_page = models.CharField(max_length=60, default="home")
    nav_groups = models.JSONField(default=list)  # subset of NAV_GROUPS (legacy shell)
    # The SIDEBAR RBAC contract (issue #85): {section_code: {capability, label}}
    # over the sections in `accounts.sections`. This is the live authority
    # the login/`/me` payload and the server-side section gate read — editing it
    # retunes access with no release (Rule 12). Seeded from `accounts.rbac_matrix`.
    section_access = models.JSONField(default=dict)
    # Protected-field policy is independent of the section ladder. The keys
    # are validated by the unified access editor and interpreted by its field
    # projector; assignments carry only role identity and scope.
    field_access = models.JSONField(default=list)
    permissions_map = models.JSONField(default=dict)  # fine-grained page-actions, later
    is_system = models.BooleanField(default=False)  # protected from deletion
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uq_role_tenant_code"),
        ]

    def __str__(self) -> str:
        return self.name


class ActorPolicy(TimeStampedModel):
    """The live role set allowed to perform one named business action.

    Sections answer whether a user may enter and operate a part of the product.
    Actor policies answer the narrower questions the capability ladder cannot:
    for example, who may inward a PT versus who may prepare one.  The immutable
    floor rules are intentionally absent from this row and remain engine rules.
    """

    action = models.CharField(max_length=100, unique=True)
    label = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True, default="")
    roles = ArrayField(models.CharField(max_length=40), default=list)

    class Meta:
        ordering = ["action"]
        db_table = "accounts_actor_policy"
        verbose_name_plural = "actor policies"

    def __str__(self) -> str:
        return self.label


class AccessChange(TimeStampedModel):
    """A proposed Setup mutation that only a second administrator may apply."""

    class Resource(models.TextChoices):
        ROLE = "role", "Role"
        USER = "user", "User"
        ACTOR_POLICY = "actor_policy", "Actor policy"
        APPROVAL_POLICY = "approval_policy", "Approval policy"

    class Operation(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"

    resource = models.CharField(max_length=24, choices=Resource.choices)
    operation = models.CharField(max_length=12, choices=Operation.choices)
    target_id = models.BigIntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    summary = models.CharField(max_length=240)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="access_changes_created",
    )
    applied_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="access_changes_applied",
    )
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        db_table = "accounts_access_change"

    def __str__(self) -> str:
        return self.summary


class ScopeType(models.TextChoices):
    ALL = "all", "All (network-wide)"
    ENTITY = "entity", "Legal entity"
    REGION = "region", "Region / state"
    STORE_GROUP = "store_group", "Store group"
    STORE = "store", "Single store"
    # A brand manager cuts *across* stores: their scope is the brands they are
    # assigned, network-wide (#88). The top bar gives them a brand filter where
    # everyone else gets a unit list.
    BRAND = "brand", "Assigned brands (across stores)"


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    username = models.CharField(max_length=60, unique=True)
    full_name = models.CharField(max_length=120, blank=True, default="")
    role = models.ForeignKey(
        Role, null=True, blank=True, on_delete=models.SET_NULL, related_name="users"
    )
    # Fail-closed default: a user created without an explicit scope sees the
    # narrowest scope (its own stores — nothing until stores are assigned), never
    # the whole network. Network-wide access must be granted deliberately.
    scope_type = models.CharField(max_length=20, choices=ScopeType.choices, default=ScopeType.STORE)
    entity = models.ForeignKey(
        "masters.LegalEntity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="users",
    )
    stores = models.ManyToManyField("masters.Store", blank=True, related_name="users")
    # Only meaningful for `scope_type = brand`: the brands this person owns.
    # Empty ⇒ they see no stock at all, never every brand — the same fail-closed
    # rule as a store-scoped user with no stores.
    brands = models.ManyToManyField("masters.Brand", blank=True, related_name="users")
    # The manager's override PIN at a till that has no network (D10, grill Q1).
    # A *hash*, and only ever a hash: it is synced down to the store's own till in
    # its dataset (#179) so a manager can authorise an over-cap discount with the
    # line cut, and a secret that leaves the building on a shop-floor device must
    # not be reversible. Blank means "this person has no counter PIN", which is
    # the default and is not a credential - the dataset leaves them out entirely.
    till_pin_hash = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    # GSA-T03/ticket 03A: an administrator-issued temporary password (create or
    # assisted reset). True until this login's own change-password succeeds; a
    # restricted session may only reach its own session lifecycle and that
    # endpoint until it clears (`accounts.authentication`).
    must_change_password = models.BooleanField(default=False)
    # Goods-v1 login (design §4.2): an email and a stable person. Neither is ever
    # guessed for an existing row; a login without both cannot sign in.
    tenant = models.ForeignKey(
        "masters.Tenant", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    human = models.OneToOneField(
        "accounts.HumanIdentity",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="login",
    )
    email = models.CharField(max_length=254, null=True, blank=True)  # noqa: DJ001 - no guessed email

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        ordering = ["username"]
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                condition=models.Q(email__isnull=False),
                name="uq_user_email_lower",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name or self.username

    @property
    def may_post_pt_or_vflip_floor(self) -> bool:
        """Legacy unscoped posting floor, now answered by role assignments.

        Resource-specific postings should use the unified evaluator with the
        document's site and brand. This compatibility property can grant only
        a full-network Owner or Accounts assignment in the bound tenant.
        """
        from accounts.principal import access_for_user
        from core.tenancy import current_tenant_id

        if current_tenant_id() is None or current_tenant_id() != self.tenant_id:
            return False
        return bool(access_for_user(self).grants_with_roles(
            "section.money.approve", [(None, None)], HEAD_OFFICE_VALUE_ACTORS,
        ))


class LoginAttempt(models.Model):
    """Lightweight brute-force guard: N failures inside the window locks login."""

    identifier = models.CharField(max_length=120, unique=True)  # username
    failures = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.identifier}: {self.failures} fails"


# Goods-v1 people, sessions and grants (design §5.2), registered with this app.
from accounts.goods_models import (  # noqa: E402, F401
    AuthenticationFailure,
    HumanIdentity,
    RoleGrant,
    RoleAssignment,
    SecurityGuard,
    ServerSession,
    Staff,
    StaffAssignment,
)
