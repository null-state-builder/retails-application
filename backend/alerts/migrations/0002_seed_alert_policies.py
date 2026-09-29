from django.db import migrations

# Return window is fixed by the issue itself (#77): 30/15/7 days before an SOR
# brand's deadline. In-transit aging has no corpus number — the design says
# only "not received within N days" — so 3 is picked here the same way
# `CAP_WARN_AT` was in `outbound.returnable`: not invented as *correct*, named
# as one number in one place an owner can retune in the admin without a
# release (Rule 12), sized for Bihar/Jharkhand's short intra-network hauls.
POLICIES = {
    "in_transit_aging": [3],
    "return_window": [30, 15, 7],
}


def seed_policies(apps, schema_editor):
    AlertPolicy = apps.get_model("alerts", "AlertPolicy")
    for kind, thresholds in POLICIES.items():
        AlertPolicy.objects.get_or_create(kind=kind, defaults={"thresholds_days": thresholds})


def unseed_policies(apps, schema_editor):
    AlertPolicy = apps.get_model("alerts", "AlertPolicy")
    AlertPolicy.objects.filter(kind__in=POLICIES.keys()).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed_policies, unseed_policies)]
