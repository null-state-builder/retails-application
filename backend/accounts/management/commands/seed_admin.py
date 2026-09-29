from __future__ import annotations

from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Idempotently seed the admin and demo foundation accounts."

    def handle(self, *args: Any, **options: Any) -> None:
        call_command("seed_foundation")
