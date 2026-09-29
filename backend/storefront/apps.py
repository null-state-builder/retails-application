from __future__ import annotations

from django.apps import AppConfig


class StorefrontConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "storefront"

    def ready(self) -> None:
        # Store operations ticket 49 (ST-OPS-4): a checklist whose time has passed
        # is found by time passing, on the worker's clock.
        from core.outbox import register_scheduled
        from storefront import checklists

        register_scheduled("store_checklists", checklists.run_check)
