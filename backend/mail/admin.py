"""Admin for mail - deliberately thin.

Accounts are listed so an operator can see who has connected and who is stuck on
"needs reconnecting", which is the only real support question this feature
generates. Messages are *not* registered at all: an admin screen over the
message table would be a way for a superuser to read any employee's private
correspondence with a two-click search, and nothing in KDPS needs that. If a
row ever has to be inspected it is a database query with a reason behind it, not
a menu item.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest

from .models import MailAccount


@admin.register(MailAccount)
# `ModelAdmin` is generic to django-stubs but not subscriptable at runtime here
# (same gap as `core.money.MoneyField`'s base) - subscripting it crashes Django's
# admin autodiscover, so the generic arg is left off rather than "fixed".
class MailAccountAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("user", "email_address", "status", "last_synced_at")
    list_filter = ("status",)
    search_fields = ("user__username", "email_address")
    # The token columns are excluded rather than made read-only: read-only still
    # renders the ciphertext on the page, and there is no reason for it to be on
    # a screen at all.
    exclude = ("refresh_token_enc", "access_token_enc")

    def has_add_permission(self, request: HttpRequest) -> bool:
        """An account only ever exists because a person consented at Google.
        Hand-creating one here would produce a row with no token that reports
        itself connected."""
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> tuple[str, ...]:
        return ("user", "email_address", "history_id", "last_synced_at", "last_sync_error")
