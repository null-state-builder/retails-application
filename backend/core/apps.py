"""The `core` app is the kernel: money type, append-only ledgers, the posting
engine, docstatus FSM, naming series, scoping. Empty at K0 — it gains its first
real primitive (MoneyField + the append-only LedgerEntry base) at K1.

Goods-v1 adds two start-up duties: every served connection switches to the
restricted application role (``core.dbroles``), and every ``migrate`` finishes
by installing the tenant walls and write protection the goods tables need
(``core.schema_guards``)."""

from __future__ import annotations

from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _install_guards(sender: Any, **kwargs: Any) -> None:
    if getattr(sender, "label", "") != "core":
        return
    from django.db import connection

    from core.schema_guards import install_goods_schema_guards

    if connection.vendor != "postgresql":
        return
    install_goods_schema_guards()


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        from core.dbroles import install_connection_hook

        install_connection_hook()
        post_migrate.connect(_install_guards, dispatch_uid="kdps-goods-schema-guards")

        from core.outbox import JobOutcome, register_job_handler

        def _publish_ceiling(intent: Any) -> JobOutcome:
            from core.numbering import publish_ceiling

            spec = (intent.payload or {}).get("ceiling_spec") or {}
            ceiling = publish_ceiling(
                intent.tenant_id, int(spec["series_id"]), int(spec["ceiling"])
            )
            return JobOutcome("confirmed", provider_ref=f"ceiling:{ceiling}")

        register_job_handler("ceiling", _publish_ceiling)
