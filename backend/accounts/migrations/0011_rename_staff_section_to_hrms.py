"""Rename the Staff section to HRMS, in place (issue #118).

The section that started as "Staff" (#87) and grew a store-manager-only
"Member Details" override (#96/#97/#100) now reads HRMS everywhere — the
word #84's parent spec already reserved for it, and the one the stores
actually use. The catalog stays thirteen codes: this renames the key in
place rather than adding a fourteenth and retiring the old one, so there is
never a moment where two codes are both live.

Every ``Role.section_access`` row already carries a ``staff`` cell (seeded by
#87's 0005, possibly retuned since by an admin, possibly overridden for
``store_manager`` by 0006). This migration moves that cell — whatever it
currently holds — from the ``staff`` key to the ``hrms`` key, unchanged. That
makes it a rename of the address, not a reset of the value: a live admin's
own tuning of this cell rides along untouched. It is a different shape from
0006/0008's "only if still the seeded default" guard — there is no default to
compare against here, only a key to relabel, so every row is moved.

Reversible: the reverse renames ``hrms`` back to ``staff``. Django unapplies
migrations most-recent-first, so this reverse runs *before* 0006's and 0008's
reverses — by the time 0006 asks "does this role still have a ``staff``
cell", it does, because this migration already put it back.
"""

from django.db import migrations

OLD_KEY = "staff"
NEW_KEY = "hrms"


def _rename_key(apps, *, old: str, new: str) -> None:
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.all():
        access = role.section_access or {}
        if old not in access:
            continue  # nothing to move — leave the row as it is
        access[new] = access.pop(old)
        role.section_access = access
        role.save(update_fields=["section_access"])


def rename_to_hrms(apps, schema_editor):
    _rename_key(apps, old=OLD_KEY, new=NEW_KEY)


def rename_to_staff(apps, schema_editor):
    _rename_key(apps, old=NEW_KEY, new=OLD_KEY)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_access_change"),
    ]

    operations = [
        migrations.RunPython(rename_to_hrms, rename_to_staff),
    ]
