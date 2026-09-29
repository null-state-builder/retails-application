"""OPS-18: the legacy GRN and its lines go (store and warehouse operations PRD §5.5).

The goods-v1 arrival, count and GRN tables in this app are untouched. Dropping
``inbound_grn`` also drops the ``status`` column 0004 left behind on purpose.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("inbound", "0010_duplicatearrivalacknowledgement_and_more"),
        # The legacy PT file pointed at the GRN; it goes first.
        ("ptmapper", "0018_delete_legacy_pt_mapper"),
    ]

    operations = [
        migrations.DeleteModel(name="GrnLine"),
        migrations.DeleteModel(name="Grn"),
    ]
