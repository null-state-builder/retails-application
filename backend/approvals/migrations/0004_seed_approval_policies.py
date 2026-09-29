from django.db import migrations


POLICIES = {
    "writeoff": {
        "tolerance_paise": 0,
        "band_paise": 25_00_000,
        "band_roles": ["store_manager", "ho_ops", "owner"],
        "escalated_roles": ["ho_ops", "owner"],
    },
    "adjustment": {
        "tolerance_paise": 2_00_000,
        "band_paise": 25_00_000,
        "band_roles": ["store_manager", "ho_ops", "owner"],
        "escalated_roles": ["ho_ops", "owner"],
    },
    "vflip": {
        "tolerance_paise": 0,
        "band_paise": 25_00_000,
        "band_roles": ["accounts", "owner"],
        "escalated_roles": ["owner"],
    },
    "return_to_brand": {
        "tolerance_paise": 0,
        "band_paise": 25_00_000,
        "band_roles": ["brand_manager", "owner"],
        "escalated_roles": ["owner"],
    },
    "transfer": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["ho_ops", "owner"],
        "escalated_roles": ["ho_ops", "owner"],
    },
    "pt_reverse": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["accounts", "owner"],
        "escalated_roles": ["accounts", "owner"],
    },
    # Existing document-family names remain until their owning slices rename
    # and wire them to the PRD families above.
    "gap_closure": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["ho_ops", "owner"],
        "escalated_roles": ["ho_ops", "owner"],
    },
    "damage": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["warehouse", "owner"],
        "escalated_roles": ["warehouse", "owner"],
    },
}

# Values written by the last outbound role correction.  A running installation
# already has these rows, so get_or_create alone would leave the superseded
# actors in place.  Match the whole old policy before rewriting: a row the
# business has retuned is data and must win over a later release.
SUPERSEDED = {
    "writeoff": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["ho_ops", "it_admin", "owner"],
        "escalated_roles": ["ho_ops", "it_admin", "owner"],
    },
    "adjustment": {
        "tolerance_paise": 2_00_000,
        "band_paise": 25_00_000,
        "band_roles": ["ho_ops", "it_admin", "owner", "store_manager"],
        "escalated_roles": ["ho_ops", "it_admin", "owner"],
    },
    "vflip": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["it_admin", "owner", "warehouse"],
        "escalated_roles": ["it_admin", "owner", "warehouse"],
    },
    "gap_closure": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["brand_manager", "ho_ops", "owner"],
        "escalated_roles": ["brand_manager", "ho_ops", "owner"],
    },
    "damage": {
        "tolerance_paise": 0,
        "band_paise": 0,
        "band_roles": ["it_admin", "owner", "warehouse"],
        "escalated_roles": ["it_admin", "owner", "warehouse"],
    },
}


def seed_policies(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    for kind, defaults in POLICIES.items():
        old_values = SUPERSEDED.get(kind)
        updated = (
            ApprovalPolicy.objects.filter(kind=kind, **old_values).update(**defaults)
            if old_values
            else 0
        )
        if not updated:
            ApprovalPolicy.objects.get_or_create(kind=kind, defaults=defaults)


def unseed_policies(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    for kind, values in POLICIES.items():
        old_values = SUPERSEDED.get(kind)
        if old_values:
            ApprovalPolicy.objects.filter(kind=kind, **values).update(**old_values)
        else:
            ApprovalPolicy.objects.filter(kind=kind, **values).delete()


class Migration(migrations.Migration):
    # Run after the historical outbound correction that rewrote any policy rows
    # it found. These are the newer ratified defaults and must be the last word.
    dependencies = [
        ("approvals", "0003_maker_is_barred_from_checking"),
        ("outbound", "0015_backfill_transfer_pt"),
    ]

    operations = [migrations.RunPython(seed_policies, unseed_policies)]
