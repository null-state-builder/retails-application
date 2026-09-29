from __future__ import annotations

from django.apps import AppConfig


class VendorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vendors"
    verbose_name = "Vendors & Bookings"

    def ready(self) -> None:
        # An approved booking is a placed booking (D11 §3).
        from vendors.approval import register

        register()
        # Ticket 39: the Owner's decisions on bookings over open-to-buy.
        from vendors.open_to_buy import register_approval_hooks

        register_approval_hooks()
