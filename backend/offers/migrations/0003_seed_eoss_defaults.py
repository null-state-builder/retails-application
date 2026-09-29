"""Seed the network-default EOSS ladder and sell-through target curve.

Idempotent: guarded by `get_or_create`, so re-running (or a fresh `migrate` on
a re-seeded dev DB) never duplicates rows. Numbers come straight off the
research in docs/05-offers/markdown-ai-research: 2-3 shallow-then-deeper steps,
and a curve that expects a third of the buy sold by mid-season.
"""

from __future__ import annotations

from django.db import migrations

LADDER = [
    # (step_no, trigger_type, trigger_value, discount_pct)
    (1, "gap", 10, 15),
    (2, "gap", 25, 30),
    (3, "gap", 40, 50),
]

TARGETS = [
    # (week_number, target_pct)
    (2, 10), (4, 20), (6, 35), (8, 50), (10, 65), (12, 80), (16, 90), (20, 95),
]


def seed(apps, schema_editor):
    EossLadderStep = apps.get_model("offers", "EossLadderStep")
    SellThroughTarget = apps.get_model("offers", "SellThroughTarget")
    for step_no, trigger_type, trigger_value, discount_pct in LADDER:
        EossLadderStep.objects.get_or_create(
            brand=None,
            step_no=step_no,
            defaults={"trigger_type": trigger_type, "trigger_value": trigger_value, "discount_pct": discount_pct},
        )
    for week_number, target_pct in TARGETS:
        SellThroughTarget.objects.get_or_create(
            brand=None, week_number=week_number, defaults={"target_pct": target_pct}
        )


def unseed(apps, schema_editor):
    EossLadderStep = apps.get_model("offers", "EossLadderStep")
    SellThroughTarget = apps.get_model("offers", "SellThroughTarget")
    EossLadderStep.objects.filter(brand__isnull=True).delete()
    SellThroughTarget.objects.filter(brand__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("offers", "0002_offer_mode_eossladderstep_eossrecommendation_and_more"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
