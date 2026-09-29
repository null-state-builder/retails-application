"""OPS-18: legacy receiving is deleted, so a stock entry no longer names a PT file."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0017_sale_posting_kind")]

    operations = [migrations.RemoveField(model_name="stockledgerentry", name="pt_file")]
