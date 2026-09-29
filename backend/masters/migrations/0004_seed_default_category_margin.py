# The global fallback margin row for deterministic non-brand pricing (D2 Q7).
# 33% inside a 30–35% band — the design's placeholder default; the per-category
# numbers are still awaited from KDPS (open item), and a steward edits rows in
# the CategoryMargin master, never code (Rule 12). Idempotent.

from __future__ import annotations

from django.db import migrations


def seed(apps, schema_editor):
    CategoryMargin = apps.get_model("masters", "CategoryMargin")
    CategoryMargin.objects.get_or_create(
        item="",
        defaults={"margin_pct": 33, "band_min_pct": 30, "band_max_pct": 35},
    )


def unseed(apps, schema_editor):
    CategoryMargin = apps.get_model("masters", "CategoryMargin")
    CategoryMargin.objects.filter(item="").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("masters", "0003_categorymargin"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
