from django.apps import AppConfig


class SellConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sell"

    def ready(self) -> None:
        """The two side effects this app needs at start-up.

        `signals` wires the deferred-costing sweep to the two things that can
        release a bill nobody could cost on the day (#186) - imported for its side
        effect, which is what `ready()` is for.

        The `declare_reversal` calls say what cancelling each of this app's
        documents undoes (#220). They live here rather than on the models because
        the cascade lives in a service that reads the models, and a model naming
        its own cascade would close a real import cycle.
        """
        from core.reversal import declare_reversal, reverse_value_legs
        from sell import signals  # noqa: F401
        from sell.models import CreditNote, Return, Sale
        from sell.services.cancel import reverse_sell_document

        declare_reversal(Sale._meta.label_lower, reverse_sell_document)
        declare_reversal(Return._meta.label_lower, reverse_sell_document)
        # A note posts no value of its own - the liability is raised by the bill or
        # return that issued it, and mirrored when *that* document is cancelled, so
        # the note's own reversal is empty. Declared all the same, because "this
        # reverses nothing" has to be a statement somebody made rather than an
        # omission nobody noticed.
        declare_reversal(CreditNote._meta.label_lower, reverse_value_legs)

        # Ticket 42: the Owner's decision on a petty cash spend over the limit.
        from sell.services.petty_cash import register_approval_hooks

        register_approval_hooks()

        # Ticket 20: customer reservations past their collect-by date end on the
        # worker's clock, and their expiry alerts refresh with them.
        from core.outbox import register_scheduled
        from sell.services.reservations import scheduled_expiry

        register_scheduled("customer_reservation_expiry", scheduled_expiry)

        # Ticket 19: what expired gift vouchers still hold is written off on the
        # worker's clock, with no GST.
        from sell.services.gift_vouchers import scheduled_expiry as gift_voucher_expiry

        register_scheduled("gift_voucher_expiry", gift_voucher_expiry)
