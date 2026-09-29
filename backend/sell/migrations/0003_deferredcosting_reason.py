"""Why a deferred line is waiting (#178).

The default backfills every existing row to `unpriced`, which is what all of them
are: until this slice, sold-before-inward was the only wait the pipeline could
produce.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sell', '0002_fsm_triggers_and_policy'),
    ]

    operations = [
        migrations.AddField(
            model_name='deferredcosting',
            name='reason',
            field=models.CharField(choices=[('unpriced', 'No cost of record - sold before inward'), ('model_unknown', 'The masters do not know this brand'), ('vendor_unknown', 'Brand-owned, but no supplier of record')], default='unpriced', max_length=16),
        ),
    ]
