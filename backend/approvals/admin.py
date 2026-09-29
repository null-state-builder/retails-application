"""Admin for the maker-checker spine (#70).

``ApprovalPolicy`` is editable because that is the whole point of it: the
tolerance and the value band are business numbers the owner retunes as stores
and volumes change, not constants that need a release (Rule 12).

``Approval`` is read-only. It is an audit record — who asked, who decided, when
and why — and a record you can quietly edit in the admin is not a record. The
one write path is ``approvals.services``.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin

from approvals.models import Approval, ApprovalPolicy, ApprovalRoute, ApprovalStepDecision


@admin.register(ApprovalRoute)
# `ModelAdmin` is generic to django-stubs but not subscriptable at runtime here
# (same gap as `core.money.MoneyField`'s base) - subscripting it crashes Django's
# admin autodiscover, so the generic arg is left off rather than "fixed".
class ApprovalRouteAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Read-only here, deliberately.

    The chain is data (Rule 12) and is meant to be retuned — but the steps of a
    live route decide who may approve what, so retuning them belongs behind the
    same audited access-change path the approval *policies* already use, not in
    a raw JSON textarea. Until that screen exists a migration is the honest way
    to change one; this page is for reading what is configured.
    """

    list_display = ("kind", "step_count")
    readonly_fields = ("created_at", "updated_at")

    def has_add_permission(self, request: Any) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return False


@admin.register(ApprovalStepDecision)
# See the type-ignore note on `ApprovalRouteAdmin` above.
class ApprovalStepDecisionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    """Read-only for the same reason ``Approval`` is: it is the audit trail."""

    list_display = ("approval", "step_order", "step_label", "decided_by", "short_circuited")
    list_filter = ("short_circuited",)

    def has_add_permission(self, request: Any) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return False


@admin.register(ApprovalPolicy)
# See the type-ignore note on `ApprovalRouteAdmin` above.
class ApprovalPolicyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("kind", "tolerance_paise", "band_paise", "band_roles", "escalated_roles")
    readonly_fields = ("created_at", "updated_at")

    def has_add_permission(self, request: Any) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return False


@admin.register(Approval)
# See the type-ignore note on `ApprovalRouteAdmin` above.
class ApprovalAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("kind_label", "title", "status", "made_by", "decided_by", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("title",)
    date_hierarchy = "created_at"

    def has_add_permission(self, request: Any) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return False
