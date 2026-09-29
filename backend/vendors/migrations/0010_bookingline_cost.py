"""A booking line's indicative cost per piece.

Optional, and never binding: the PT holds the cost that counts (overall PRD
R-BUY-001). Store and warehouse logins never see it (`vendors.serializers`).
"""

from __future__ import annotations

from django.db import migrations

import core.money


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0009_enforce_vendor_tenant"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingline",
            name="cost_paise",
            field=core.money.MoneyField(blank=True, null=True),
        ),
    ]
