"""Ticket 31: which offer gave each copied line its discount.

The copy is incremental, so lines copied before this change would never gain
their offer. Clearing the copy's as-of time makes the worker's next run rebuild
it whole, and the offer simulation says the copy is not built until then.
"""

from django.db import migrations, models


def rebuild_whole(apps, schema_editor):
    ReportRefresh = apps.get_model("reporting", "ReportRefresh")
    ReportRefresh.objects.filter(key="offer_sim").update(as_of=None)


class Migration(migrations.Migration):
    dependencies = [
        ("reporting", "0008_merge_20260928_1427"),
    ]

    operations = [
        migrations.AddField(
            model_name="offersimlinefact",
            name="offer_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="offersimlinefact",
            name="offer_parts",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="offersimlinefact",
            name="offer_parts_unread",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(rebuild_whole, migrations.RunPython.noop),
    ]
