# Vocabulary the non-brand authoring path relies on (D2 Q28/Q29), guaranteed
# present even on a database that never ran `seed_ptmapper` (which loads them
# from the Master Sheet when available): the P/M/E price tiers that KDPS keeps
# in the COLOR dimension, and FREE SIZE for sizeless goods. Idempotent, additive
# — data, not code (Rule 12).

from __future__ import annotations

from django.db import migrations

SEEDS = [
    ("color", "PREMIUM"),
    ("color", "MEDIUM"),
    ("color", "ECONOMY"),
    ("size", "FREE SIZE"),
]


def seed(apps, schema_editor):
    ControlledValue = apps.get_model("ptmapper", "ControlledValue")
    for dimension, value in SEEDS:
        ControlledValue.objects.get_or_create(dimension=dimension, value=value)


def unseed(apps, schema_editor):
    # Leave the vocabulary in place on rollback: rows may since be referenced by
    # mapped cells, and removing Master values is a steward decision, not a
    # migration's. Reversing is a no-op by design.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("ptmapper", "0011_ptfile_grn_ptfile_source"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
