from __future__ import annotations

from django.apps import AppConfig


class AlertsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "alerts"

    def ready(self) -> None:
        from alerts.goods_health import run_health_checks
        from alerts.goods_services import deliver_notification
        from core.outbox import register_job_handler, register_scheduled

        register_job_handler("notification", deliver_notification)
        # Health checks and evidence verification run on the worker's anchoring
        # clock, never on a monitoring page load (GSA-T18/ticket 19).
        register_scheduled("operations_health", run_health_checks)
