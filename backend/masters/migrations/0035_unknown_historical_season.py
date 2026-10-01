"""Every installation has the one explicit unknown historical season (OPS-03).

Opening stock whose buying season cannot be established is booked to this named
season. Until now only the demo foundation seed created it, so a blank real
installation could never import such rows. It is closed and sorted oldest, the
same row the seed writes, so the counter never resolves a bare scan to it.
"""

from django.db import migrations


def create(apps, schema_editor):
    Season = apps.get_model("masters", "Season")
    if Season.objects.filter(historical_unknown=True).exists():
        return
    Season.objects.update_or_create(
        code="UNKNOWN-HIST",
        defaults={
            "name": "Unknown historical season",
            "status": "closed",
            "sort_order": 0,
            "historical_unknown": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [("masters", "0034_master_sheet_import")]

    operations = [migrations.RunPython(create, migrations.RunPython.noop)]
