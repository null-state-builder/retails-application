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

        # Store test run: a staged opening source is loaded by a durable job, not a web thread.
        from core.outbox import register_job_handler
        from ptmapper.pt_opening_services import JOB, run_load

        assert JOB == "opening_load"
        register_job_handler("opening_load", run_load)

        # A test store's opening decided with no second person when its policy says so
        # (ptmapper.opening_auto, built alongside; registered only where it exists).
        from importlib import import_module
        from importlib.util import find_spec

        if find_spec("ptmapper.opening_auto") is not None:
            import_module("ptmapper.opening_auto").register()
