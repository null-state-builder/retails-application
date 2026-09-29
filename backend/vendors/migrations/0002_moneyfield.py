from __future__ import annotations

from django.db import migrations

import core.money


class Migration(migrations.Migration):

    dependencies = [
        ("vendors", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="booking",
            name="estimated_value_paise",
            field=core.money.MoneyField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="bookingline",
            name="mrp_paise",
            field=core.money.MoneyField(blank=True, null=True),
        ),
    ]
