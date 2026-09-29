from django.db import migrations, models

# Store operations ticket 20 (ST-ORD-1, ST-OPS-2): a reservation-expiry alert
# shows this many days before a reservation's collect-by date (baseline B96).
# One number in one place an owner can retune in the admin (Rule 12).
RESERVATION_EXPIRY_DAYS = [1]

KINDS = [
    ("in_transit_aging", "Transfer stuck in transit"),
    ("return_window", "Return window closing"),
    ("document_number", "Document number problem"),
    ("stock_ageing", "Stock ageing"),
    ("reservation_expiry", "Reservation expiring"),
]


def seed_policy(apps, schema_editor):
    AlertPolicy = apps.get_model("alerts", "AlertPolicy")
    AlertPolicy.objects.get_or_create(
        kind="reservation_expiry", defaults={"thresholds_days": RESERVATION_EXPIRY_DAYS}
    )


def unseed_policy(apps, schema_editor):
    AlertPolicy = apps.get_model("alerts", "AlertPolicy")
    AlertPolicy.objects.filter(kind="reservation_expiry").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0012_stock_ageing_alert"),
    ]

    operations = [
        migrations.AlterField(
            model_name="alert",
            name="kind",
            field=models.CharField(choices=KINDS, max_length=32),
        ),
        migrations.AlterField(
            model_name="alertpolicy",
            name="kind",
            field=models.CharField(choices=KINDS, max_length=32, unique=True),
        ),
        migrations.RunPython(seed_policy, unseed_policy),
    ]
