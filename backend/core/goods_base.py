"""Abstract bases for every goods-v1 table (design §5.1).

Two shapes cover the whole new schema:

* ``TenantOwned`` - a UUID row that belongs to exactly one tenant. The schema
  guard installer (``core.schema_guards``) gives every concrete subclass forced
  row-level security, ``UNIQUE(tenant_id, id)`` and same-tenant composite foreign
  keys, so no table can be added without its tenant wall.
* ``EvidenceRow`` - an immutable fact written by a command: who (a human or a
  named service, never both), which command, business time, recorded time and a
  chained SHA-256. The kernel fills these; callers never do.

``PROTECTION`` tells the installer which grants and triggers a table receives:
``projection`` rows are rebuildable and mutable by their owning writer,
``append_only`` rows are INSERT-only, ``journal`` rows are insertable only by the
ledger role's append function.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.db.models.functions import Now

PROJECTION = "projection"
APPEND_ONLY = "append_only"
JOURNAL = "journal"


class CurrentTenant(models.Func):
    """The database's trusted tenant for this connection (``kdps_current_tenant()``)."""

    function = "kdps_current_tenant"
    template = "%(function)s()"
    output_field = models.UUIDField()


class InheritedTenantMaster(models.Model):
    """Ownership for a legacy master that goods-v1 inherits (change PRD §14.2).

    Roles, vendors, brands, legal entities, registrations and stores keep their
    integer identities and history, but each row has one non-null tenant. The
    column defaults to the connection's trusted tenant, so an existing writer
    cannot choose it, and a write with no tenant bound fails. The schema guard
    installer gives these tables the same wall as ``TenantOwned`` tables.
    """

    tenant = models.ForeignKey(
        "masters.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
        db_default=CurrentTenant(),
    )

    class Meta:
        abstract = True


class TenantOwned(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("masters.Tenant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(db_default=Now())

    PROTECTION: ClassVar[str] = PROJECTION

    class Meta:
        abstract = True


class EvidenceRow(TenantOwned):
    command_key = models.ForeignKey("core.CommandKey", on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(
        "accounts.HumanIdentity", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    service_code = models.CharField(max_length=60, null=True, blank=True)
    event_at = models.DateTimeField()
    recorded_at = models.DateTimeField(db_default=Now())
    previous_hash = models.CharField(max_length=64, null=True, blank=True)
    row_hash = models.CharField(max_length=64)

    PROTECTION: ClassVar[str] = APPEND_ONLY
    #: Columns the chained hash leaves out (set by the kernel after hashing).
    HASH_EXCLUDED: ClassVar[frozenset[str]] = frozenset({"previous_hash", "row_hash", "created_at"})

    class Meta:
        abstract = True
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(actor__isnull=False, service_code__isnull=True)
                    | models.Q(actor__isnull=True, service_code__isnull=False)
                ),
                name="%(app_label)s_%(class)s_one_actor",
            ),
        ]
