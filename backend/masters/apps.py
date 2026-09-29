from __future__ import annotations

from django.apps import AppConfig


class MastersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "masters"
    verbose_name = "Master Data"

    def ready(self) -> None:
        # A configuration draft becomes a frozen ConfigVersion only on approval
        # (design §3.1, §6.1) - approvals never imports masters, so masters
        # registers the handler here in the project composition layer.
        from approvals.goods_services import register_subject_handler
        from masters.goods_identity_services import install
        from masters.goods_services import handle_configuration_approval

        register_subject_handler(
            "configuration", "config.approve", handle_configuration_approval, policy_governed=False
        )
        # Goods-v1 master proposals are decided through the one approvals command
        # (E234); their conflict constraints refuse as MASTER_CONFLICT.
        install()
