"""Fail the gate when the database disagrees with the migration graph.

The complement to `makemigrations --check`, which only ever compares models to
migrations. See `core/dbdrift` for what "disagrees" means and why the third
corner of that triangle is the one that bit us (issue #93).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from core.dbdrift import find_drift


class Command(BaseCommand):
    help = "Compare the database schema against the migration graph and report any drift."

    def handle(self, *args: Any, **options: Any) -> None:
        findings = find_drift(connection)
        if not findings:
            self.stdout.write(
                self.style.SUCCESS("No schema drift: the database matches the migrations.")
            )
            return

        for finding in findings:
            self.stderr.write(self.style.ERROR(str(finding)))
        raise CommandError(
            f"{len(findings)} schema drift finding(s) - the database is not what the "
            "migrations say it is. Each line above names the table and column."
        )
