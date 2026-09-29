"""Bind this deployment to its one tenant and create the first administrator.

Protected setup, not a public endpoint (design §8.1). The administrator password
comes from the ``KDPS_BOOTSTRAP_ADMIN_PASSWORD`` environment variable, never a
command-line argument that would land in shell history. Re-running with the same
deployment key and tenant code does nothing; a different code is refused.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from accounts.goods_setup import bootstrap_deployment
from core.refusals import Refusal


class Command(BaseCommand):
    help = "Bind the deployment key to a tenant and create the first administrator (idempotent)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--deployment-key", default=None)
        parser.add_argument("--code", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--timezone", default="Asia/Kolkata")
        parser.add_argument("--currency", default="INR")
        parser.add_argument("--locale", default="en-IN")
        parser.add_argument("--synthetic", action="store_true")
        parser.add_argument("--admin-email", required=True)
        parser.add_argument("--admin-name", required=True)
        parser.add_argument("--admin-staff-code", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        password = os.environ.get("KDPS_BOOTSTRAP_ADMIN_PASSWORD", "")
        if len(password) < 8:
            raise CommandError("Set KDPS_BOOTSTRAP_ADMIN_PASSWORD (at least 8 characters).")
        key = options["deployment_key"] or settings.KDPS_DEPLOYMENT_KEY
        try:
            deployment_key = uuid.UUID(str(key))
        except ValueError as exc:
            raise CommandError("The deployment key must be a UUID.") from exc
        try:
            tenant = bootstrap_deployment(
                deployment_key=deployment_key,
                code=options["code"],
                name=options["name"],
                timezone_name=options["timezone"],
                currency=options["currency"],
                locale=options["locale"],
                synthetic=bool(options["synthetic"]),
                admin_email=options["admin_email"],
                admin_name=options["admin_name"],
                admin_staff_code=options["admin_staff_code"],
                admin_password=password,
            )
        except Refusal as refusal:
            raise CommandError(refusal.message) from refusal
        self.stdout.write(f"Deployment {deployment_key} serves tenant {tenant.code}.")
