"""Tenant-bound authority and legacy ownership for the Money book APIs.

The old ledger tables lack a tenant column. Their required business links, or
the posting/upload actor where that link is optional, identify the tenant.
Rows with no owner or conflicting links remain in the database for SO-12
reconciliation but are never served or mutated through these APIs.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.db.models import Q, QuerySet
from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from accounts.principal import resolve_access
from accounts.role_assignments import effective_assignments
from accounts.unified_policy import role_actions, role_fields
from core.commands import database_now
from core.gl import GLEntry
from core.tenancy import require_tenant_id
from finledger.models import (
    BankStatementImport,
    BankStatementLine,
    CashLedgerEntry,
    PartnerLedgerEntry,
    VendorLedgerEntry,
)


def keeps_books(user: Any) -> bool:
    """One active, tenant-wide assignment must hold Money manage and financial."""
    tenant_id = require_tenant_id()
    human_id = getattr(user, "human_id", None)
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or human_id is None
        or getattr(user, "tenant_id", None) != tenant_id
    ):
        return False
    return any(
        assignment.all_sites
        and assignment.all_brands
        and "section.money.manage" in role_actions(assignment.role)
        and "financial" in role_fields(assignment.role)
        for assignment in effective_assignments(human_id, database_now())
    )


class IsBooksKeeper(BasePermission):
    message = "You do not have access to the Money books."

    def has_permission(self, request: Request, view: Any) -> bool:
        return keeps_books(request.user)


def guarded_book_write(view_method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep the legacy Money write and its authority check in one transaction.

    The route's permission class is the early gate. This guard reloads the
    server session and replays the same indivisible Money predicate under the
    person's security lock before and after the business mutation.
    """

    @wraps(view_method)
    def wrapped(view: Any, request: Request, *args: Any, **kwargs: Any) -> Any:
        access = resolve_access(request)
        with access.guard_legacy_write(lambda current: keeps_books(current.user)):
            return view_method(view, request, *args, **kwargs)

    return wrapped


def vendor_entries() -> QuerySet[VendorLedgerEntry]:
    tenant_id = require_tenant_id()
    return VendorLedgerEntry.objects.filter(vendor__tenant_id=tenant_id).filter(
        Q(posted_by__isnull=True) | Q(posted_by__tenant_id=tenant_id)
    )


def cash_entries() -> QuerySet[CashLedgerEntry]:
    tenant_id = require_tenant_id()
    return CashLedgerEntry.objects.filter(
        Q(vendor__tenant_id=tenant_id, posted_by__isnull=True)
        | Q(vendor__tenant_id=tenant_id, posted_by__tenant_id=tenant_id)
        | Q(vendor__isnull=True, posted_by__tenant_id=tenant_id)
    )


def partner_entries() -> QuerySet[PartnerLedgerEntry]:
    tenant_id = require_tenant_id()
    return PartnerLedgerEntry.objects.filter(store__tenant_id=tenant_id).filter(
        Q(posted_by__isnull=True) | Q(posted_by__tenant_id=tenant_id)
    )


def gl_entries() -> QuerySet[GLEntry]:
    """A GL leg needs at least one stable tenant link and no conflicting link."""
    tenant_id = require_tenant_id()
    return (
        GLEntry.objects.filter(Q(store__isnull=True) | Q(store__tenant_id=tenant_id))
        .filter(Q(gstin__isnull=True) | Q(gstin__tenant_id=tenant_id))
        .filter(Q(posted_by__isnull=True) | Q(posted_by__tenant_id=tenant_id))
        .filter(
            Q(store__tenant_id=tenant_id)
            | Q(gstin__tenant_id=tenant_id)
            | Q(posted_by__tenant_id=tenant_id)
        )
    )


def bank_imports() -> QuerySet[BankStatementImport]:
    tenant_id = require_tenant_id()
    return (
        BankStatementImport.objects.filter(file__kind="bank_statement")
        .filter(Q(uploaded_by__isnull=True) | Q(uploaded_by__tenant_id=tenant_id))
        .filter(Q(file__uploaded_by__isnull=True) | Q(file__uploaded_by__tenant_id=tenant_id))
        .filter(
            Q(uploaded_by__tenant_id=tenant_id)
            | Q(file__uploaded_by__tenant_id=tenant_id)
        )
    )


def bank_lines() -> QuerySet[BankStatementLine]:
    tenant_id = require_tenant_id()
    return (
        BankStatementLine.objects.filter(import_batch__in=bank_imports())
        .filter(
            Q(matched_entry__isnull=True)
            | Q(matched_entry__in=cash_entries())
        )
        .filter(Q(matched_by__isnull=True) | Q(matched_by__tenant_id=tenant_id))
    )
