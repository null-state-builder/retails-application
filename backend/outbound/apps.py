from django.apps import AppConfig


class OutboundConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "outbound"

    def ready(self) -> None:
        """Say which outbound document is posted *by* its own approval (#138).

        Only damage flags are: a store person may report damage but not move
        the stock, so the warehouse's confirmation has to be what posts it.
        Registered here, at app-ready, so every route to a decision — API,
        shell, management command — goes through the same call.
        """
        from approvals.hooks import register_on_approved
        from outbound.models import MarkDamaged
        from outbound.posting import confirm_mark_damaged

        register_on_approved(MarkDamaged, confirm_mark_damaged)

        # Goods-v1: a release gives held stock back, so it is decided by a
        # distinct `movement.approve` checker through the generic approvals
        # command (E234); the subject effects live in `goods_movements`.
        from outbound.goods_movements import register_approval_handlers

        register_approval_handlers()

        # Goods ticket 15H: the Owner's decision on a prepared RTV shortfall
        # closure goes through the same approvals command, under the RTV policy.
        from outbound.goods_rtv_shipments import (
            register_approval_handlers as register_shortfall_closure,
        )

        register_shortfall_closure()

        # Goods ticket 17: the count's database guards answer with their own
        # refusals, and an idle pass becomes owned work on the worker's clock -
        # idleness is found by time passing, never by an action.
        from core.outbox import register_scheduled
        from outbound import goods_counts

        goods_counts.install()
        register_scheduled("count_staleness", goods_counts.sweep_stale)

        from outbound import goods_soh_reconciliation

        goods_soh_reconciliation.install()
        from outbound import transfer_authority

        transfer_authority.install()
        from outbound import count_review

        count_review.install()

        # Store operations ticket 35 (ST-INV-3): a scheduled count whose day has
        # passed is found by time passing, on the same clock.
        from outbound import count_schedules

        register_scheduled("count_schedules", count_schedules.run_check)
