"""OPS-18: the legacy PT mapper's files, rows, rule tables and learning records go.

Store and warehouse operations PRD §5.5: legacy receiving is deleted, code and
records together. Dropping a table drops its rows, indexes, constraints and
triggers with it; the goods-v1 PT tables in this app are untouched.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ptmapper", "0017_openingseasoncorrection"),
        ("finledger", "0007_remove_vendorledgerentry_pt_file"),
        ("stockledger", "0018_remove_stockledgerentry_pt_file"),
    ]

    operations = [
        migrations.DeleteModel(name="CorrectionEvent"),
        migrations.DeleteModel(name="PtRow"),
        migrations.DeleteModel(name="PtFile"),
        migrations.DeleteModel(name="LookupProposal"),
        migrations.DeleteModel(name="ReviewItem"),
        migrations.DeleteModel(name="Lookup"),
        migrations.DeleteModel(name="TaxonomyRule"),
        migrations.DeleteModel(name="ControlledValue"),
        migrations.DeleteModel(name="ItemTaxonomy"),
    ]
