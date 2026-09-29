"""The pause's end names its financial year too (OPS-11, 1 April follow-up)."""

from django.db import migrations, models
from django.db.models import F


def _backfill(apps, schema_editor):
    # Every pause ended so far ended in the year it began: nothing numbered
    # across 1 April before the end carried a year of its own.
    TillPause = apps.get_model("sell", "TillPause")
    TillPause.objects.filter(resumed_at__isnull=False).update(resumed_fy=F("fy"))


class Migration(migrations.Migration):
    dependencies = [("sell", "0022_ops11_till_pause")]

    operations = [
        migrations.AddField(
            model_name="tillpause",
            name="resumed_fy",
            field=models.CharField(blank=True, default="", max_length=7),
        ),
        migrations.RunPython(_backfill, migrations.RunPython.noop),
    ]
