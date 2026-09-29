"""Close the TRUNCATE bypass on the append-only stock ledger (ADR-0006).

The BEFORE UPDATE/DELETE append-only guard does not fire on TRUNCATE; this adds a
statement-level BEFORE TRUNCATE trigger (binds even the superuser) + REVOKE.
"""

from django.db import migrations

from core.ledger import truncate_guard_reverse_sql, truncate_guard_sql

TABLE = "stockledger_entry"


class Migration(migrations.Migration):

    dependencies = [
        ("stockledger", "0001_initial"),
        ("core", "0005_glentry"),
    ]

    operations = [
        migrations.RunSQL(
            sql=truncate_guard_sql(TABLE),
            reverse_sql=truncate_guard_reverse_sql(TABLE),
        ),
    ]
