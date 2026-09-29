"""The store's mirror of what the counter has parked (#185, grill Q13).

Non-financial by construction: no `Document` base, no series, no ledger. The till
owns the list and replaces it wholesale on each push, and the key is unique
**per store** - a hold matched by its uuid alone could be another store's.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('masters', '0007_dataset_watermarks'),
        ('sell', '0006_sale_override_evidence'),
    ]

    operations = [
        migrations.CreateModel(
            name='HeldBill',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('held_uuid', models.UUIDField()),
                ('label', models.CharField(blank=True, default='', max_length=120)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('held_at', models.DateTimeField()),
                ('expires_policy', models.CharField(choices=[('today', 'Expires at day close unless the store keeps it'), ('kept', 'The store chose to carry it forward')], default='today', max_length=8)),
                ('store', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='held_bills', to='masters.store')),
            ],
            options={
                'db_table': 'sell_held_bill',
                'ordering': ['held_at', 'id'],
                'indexes': [models.Index(fields=['store', 'held_at'], name='heldbill_store_idx')],
                'constraints': [models.UniqueConstraint(fields=('store', 'held_uuid'), name='uq_heldbill_store_uuid')],
            },
        ),
    ]
