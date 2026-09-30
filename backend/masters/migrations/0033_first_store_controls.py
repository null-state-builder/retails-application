from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("masters", "0032_so03_brand_identity")]

    operations = [
        migrations.AddField(
            model_name="siteguard",
            name="selling_mode",
            field=models.CharField(
                choices=[
                    ("historical", "Retained counter contract"),
                    ("online_alpha", "Server-confirmed online trading"),
                ],
                default="historical",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="productsku", name="no_discount", field=models.BooleanField(default=False)
        ),
    ]
