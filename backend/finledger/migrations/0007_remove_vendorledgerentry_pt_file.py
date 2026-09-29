"""OPS-18: legacy receiving is deleted, so a vendor entry no longer names a PT file."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("finledger", "0006_bank_reconciliation")]

    operations = [migrations.RemoveField(model_name="vendorledgerentry", name="pt_file")]
