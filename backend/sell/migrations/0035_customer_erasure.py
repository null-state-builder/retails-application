import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("masters", "0026_consent_wording"),
        ("sell", "0034_customer_consent"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerErasure",
            fields=[
                ("id", models.UUIDField(editable=False, primary_key=True, serialize=False)),
                ("erased_at", models.DateTimeField()),
                (
                    "store",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="masters.store",
                    ),
                ),
            ],
            options={
                "db_table": "sell_customer_erasure",
                "ordering": ["-erased_at"],
                "indexes": [
                    models.Index(fields=["erased_at"], name="sell_cust_erasure_at_idx")
                ],
            },
        ),
    ]
