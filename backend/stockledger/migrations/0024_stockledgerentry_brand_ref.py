from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("stockledger", "0023_so03_brand_identity"),
        ("masters", "0032_so03_brand_identity"),
    ]
    operations = [migrations.AddField(
        model_name="stockledgerentry", name="brand_ref",
        field=models.ForeignKey(
            to="masters.brand", null=True, blank=True, editable=False,
            on_delete=django.db.models.deletion.PROTECT, related_name="+",
        ),
    )]
