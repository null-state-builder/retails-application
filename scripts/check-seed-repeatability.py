#!/usr/bin/env python3
"""Prove reseeding preserves a changed password/access cell and master counts.

Run only through ``scripts/proof.py run`` after ``proof.py prepare``. The proof
database is disposable; this script restores the changed cells even on failure.
"""

from __future__ import annotations

import os
import secrets

import django


if os.environ.get("KDPS_PROOF_MODE") != "1":
    raise SystemExit("Seed repeatability may run only in isolated proof mode.")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from accounts.management.commands.seed_foundation import deployment_tenant  # noqa: E402
from accounts.models import Role, User  # noqa: E402
from core.tenancy import tenant_context  # noqa: E402
from django.core.management import call_command  # noqa: E402
from masters.models import Brand, Store  # noqa: E402


tenant = deployment_tenant()
with tenant_context(tenant.pk):
    owner = User.objects.get(username="owner")
    role = Role.objects.get(code="warehouse")
    original_hash = owner.password
    original_access = dict(role.section_access)
    changed_password = secrets.token_urlsafe(24)
    changed_access = dict(original_access)
    old_home = original_access["home"]
    changed_access["home"] = {
        "capability": "none" if old_home["capability"] != "none" else "view",
        "label": "SO-02 proof operator change",
    }
    counts = {
        "users": User.objects.count(),
        "roles": Role.objects.count(),
        "stores": Store.objects.count(),
        "brands": Brand.objects.count(),
    }
    owner.set_password(changed_password)
    owner.save(update_fields=["password"])
    role.section_access = changed_access
    role.save(update_fields=["section_access"])

try:
    call_command("seed_foundation", verbosity=0)
    with tenant_context(tenant.pk):
        owner.refresh_from_db()
        role.refresh_from_db()
        after = {
            "users": User.objects.count(),
            "roles": Role.objects.count(),
            "stores": Store.objects.count(),
            "brands": Brand.objects.count(),
        }
        assert after == counts, f"Foundation reseed duplicated or removed masters: {counts} -> {after}"
        assert owner.check_password(changed_password), "Foundation reseed replaced an operator password"
        assert role.section_access["home"] == changed_access["home"], (
            "Foundation reseed replaced an operator access decision"
        )
finally:
    with tenant_context(tenant.pk):
        owner.refresh_from_db()
        role.refresh_from_db()
        owner.password = original_hash
        owner.save(update_fields=["password"])
        role.section_access = original_access
        role.save(update_fields=["section_access"])

print("Foundation reseed preserved user/role/store/brand counts, password and access setting.")
