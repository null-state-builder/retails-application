"""Grant the new ``staff`` section on already-seeded roles (issue #87).

#85 shipped twelve sections; the 13-section sidebar adds ``staff`` (Attendance
now, Members pending). ``Role.section_access`` rows written before this point
have no ``staff`` key, and an absent key fail-closes to ``none`` — so without
this backfill the section would be invisible to everyone until a re-seed.

Only the missing key is added. Every other cell is left exactly as it is, so a
role a live admin has already retuned keeps that tuning (Rule 12: access is
their data, not ours).

The cell each role gets is frozen literally below rather than imported live
from ``rbac_matrix`` — #118 later renames this very section, and a live import
would make a fresh replay of this migration write whatever the table says
*today* instead of what #87 actually shipped.
"""

from django.db import migrations

#: Every ratified role code's ``staff`` cell, frozen at its final pre-#118
#: value (``store_manager`` carries #96/#97's later override, not #87's
#: original) — the same answer ``section_access_for(code)["staff"]`` gave
#: before #118 renamed the key away. On a fresh replay this makes 0006 a
#: no-op for ``store_manager`` (already at the value 0006 would grant), which
#: converges to the same end state as the original history. A role code this
#: table does not know gets ``none`` / ``"No"``, matching
#: ``section_access_for``'s own fail-closed default.
STAFF_CELL_BY_ROLE: dict[str, tuple[str, str]] = {
    "owner": ("manage", "Full (derived)"),
    "store_manager": ("manage", "Own store members + attendance (derived)"),
    "store_staff": ("operate", "Own attendance (derived)"),
    "warehouse": ("operate", "Own attendance (derived)"),
    "brand_manager": ("none", "No (derived)"),
    "accounts": ("view", "View (payroll inputs) (derived)"),
    "it_admin": ("manage", "Full (derived)"),
    "ho_ops": ("view", "All (network)"),
    "data_steward": ("none", "No"),
}


def add_staff_section(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.all():
        access = role.section_access or {}
        if "staff" in access:
            continue
        capability, label = STAFF_CELL_BY_ROLE.get(role.code, ("none", "No"))
        access["staff"] = {"capability": capability, "label": label}
        role.section_access = access
        role.save(update_fields=["section_access"])


def drop_staff_section(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.all():
        access = role.section_access or {}
        if access.pop("staff", None) is not None:
            role.section_access = access
            role.save(update_fields=["section_access"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_backfill_section_access"),
    ]

    operations = [
        migrations.RunPython(add_staff_section, drop_staff_section),
    ]
