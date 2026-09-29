"""Bind the sale and the credit note to the docstatus FSM, and lay in the dials.

The trigger is the half of the document skeleton that binds even the superuser
the CI connects as: a row must be born a draft, may not walk backwards, may not
be edited once posted, and may not be deleted at all. Without it a `Sale` is an
ordinary table with a good docstring.

The policy row is seeded here rather than by `seed_foundation`, because a
missing one would mean the accept pipeline had no cap to check a discount
against - and a dial that only exists on seeded machines is not a dial.
"""

from __future__ import annotations

from django.db import migrations

from core.documents import document_fsm_reverse_sql, document_fsm_sql

TABLES = ["sell_sale", "sell_credit_note"]


def seed_policy(apps, schema_editor):
    """One row, at the strict end of both dials (see `SellPolicy`)."""
    SellPolicy = apps.get_model("sell", "SellPolicy")
    SellPolicy.objects.get_or_create(
        id=1,
        defaults={"manual_discount_cap_percent": 0, "credit_note_validity_days": 180},
    )


def drop_policy(apps, schema_editor):
    apps.get_model("sell", "SellPolicy").objects.filter(id=1).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sell", "0001_initial"),
        ("core", "0006_external_number_acceptance"),
    ]

    operations = [
        *(
            migrations.RunSQL(
                sql=document_fsm_sql(table),
                reverse_sql=document_fsm_reverse_sql(table),
            )
            for table in TABLES
        ),
        migrations.RunPython(seed_policy, drop_policy),
    ]
