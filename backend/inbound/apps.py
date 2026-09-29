from __future__ import annotations

from django.apps import AppConfig


class InboundConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inbound"
    verbose_name = "Inbound / Receiving"

    def ready(self) -> None:
        # Goods-v1 counter-GRN and disposition approvals decide through the generic
        # approvals command (E234); the subject effects live here (design §3.1).
        from inbound.debit_notes import register_approval_hooks
        from inbound.goods_services import register_approval_handlers

        register_approval_handlers()
        # Ticket 38: the Owner approves a debit note in the shared approvals inbox;
        # each decision is recorded against the note by its own audited command.
        register_approval_hooks()
