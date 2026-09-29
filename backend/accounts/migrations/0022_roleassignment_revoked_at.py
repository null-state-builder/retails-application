from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0021_role_field_access_roleassignment"),
    ]

    operations = [
        migrations.AddField(
            model_name="roleassignment",
            name="revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
