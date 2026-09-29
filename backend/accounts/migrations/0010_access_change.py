from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_actor_policy"),
    ]

    operations = [
        migrations.CreateModel(
            name="AccessChange",
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
                (
                    "resource",
                    models.CharField(
                        choices=[
                            ("role", "Role"),
                            ("user", "User"),
                            ("actor_policy", "Actor policy"),
                            ("approval_policy", "Approval policy"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "operation",
                    models.CharField(
                        choices=[("create", "Create"), ("update", "Update")],
                        max_length=12,
                    ),
                ),
                ("target_id", models.BigIntegerField(blank=True, null=True)),
                ("payload", models.JSONField(default=dict)),
                ("summary", models.CharField(max_length=240)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                (
                    "applied_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="access_changes_applied",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="access_changes_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "accounts_access_change",
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
