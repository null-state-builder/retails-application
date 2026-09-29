# Store operations ticket 20, review fixes: a reservation's reference is unique
# within its store (store codes are unique only within a company), and the
# advance's movements are append-only in the database, like every other ledger.

from django.db import migrations, models

from core.ledger import append_only_reverse_sql, append_only_sql


class Migration(migrations.Migration):
    dependencies = [
        ("sell", "0035_customer_reservation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customerreservation",
            name="ref",
            field=models.CharField(max_length=32),
        ),
        migrations.AddConstraint(
            model_name="customerreservation",
            constraint=models.UniqueConstraint(
                fields=("store", "ref"), name="uq_reservation_store_ref"
            ),
        ),
        migrations.RunSQL(
            sql=append_only_sql("sell_reservation_advance"),
            reverse_sql=append_only_reverse_sql("sell_reservation_advance"),
        ),
    ]
