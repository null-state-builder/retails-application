"""Refresh every connected mailbox.

Exists so that the day this backend grows a scheduler (Render cron, or the
worker a dozen other features want), switching mail from "syncs when somebody
opens the app" to "syncs on its own" is a cron line and not a rewrite:

    python manage.py sync_mail

Safe to run as often as you like - :func:`sync.sync_account` collapses repeats
inside its own window unless ``--force`` says otherwise.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from mail import crypto
from mail.models import MailAccount, MailAccountStatus
from mail.sync import sync_account


class Command(BaseCommand):
    help = "Pull new mail for every connected mailbox."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignore the rate-limit window and sync every account now.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not crypto.is_configured():
            self.stdout.write(self.style.WARNING("Mail is not configured on this server."))
            return

        accounts = MailAccount.objects.filter(status=MailAccountStatus.CONNECTED)
        if not accounts:
            self.stdout.write("No connected mailboxes.")
            return

        for account in accounts:
            written = sync_account(account, force=bool(options.get("force")))
            if account.last_sync_error:
                self.stdout.write(
                    self.style.WARNING(f"{account.email_address}: {account.last_sync_error}")
                )
            else:
                self.stdout.write(f"{account.email_address}: {written} message(s)")
