from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("approvals", "0015_so03_brand_identity")]

    operations = [
        migrations.AddField(
            model_name="approval",
            name="authority_pin",
            field=models.JSONField(blank=True, editable=False, null=True),
        ),
    ]
