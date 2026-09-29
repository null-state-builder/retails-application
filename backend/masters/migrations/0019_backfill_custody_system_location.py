"""GSA-T02: create the sixth protected system location (custody) for every site
that was set up before this ticket added it to `Location.SYSTEM_KINDS`. Idempotent
- a site that already has one is left alone.
"""

from __future__ import annotations

from django.db import migrations


def backfill_custody(apps, schema_editor):
    Store = apps.get_model("masters", "Store")
    Location = apps.get_model("masters", "Location")
    for site in Store.objects.all():
        if Location.objects.filter(site=site, system=True, kind="custody").exists():
            continue
        Location.objects.create(
            tenant_id=site.tenant_id,
            site=site,
            parent=None,
            name="Custody",
            kind="custody",
            system=True,
        )


def noop_reverse(apps, schema_editor):
    # Deliberately not reversed: dropping a system location a real GRN/PT flow
    # may already reference would be a data-losing operation this migration
    # never needs to perform.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("masters", "0018_location_rack_and_custody"),
    ]

    operations = [
        migrations.RunPython(backfill_custody, noop_reverse),
    ]
