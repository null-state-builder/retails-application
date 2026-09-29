"""PT-making leaves the store's Receive Goods cell for the warehouse's (#119).

Store and warehouse held the same ``operate`` rung on ``receive_goods``, so
nothing separated "receive" from "make a PT" and no gate could tell the two
apart. Anand ruled PT-making is warehouse work: the goods land there, the
brand's own PT is read there, and the KDPS PT is produced there. A store's one
legitimate right on this section is the bill upload for brands that deliver
straight to it (Madura is the named case).

Two cells move, both already seeded rows only - a live admin's own retune of
either is left exactly as it is (Rule 12):

  · the warehouse rises one rung, ``operate`` -> ``approve`` - the rung
    ``PtFileListCreateView`` now demands to write. Nothing else on this
    section gates at ``approve`` (checked against every ``receive_goods`` call
    site in the code), so the raise grants the warehouse nothing beyond
    PT-making.
  · the store's cell keeps its rung - receiving stays theirs - and only its
    label changes, from "Receive + PT (own store)" to "Receive + bill upload
    (own store)", so the sheet's own wording stops promising what #119 takes
    away.

Both directions are guarded the pattern from 0006/0008: forward asks "is this
still the seeded default?", reverse asks "is this still exactly what I
wrote?" - capability **and** label, because a bare ``operate`` is also what an
admin would set by hand and only the ratified wording tells the two apart. So
migrate-then-rollback is an identity, and a rollback never eats an admin's own
grant.
"""

from __future__ import annotations

from django.db import migrations

SECTION = "receive_goods"

WAREHOUSE_ROLE_CODES = ("warehouse",)
STORE_ROLE_CODES = ("store_manager", "store_staff")

#: What each role held before this migration - the sheet's own wording, frozen
#: here rather than read live, the same reasoning 0006 gives for not trusting a
#: later rename of the section key to leave an old cell recognisable.
WAREHOUSE_SEEDED_DEFAULT = {"capability": "operate", "label": "Create (GRN, PT)"}
STORE_SEEDED_DEFAULT = {"capability": "operate", "label": "Receive + PT (own store)"}

#: What each role holds after this migration - frozen for the same reason. The
#: store codes named above were merged into ``store_person`` on 22 Sep 2026
#: (0020); a live ``section_access_for("store_staff")`` would answer "no
#: access" and this migration would write ``none`` over the store's cell on any
#: database that had not yet run it.
WAREHOUSE_RAISED = {"capability": "approve", "label": "Create (GRN, PT)"}
STORE_RETITLED = {"capability": "operate", "label": "Receive + bill upload (own store)"}


def _retune(
    apps, role_codes: tuple[str, ...], cell: dict[str, str], *, only_if: dict[str, str]
) -> None:
    """Move every named role's ``receive_goods`` cell to ``cell``, from a known
    state only.

    ``only_if`` is the fields the current cell must match to count as
    untouched; anything else is an admin's tuning and is left exactly as it is.
    """
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(code__in=role_codes):
        access = role.section_access or {}
        current = access.get(SECTION)
        if not current or any(current.get(k) != v for k, v in only_if.items()):
            continue  # never seeded, or an admin has already retuned it
        access[SECTION] = dict(cell)
        role.section_access = access
        role.save(update_fields=["section_access"])


def raise_warehouse_and_retitle_store(apps, schema_editor):
    _retune(apps, WAREHOUSE_ROLE_CODES, WAREHOUSE_RAISED, only_if=WAREHOUSE_SEEDED_DEFAULT)
    _retune(apps, STORE_ROLE_CODES, STORE_RETITLED, only_if=STORE_SEEDED_DEFAULT)


def lower_warehouse_and_restore_store_label(apps, schema_editor):
    _retune(apps, WAREHOUSE_ROLE_CODES, WAREHOUSE_SEEDED_DEFAULT, only_if=WAREHOUSE_RAISED)
    _retune(apps, STORE_ROLE_CODES, STORE_SEEDED_DEFAULT, only_if=STORE_RETITLED)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_return_to_brand_actor_policy"),
    ]

    operations = [
        migrations.RunPython(
            raise_warehouse_and_retitle_store, lower_warehouse_and_restore_store_label
        ),
    ]
