"""Give the store *manager* the Staff section their sketch asks for (issue #96).

#87 seeded both store roles from one "Store Person" persona, so a manager and a
cashier came out identical: ``staff: operate`` — own attendance only. The
hand-drawn Store Ops screen puts "Member Details" (add/remove members, contact
and bank details, monthly target vs achievement) in the store's own list, and
that is manager work. ``ROLE_OVERRIDES`` now lifts ``store_manager`` to
``staff: manage``; this backfill moves the rows already in the database.

Only a row still sitting at the seeded default is touched. If a live admin has
already retuned this cell, their value wins — access is their data (Rule 12).
The cashier is not touched at all.

Both directions are guarded the same way, from opposite ends (issue #100):
forward asks "is this still the seeded default?", reverse asks "is this still
exactly what I wrote?" — capability **and** label, because a bare ``manage`` is
also what an admin would set by hand, and only the derived label tells the two
apart. So migrate-then-rollback is an identity, and a rollback never eats an
admin's grant.

Both cells are frozen literally below rather than imported live from
``rbac_matrix`` — #118 later renames this very section, and a live import
would make a fresh replay compare against whatever the table says *today*
instead of the pair this migration actually wrote.
"""

from django.db import migrations

SEEDED_DEFAULT = "operate"
ROLE_CODE = "store_manager"

#: The cell 0005 seeds before this migration runs, and the cell this
#: migration moves it to.
SEEDED_CELL = {"capability": "operate", "label": "Own attendance (derived)"}
GRANTED_CELL = {
    "capability": "manage",
    "label": "Own store members + attendance (derived)",
}


def _retune(apps, cell: dict[str, str], *, only_if: dict[str, str]) -> None:
    """Move ``store_manager``'s ``staff`` cell to ``cell``, from a known state only.

    ``only_if`` is the fields the current cell must match to count as untouched;
    anything else is an admin's tuning and is left exactly as it is.
    """
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(code=ROLE_CODE):
        access = role.section_access or {}
        current = access.get("staff")
        if not current or any(current.get(k) != v for k, v in only_if.items()):
            continue  # never seeded, or an admin has already retuned it
        access["staff"] = dict(cell)
        role.section_access = access
        role.save(update_fields=["section_access"])


def grant_members(apps, schema_editor):
    _retune(
        apps,
        GRANTED_CELL,
        only_if={"capability": SEEDED_DEFAULT},
    )


def revoke_members(apps, schema_editor):
    _retune(
        apps,
        SEEDED_CELL,
        # The whole cell this migration writes. If the override's wording ever
        # drifts from what was written here, the match fails and the row is
        # preserved — the safe way to be wrong.
        only_if=GRANTED_CELL,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_add_staff_section_access"),
    ]

    operations = [
        migrations.RunPython(grant_members, revoke_members),
    ]
