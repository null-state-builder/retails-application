"""Check a migrated blank installation without adding demo or business data."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from accounts.registration_services import public_state


class Command(BaseCommand):
    help = "Check one-time signup availability; never seed company, staff, stock or policy."

    def handle(self, *args: Any, **options: Any) -> None:
        state = public_state()
        if not state["available"]:
            raise CommandError(state["message"])
        self.stdout.write(
            "Blank installation ready for jointly confirmed company signup; no demo data was seeded."
        )
