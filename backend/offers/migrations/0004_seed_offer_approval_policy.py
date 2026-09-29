from django.db import migrations

#: Who countersigns an offer (D11 §7, ratified 3 Aug 2026). Both lists are the
#: same because an offer's value is never known at authoring time — the policy
#: reads an unknown value as "escalate", so the band would never be reached and
#: two different lists would only be a lie in a column.
#:
#: The two roles are exactly the ones the ratified access table puts at
#: `offers_price: approve`. IT Admin holds `manage` there and is deliberately
#: *not* here: configuring the module is not the same as signing a commercial
#: decision, which is the same reason Admin was taken off every other family.
POLICY = {
    "tolerance_paise": 0,
    "band_paise": 0,
    "band_roles": ["brand_manager", "owner"],
    "escalated_roles": ["brand_manager", "owner"],
}


def seed_policy(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalPolicy.objects.get_or_create(kind="offer", defaults=POLICY)


def unseed_policy(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalPolicy.objects.filter(kind="offer", **POLICY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("offers", "0003_seed_eoss_defaults"),
        ("approvals", "0008_seed_stock_request_route"),
    ]

    operations = [migrations.RunPython(seed_policy, unseed_policy)]
