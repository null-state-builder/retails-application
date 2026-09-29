from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_alter_user_scope_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="role",
            name="section_access",
            field=models.JSONField(default=dict),
        ),
    ]
