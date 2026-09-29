"""Seed masters.Customer from every bill already on the books (#242).

One row per distinct non-blank `customer_mobile`, newest bill's name winning,
gstin from the newest B2B bill carrying that mobile. Lives in `sell` rather
than `masters` (where the table itself lives) because a `masters` migration
depending on `sell` would invert the ratified dependency arrow
(ADR-0002: domain modules -> masters -> core) and risk a migration-graph
cycle later - `sell` reading down into `masters` is the allowed direction.

Idempotent (keyed on mobile via `sell.services.customers.upsert_customer`), so
re-running is safe, and a historic mobile that is blank or whose digits exceed
`varchar(15)` is skipped rather than raising - a migration that dies mid-way is
a deploy that does not land.
"""

from django.db import migrations


def backfill_customers(apps, schema_editor):
    from sell.services.customers import backfill_customers as run_backfill

    Sale = apps.get_model("sell", "Sale")
    Customer = apps.get_model("masters", "Customer")
    run_backfill(Sale, Customer)


class Migration(migrations.Migration):
    dependencies = [
        ("sell", "0014_upi_stamp"),
        ("masters", "0008_customer"),
    ]

    operations = [
        migrations.RunPython(backfill_customers, migrations.RunPython.noop),
    ]
