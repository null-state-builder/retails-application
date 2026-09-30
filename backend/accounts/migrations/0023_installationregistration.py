import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0022_roleassignment_revoked_at")]

    operations = [
        migrations.CreateModel(
            name="InstallationRegistration",
            fields=[
                ("deployment_key", models.UUIDField(primary_key=True, editable=False, serialize=False)),
                ("command_id", models.UUIDField(default=uuid.uuid4)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("summary", models.JSONField()),
                ("summary_hash", models.CharField(max_length=64)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("owner_password_hash", models.CharField(max_length=256)),
                ("admin_password_hash", models.CharField(max_length=256)),
                ("owner_confirmed_at", models.DateTimeField(null=True, blank=True)),
                ("admin_confirmed_at", models.DateTimeField(null=True, blank=True)),
                ("confirmation_history", models.JSONField(default=list)),
                ("first_store_id", models.BigIntegerField(null=True, blank=True)),
                ("completed_at", models.DateTimeField(null=True, blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("tenant", models.OneToOneField(to="masters.tenant", null=True, blank=True,
                    on_delete=django.db.models.deletion.PROTECT, related_name="registration")),
            ],
        ),
    ]
