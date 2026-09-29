from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


SEEDED_POLICIES = (
    (
        "masters.writes",
        "Edit business master data",
        ["owner", "it_admin", "data_steward"],
        "Edit vendors, stores, GST registrations and other shared business masters.",
    ),
    (
        "ptmapper.post_and_reverse_pt",
        "Inward or reverse a PT",
        ["accounts", "owner"],
        "Post stock value and vendor liability from a priced PT, or reverse it.",
    ),
    (
        "ptmapper.mapping_stewardship",
        "Steward PT mappings",
        ["warehouse", "data_steward"],
        "Price PTs and resolve mapping review items.",
    ),
    (
        "outbound.execute_vflip",
        "Execute a V-flip",
        ["accounts", "owner"],
        "Post an approved ownership flip and its value entries.",
    ),
)


def seed_actor_policies(apps, schema_editor):
    ActorPolicy = apps.get_model("accounts", "ActorPolicy")
    for action, label, roles, description in SEEDED_POLICIES:
        ActorPolicy.objects.get_or_create(
            action=action,
            defaults={"label": label, "roles": roles, "description": description},
        )


def unseed_actor_policies(apps, schema_editor):
    ActorPolicy = apps.get_model("accounts", "ActorPolicy")
    for action, _label, roles, description in SEEDED_POLICIES:
        ActorPolicy.objects.filter(
            action=action,
            roles=roles,
            description=description,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_store_booking_view")]

    operations = [
        migrations.CreateModel(
            name="ActorPolicy",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("action", models.CharField(max_length=100, unique=True)),
                ("label", models.CharField(max_length=120)),
                ("description", models.CharField(blank=True, default="", max_length=300)),
                (
                    "roles",
                    ArrayField(
                        base_field=models.CharField(max_length=40),
                        default=list,
                        size=None,
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "actor policies",
                "db_table": "accounts_actor_policy",
                "ordering": ["action"],
            },
        ),
        migrations.RunPython(seed_actor_policies, unseed_actor_policies),
    ]
