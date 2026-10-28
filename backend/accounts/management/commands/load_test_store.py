"""Load one functional test store and its linked warehouse, from a legacy SOH or empty.

Synthetic tenant only. See ``accounts.test_store_loader`` for what it does and
the test-only decisions it records.

    python manage.py load_test_store --soh ../docs/VAS-SGMR_SOH_28-09-2026.xlsx

Without ``--soh`` the store is sell-ready with no stock; its opening comes from
its PT files at /goods/opening. ``--setup`` takes the store, the company and the
named people from a file instead of the flags:

    python manage.py load_test_store --setup ../docs/wardrobe-ratu-road/setup.json

``--password-file`` gives every login this run creates its own random password
and writes them (mode 600) with a ready ``KDPS_DEMO_LOGINS`` line; without it
they use the synthetic test password.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Load a functional test store + linked warehouse, from a legacy SOH or empty (synthetic tenant only)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--soh", help="Legacy SOH .xlsx; leave out to open the store empty (PT files at /goods/opening)")
        parser.add_argument("--setup", help="Store setup file (store, warehouse, company, named people)")
        parser.add_argument("--password-file", help="Give new logins random passwords and write them here (mode 600)")
        parser.add_argument("--store", default="VAS-SGMR")
        parser.add_argument("--store-name", default="Vaishnavi Singh More (test)")
        parser.add_argument("--warehouse", default="VAS-WH")
        parser.add_argument("--warehouse-name", default="Vaishnavi Warehouse (test)")
        parser.add_argument("--prefix", default="vas", help="Login prefix for the people created")

    def handle(self, *args: Any, **options: Any) -> None:
        from accounts.goods_demo import owner_user
        from accounts.test_store_loader import Loader, Plan, plan_from_setup
        from core.refusals import Refusal
        from masters.goods_models import Tenant

        path = Path(options["soh"]) if options["soh"] else None
        if path is not None and not path.is_file():
            raise CommandError(f"SOH file not found: {path}")
        tenants = list(Tenant.objects.filter(synthetic=True))
        if len(tenants) != 1:
            raise CommandError("Expected exactly one synthetic tenant on this deployment.")
        if options["setup"]:
            setup = Path(options["setup"])
            if not setup.is_file():
                raise CommandError(f"Setup file not found: {setup}")
            try:
                plan = plan_from_setup(setup)
            except Refusal as exc:
                raise CommandError(f"{setup}: {exc.message}") from exc
        else:
            plan = Plan(store_code=options["store"], store_name=options["store_name"],
                        warehouse_code=options["warehouse"], warehouse_name=options["warehouse_name"],
                        entity_code="syn-vas", entity_name="Vaishnavi Test Retail Pvt Ltd (synthetic)",
                        pan="AAAVT4444D", state_code="20", state_name="Jharkhand (test)", city="Ranchi",
                        prefix=options["prefix"])
        out = Path(options["password_file"]) if options["password_file"] else None
        loader = Loader(tenants[0], plan, path.read_bytes() if path else None, lambda line: self.stdout.write(line),
                        password_for=(lambda _email: secrets.token_urlsafe(12)) if out else None)
        loader.run()
        owner = owner_user(tenants[0])
        if out is not None:
            self._write_passwords(out, plan, loader.created, owner.email if owner else None)
        who = next((p.email for p in plan.people if p.email == loader.email("store")), loader.email("store"))
        self.stdout.write(self.style.SUCCESS(
            f"Done. Sign in as {who} to sell at {plan.store_code}; {owner.email if owner else 'the owner'} approves."))

    def _write_passwords(self, out: Path, plan: Any, created: dict[str, str], owner_email: str | None) -> None:
        """Append the new logins' passwords and a ``KDPS_DEMO_LOGINS`` line for the sign-in chips."""
        if not created:
            self.stdout.write("No new logins, so no passwords were written.")
            return
        chips: list[dict[str, str]] = []
        owner_password = os.environ.get("KDPS_BOOTSTRAP_ADMIN_PASSWORD")
        if owner_email and owner_password:
            chips.append({"label": "Owner", "email": owner_email, "password": owner_password})
        for person in plan.people:
            if person.chip and person.email in created:
                chips.append({"label": person.label, "email": person.email, "password": created[person.email]})
        lines = [f"{email}\t{password}" for email, password in created.items()]
        lines.append("KDPS_DEMO_LOGINS=" + json.dumps(chips, separators=(",", ":")))
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(out, 0o600)
        self.stdout.write(f"Passwords for {len(created)} new login(s) written to {out} (mode 600).")
