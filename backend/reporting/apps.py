from __future__ import annotations

from django.apps import AppConfig


class ReportingConfig(AppConfig):
    """Reports (store operations PRD §17): read across the domain apps, imported by none."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "reporting"

    def ready(self) -> None:
        from core.outbox import register_scheduled
        from reporting import (
            brand_reports,
            brand_sale_facts,
            exception_facts,
            funding_facts,
            gift_facts,
            gst_facts,
            inventory_facts,
            margin_facts,
            offer_sim_facts,
            shrinkage_facts,
        )
        from reporting.sales_facts import scheduled_refresh

        # The reporting copy is refreshed on the worker's clock, never while a
        # report page loads and never inside a bill (R-AN-003, §27).
        register_scheduled("sales_report_refresh", scheduled_refresh)
        # Ticket 30: the offer simulation's copy of sold lines, on the same clock.
        register_scheduled("offer_sim_refresh", offer_sim_facts.scheduled_refresh)
        register_scheduled("gst_report_refresh", gst_facts.scheduled_refresh)
        # Ticket 14: the gift stock report's copy of the gift tags.
        register_scheduled("gift_report_refresh", gift_facts.scheduled_refresh)
        # Ticket 25: the discount funding split's copy, on the same clock.
        register_scheduled("discount_funding_refresh", funding_facts.scheduled_refresh)
        # Ticket 44: approved shrinkage, rebuilt whole on the same clock.
        register_scheduled("shrinkage_report_refresh", shrinkage_facts.scheduled_refresh)
        # Ticket 43: the day's stock snapshot and pieces received, on the same clock.
        register_scheduled("inventory_report_refresh", inventory_facts.scheduled_refresh)
        # Ticket 27: the margin share split's copy, on the same clock.
        register_scheduled("margin_share_refresh", margin_facts.scheduled_refresh)
        # Ticket 48: the counter's exceptions, rebuilt whole on the same clock.
        register_scheduled("exceptions_report_refresh", exception_facts.scheduled_refresh)
        # Ticket 29: bill lines by brand for the brands' Sale reports, on the same
        # clock; then last month's brand reports, once both copies have passed its end.
        register_scheduled("brand_sales_refresh", brand_sale_facts.scheduled_refresh)
        register_scheduled("brand_reports_monthly", brand_reports.scheduled_monthly)
