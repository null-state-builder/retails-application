from django.db import migrations

#: Same shape as the transfer policy it sits beside (0004), and for the same
#: reason: neither a store person nor a warehouse person holds `transfer:
#: approve` on the ratified matrix (#104) — only Operations Head, brand
#: manager or Owner do. Zero tolerance, zero band: whatever a store is asking
#: for, another location's stock is not committed on the asker's say-so alone
#: (#74), whatever it is worth.
POLICY = {
    "tolerance_paise": 0,
    "band_paise": 0,
    "band_roles": ["ho_ops", "owner"],
    "escalated_roles": ["ho_ops", "owner"],
}


def seed_policy(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalPolicy.objects.get_or_create(kind="stock_request", defaults=POLICY)


def unseed_policy(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalPolicy.objects.filter(kind="stock_request", **POLICY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0004_seed_approval_policies"),
    ]

    operations = [migrations.RunPython(seed_policy, unseed_policy)]
