from django.apps import AppConfig


class PtmapperConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ptmapper"

    def ready(self) -> None:
        # Goods-v1 receipt PT approval and reversal decisions (design E234).
        from ptmapper.goods_pt_services import install as install_goods_pt

        install_goods_pt()

        # GSA-T10: opening manifest, variance and opening PT approval decisions.
        from ptmapper.goods_manifest_services import install as install_goods_manifest

        install_goods_manifest()

        from ptmapper.soh_services import install as install_soh

        install_soh()
