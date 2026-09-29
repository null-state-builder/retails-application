from __future__ import annotations

from django.apps import AppConfig


class FilesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "files"
    verbose_name = "Stored Files"

    def ready(self) -> None:
        # The durable-export job (design §4.4). `files` owns producing and
        # protecting the bytes; each domain registers what its own export
        # contains (`register_export_kind`), so this never imports a domain.
        from core.outbox import register_job_handler
        from files.goods_exports import run_export

        register_job_handler("export", run_export)
