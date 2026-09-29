from django.db import migrations

#: Who signs a booking (D11 §3, ratified 3 Aug 2026). The ratified access table
#: puts exactly one business seat at `booking: approve` — the Owner. IT Admin
#: holds `manage` there and is deliberately absent, as it is from every other
#: family: configuring bookings is not committing the season's money.
POLICY = {
    "tolerance_paise": 0,
    "band_paise": 0,
    "band_roles": ["owner"],
    "escalated_roles": ["owner"],
}


def seed_policy(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalPolicy.objects.get_or_create(kind="booking", defaults=POLICY)


def unseed_policy(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalPolicy.objects.filter(kind="booking", **POLICY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("vendors", "0005_booking_submitted"),
        ("approvals", "0008_seed_stock_request_route"),
    ]

    operations = [migrations.RunPython(seed_policy, unseed_policy)]
