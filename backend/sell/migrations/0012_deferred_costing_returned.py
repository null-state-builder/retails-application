"""A piece given back before the books could ever price it (#184).

`DeferredCosting` gains a third status. A sold-before-inward line whose piece
comes back on a plain return has no cost to post and never will: the sale's cost
event and the return's reversal would be the same figure in opposite directions.
Without this the sweep (#186) would post COGS and take the piece off a shelf it
is standing on, days later, when the PT finally priced the cohort - with the
trial balance at nought throughout, which is why it needs saying in the schema
rather than being left to a comment.
"""


from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sell', '0011_plain_return'),
    ]

    operations = [
        migrations.AlterField(
            model_name='deferredcosting',
            name='status',
            field=models.CharField(choices=[('waiting', 'Waiting on inward'), ('posted', 'Posted'), ('returned', 'Given back before it could be priced')], default='waiting', max_length=8),
        ),
    ]
