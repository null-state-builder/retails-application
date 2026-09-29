"""One "Store Person": merge ``store_manager`` and ``store_staff`` into ``store_person``.

The SIDEBAR RBAC sheet has one store persona. The app carried two role codes
for it, which held the same twelve sheet cells and parted company only on the
sheetless ``hrms`` section (the manager's "Member Details" rung, 0006). On
22 Sep 2026 Anand ruled the six sheet personas are the initial RBAC and that
Store Person is one role (``docs/features/rbac/rbac-v1-initial-roles.md``).

What moves:

  · a ``store_person`` Role row is created if absent, from the cells written
    out below - frozen, not read from ``rbac_matrix``, so this migration keeps
    saying what it did on the day even after the table is next retuned;
  · every user on either old code is moved onto it (``Role.users`` is
    ``SET_NULL``, so the move must come before the delete or a cashier would
    silently lose every screen);
  · the two old Role rows are deleted.

If an admin had retuned either old row, the merged row starts from the seed
default and their tuning is not carried - it cannot be, because the two old
rows could disagree and nothing says which one wins. The old rows are logged
by ``AccessChange`` history where it exists, and access is data: retune the
merged row in Setup.

Not reversible in data: once merged, nothing records which user was the
manager and which the cashier. The reverse is a deliberate no-op.
"""

from __future__ import annotations

from django.db import migrations

OLD_CODES = ("store_manager", "store_staff")
NEW_CODE = "store_person"

#: ``section_access_for("store_person")`` on 22 Sep 2026 - the sheet's twelve
#: cells plus the sheetless ``hrms`` at the manager's rung, so no store loses a
#: screen it had.
SECTION_ACCESS = {
    "home": {"capability": "view", "label": "Own store"},
    "sell": {"capability": "operate", "label": "Create (bill, return, customer)"},
    "booking": {"capability": "view", "label": "View bookings for own store"},
    "receive_goods": {"capability": "operate", "label": "Receive + bill upload (own store)"},
    "transfer": {"capability": "operate", "label": "Request / Send / Receive"},
    "stock_count": {"capability": "operate", "label": "Count own store"},
    "return_to_brand": {"capability": "operate", "label": "Mark damage only"},
    "stock": {"capability": "view", "label": "Own store"},
    "money": {"capability": "operate", "label": "Expenses only (create)"},
    "offers_price": {"capability": "view", "label": "View"},
    "hrms": {"capability": "manage", "label": "Own store members + attendance (derived)"},
    "reports": {"capability": "view", "label": "Own store only"},
    "setup": {"capability": "none", "label": "No"},
}

ROLE_DEFAULTS = {
    "name": "Store Person",
    "landing_page": "store",
    "nav_groups": ["home", "store_ops", "documents", "ledgers", "controls", "outbound"],
    "description": "One store's floor: bills, returns, receives, counts, own expenses.",
    "section_access": SECTION_ACCESS,
    "is_system": True,
}


def merge_store_roles(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    User = apps.get_model("accounts", "User")

    old_rows = list(Role.objects.filter(code__in=OLD_CODES))
    if not old_rows and not Role.objects.filter(code=NEW_CODE).exists():
        return  # never seeded - seed_foundation will write the merged row itself

    defaults = dict(ROLE_DEFAULTS)
    # A tenant-bound install keeps the merged row inside the same tenant as
    # the rows it replaces.
    tenant_ids = {getattr(row, "tenant_id", None) for row in old_rows} - {None}
    if len(tenant_ids) == 1:
        defaults["tenant_id"] = tenant_ids.pop()

    merged, _ = Role.objects.get_or_create(code=NEW_CODE, defaults=defaults)
    User.objects.filter(role__code__in=OLD_CODES).update(role=merged)
    Role.objects.filter(code__in=OLD_CODES).delete()


def noop_reverse(apps, schema_editor):
    """Deliberately not reversible: nothing records who was manager and who
    was cashier once they hold the one role."""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0019_user_must_change_password"),
    ]

    operations = [
        migrations.RunPython(merge_store_roles, noop_reverse),
    ]
