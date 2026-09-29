"""Open Booking, read-only, to the two store roles (issue #130).

The SIDEBAR RBAC sheet said "No" on the store person's Booking cell. PRD #104
overturned it on 26 July 2026: a store plans space and staff against the goods
headed its way, so the section opens at ``view`` - the screen and the list, never
the create. This backfill moves the rows already in the database onto the
ratified cell; ``rbac_matrix`` carries it for every fresh install.

Both store roles are touched, because both are the one "Store Person" persona
and the correction is the persona's, not the manager's.

Only a row still sitting at the seeded default is moved. If a live admin has
already retuned this cell, their value wins - access is their data (Rule 12).

Both directions are guarded the same way, from opposite ends (the pattern from
0006): forward asks "is this still the seeded default?", reverse asks "is this
still exactly what I wrote?" - capability **and** label, because a bare ``view``
is also what an admin would set by hand and only the ratified wording tells the
two apart. So migrate-then-rollback is an identity, and a rollback never eats an
admin's grant.

The other half of #130 - ``ho_ops`` and ``data_steward`` becoming ratified rows
rather than derived fallbacks - moves no data at all. Their thirteen cells are
written out with the same capabilities and labels the derived block resolved to,
so a seeded row is already exactly where this migration would put it.
"""

from django.db import migrations

SECTION = "booking"
#: What ``store_person`` held before the correction - the sheet's flat "No".
SEEDED_DEFAULT = {"capability": "none", "label": "No"}
#: The ratified cell, frozen here rather than read from ``rbac_matrix`` at run
#: time: this migration is the record of one correction made on one day, and
#: the two role codes it names were merged into ``store_person`` on
#: 22 Sep 2026 (0020) - a live lookup by the old code would answer "no
#: access" (fail-closed) and quietly close the store's Booking screen.
OPENED = {"capability": "view", "label": "View bookings for own store"}
ROLE_CODES = ("store_manager", "store_staff")


def _retune(apps, cell: dict[str, str], *, only_if: dict[str, str]) -> None:
    """Move both store roles' ``booking`` cell to ``cell``, from a known state only.

    ``only_if`` is the fields the current cell must match to count as untouched;
    anything else is an admin's tuning and is left exactly as it is.
    """
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(code__in=ROLE_CODES):
        access = role.section_access or {}
        current = access.get(SECTION)
        if not current or any(current.get(k) != v for k, v in only_if.items()):
            continue  # never seeded, or an admin has already retuned it
        access[SECTION] = dict(cell)
        role.section_access = access
        role.save(update_fields=["section_access"])


def open_booking(apps, schema_editor):
    _retune(apps, OPENED, only_if=SEEDED_DEFAULT)


def close_booking(apps, schema_editor):
    # ``only_if`` is the whole cell this migration writes. A cell an admin has
    # since reworded does not match, and the row is preserved - the safe way
    # to be wrong.
    _retune(apps, SEEDED_DEFAULT, only_if=OPENED)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_brand_scoped_users"),
    ]

    operations = [
        migrations.RunPython(open_booking, close_booking),
    ]
