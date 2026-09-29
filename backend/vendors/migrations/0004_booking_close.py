"""How a booking ends: short-close / cancel, with a reason and an actor.

A commitment that can only ever be fulfilled has no honest exit. This adds the
three columns that record the decision — see `Booking.end`.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0003_bookingline_store"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="booking",
            name="close_reason",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="booking",
            name="closed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="bookings_closed",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
