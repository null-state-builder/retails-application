"""Ticket 07, step 3 of 3: the old salesperson table goes (ST-OPS-5, §29).

* The "a sold line names who sold it" check comes back over the new columns: a
  staff record, or - for a line sold before the move - its frozen old row.
* The two columns that pointed at the old table are dropped. 0028 already
  copied what they said onto every line and checked it.
* The old table itself is dropped **only when no old row is left unmatched**.
  While Admin still has rows to resolve, it stays in the database untouched and
  ``manage.py finish_salesperson_move`` drops it later, refusing for as long as
  any row is unmatched. A refusal here would fail the deploy (Railway runs
  ``migrate`` before it switches over), so this step waits rather than refuses.

Nothing in the code reads the old table after this; the Django model is gone
either way.
"""

from __future__ import annotations

from django.db import migrations, models

from sell.services.salesperson_move import OldTableStillNeeded, drop_old_table


def drop_when_all_matched(apps, schema_editor):  # type: ignore[no-untyped-def]
    with schema_editor.connection.cursor() as cursor:
        try:
            drop_old_table(cursor)
        except OldTableStillNeeded as waiting:
            print(f"\n  Old salesperson table kept: {waiting}")  # noqa: T201 - migrate output


def recreate_old_table(apps, schema_editor):  # type: ignore[no-untyped-def]
    """Going back: the old table as 0001 made it, if this step had dropped it."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.sell_salesman') IS NOT NULL")
        if cursor.fetchone()[0]:
            return
    schema_editor.create_model(apps.get_model("sell", "Salesman"))


class Migration(migrations.Migration):
    dependencies = [("sell", "0028_salesperson_match_data")]

    operations = [
        migrations.AddConstraint(
            model_name="saleline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("direction", "sale"), _negated=True),
                    ("salesperson__isnull", False),
                    ("salesperson_match__isnull", False),
                    _connector="OR",
                ),
                name="ck_saleline_sale_has_salesperson",
            ),
        ),
        migrations.AddConstraint(
            model_name="saleline",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("salesperson__isnull", True),
                    ("salesperson_match__isnull", True),
                    _connector="OR",
                ),
                name="ck_saleline_one_salesperson_source",
            ),
        ),
        migrations.RemoveField(model_name="saleline", name="salesman"),
        migrations.RemoveField(model_name="sale", name="salesman_default"),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name="Salesman")],
            database_operations=[
                migrations.RunPython(drop_when_all_matched, recreate_old_table)
            ],
        ),
    ]
