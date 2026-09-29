"""Turn the store-operations features on for the demo stores, so the screens show.

Every store-operations feature is off until Admin switches it on (ST-OPS-6, B3), so
a freshly seeded demo shows almost none of the store screens. This switches them
all on, at every active store of every **synthetic** tenant, for looking at the UI.

    uv run python manage.py seed_store_features            # switch everything on
    uv run python manage.py seed_store_features --off      # put it all back to the defaults

A real (non-synthetic) tenant is never touched: its gated features stay locked
until Anand closes the gate (B2). Demo probes are skipped. Idempotent.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Switch every store-operations feature on at the synthetic demo stores."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--off",
            action="store_true",
            help="Switch every feature back to its registered default instead.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from core.tenancy import tenant_context
        from masters.goods_models import Tenant
        from masters.models import Store
        from masters.store_feature_models import StoreFeatureSwitch
        from masters.store_feature_registry import FEATURES

        features = [f for f in FEATURES if not f.key.startswith("demo")]
        for tenant in Tenant.objects.filter(synthetic=True):
            with tenant_context(tenant.pk), transaction.atomic():
                stores = Store.objects.filter(tenant_id=tenant.pk, is_active=True)
                for store in stores:
                    for feature in features:
                        wanted = feature.default_on if options["off"] else True
                        StoreFeatureSwitch.objects.update_or_create(
                            tenant_id=tenant.pk,
                            site=store,
                            feature_key=feature.key,
                            defaults={"enabled": wanted, "updated_at": timezone.now()},
                        )
        state = "back to defaults" if options["off"] else "on"
        self.stdout.write(f"Store features {state} at the synthetic demo stores.")
