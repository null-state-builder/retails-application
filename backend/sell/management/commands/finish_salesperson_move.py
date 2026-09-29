"""Drop the old salesperson table once every old row is matched (ticket 07, §29).

Migration 0029 drops it itself when nothing is left unmatched at deploy. When
Admin still had rows to resolve, the table stays until this is run, and this
refuses for as long as any row is unmatched. Safe to run again: it says so when
the table is already gone.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Drop the old salesperson table, only once every old row has a staff record."

    def handle(self, *args: Any, **options: Any) -> None:
        from sell.services.salesperson_move import OldTableStillNeeded, drop_old_table

        try:
            with transaction.atomic(), connection.cursor() as cursor:
                dropped = drop_old_table(cursor)
        except OldTableStillNeeded as waiting:
            raise CommandError(str(waiting)) from None
        if dropped:
            self.stdout.write(self.style.SUCCESS("The old salesperson table is dropped."))
        else:
            self.stdout.write("The old salesperson table is already gone.")
