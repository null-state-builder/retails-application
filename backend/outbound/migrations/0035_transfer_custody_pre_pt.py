# Goods ticket 13E: the controlled transfer of damaged pre-PT custody.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('outbound', '0034_transfer_custody'),
    ]

    operations = [
        migrations.AlterField(
            model_name='goodstransfer',
            name='custody',
            field=models.CharField(choices=[('ordinary', 'Ordinary'), ('quarantine', 'Quarantine'), ('pre_pt', 'Pre-PT custody')], default='ordinary', max_length=12),
        ),
    ]
