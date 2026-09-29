from __future__ import annotations

from django.apps import AppConfig


class OffersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "offers"

    def ready(self) -> None:
        # An approved offer publishes itself (D11 §7). Registered here so the API,
        # the shell and a management command all reach the same callback.
        from offers.approval import register

        register()
