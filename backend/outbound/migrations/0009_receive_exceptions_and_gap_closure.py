"""Receive exceptions (short / extra / damaged) + the gap-closure document (#71)."""

import core.money
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_glentry'),
        ('masters', '0004_seed_default_category_margin'),
        ('outbound', '0008_rbac_approver_roles'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='storetransferline',
            name='qty_resolved',
            field=models.IntegerField(default=0, help_text='Pieces a posted gap closure accounted for (#71) — found later, returned to the sender, or written off as lost. Deliberately not folded into qty_received: only two of those three ever reached the destination, and the receipt must keep saying what was actually scanned in.'),
        ),
        migrations.AlterField(
            model_name='transferreceipt',
            name='shortfall_notes',
            field=models.TextField(blank=True, default='', help_text='What the receiver typed about the shortfall. The screen has always asked for it; until #71 the payload dropped it before the server saw it, so the one sentence explaining a gap was thrown away.'),
        ),
        migrations.CreateModel(
            name='TransferGapClosure',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('doc_number', models.CharField(blank=True, max_length=128, null=True, unique=True)),
                ('idempotency_uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('docstatus', models.IntegerField(choices=[(0, 'draft'), (1, 'submitted'), (2, 'cancelled')], default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reason', models.CharField(choices=[('found_later', 'Found later — the pieces did arrive'), ('lost_in_transit', 'Lost in transit — written off'), ('wrongly_scanned', 'Wrongly scanned — never left the sender')], max_length=20)),
                ('note', models.TextField(blank=True, default='')),
                ('approved_by', models.ForeignKey(blank=True, help_text='Stamped by the approvals inbox on approve — never typed (#70).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='gap_closures_approved', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='gap_closures_created', to=settings.AUTH_USER_MODEL)),
                ('series', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='core.voucherseries')),
                ('store', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='gap_closures', to='masters.store')),
                ('transfer', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='gap_closure', to='outbound.storetransfer')),
            ],
            options={
                'db_table': 'outbound_transfer_gap_closure',
                'ordering': ['-created_at'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='TransferGapClosureLine',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('sku_code', models.CharField(max_length=64)),
                ('design', models.CharField(blank=True, default='', max_length=120)),
                ('color', models.CharField(blank=True, default='', max_length=60)),
                ('size', models.CharField(blank=True, default='', max_length=24)),
                ('brand', models.CharField(blank=True, default='', max_length=120)),
                ('season', models.CharField(blank=True, default='', max_length=120)),
                ('item', models.CharField(blank=True, default='', max_length=120)),
                ('hsn', models.CharField(blank=True, default='', max_length=24)),
                ('qty', models.IntegerField()),
                ('unit_cost_paise', core.money.MoneyField(default=0)),
                ('closure', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lines', to='outbound.transfergapclosure')),
            ],
            options={
                'db_table': 'outbound_transfer_gap_closure_line',
                'ordering': ['id'],
            },
        ),
        migrations.CreateModel(
            name='TransferReceiptException',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('kind', models.CharField(choices=[('short', 'Short — sent but not scanned in'), ('extra', 'Extra / wrong item — not on this transfer'), ('damaged', 'Damaged on arrival — into quarantine')], max_length=12)),
                ('sku_code', models.CharField(max_length=64)),
                ('design', models.CharField(blank=True, default='', max_length=120)),
                ('color', models.CharField(blank=True, default='', max_length=60)),
                ('size', models.CharField(blank=True, default='', max_length=24)),
                ('brand', models.CharField(blank=True, default='', max_length=120)),
                ('season', models.CharField(blank=True, default='', max_length=120)),
                ('item', models.CharField(blank=True, default='', max_length=120)),
                ('hsn', models.CharField(blank=True, default='', max_length=24)),
                ('qty', models.IntegerField()),
                ('unit_cost_paise', core.money.MoneyField(default=0)),
                ('note', models.CharField(blank=True, default='', max_length=240)),
                ('receipt', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='exceptions', to='outbound.transferreceipt')),
            ],
            options={
                'db_table': 'outbound_transfer_receipt_exception',
                'ordering': ['kind', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='transfergapclosure',
            constraint=models.CheckConstraint(condition=models.Q(('docstatus', 0), ('doc_number__isnull', False), _connector='OR'), name='outbound_transfergapclosure_posted_has_number'),
        ),
        migrations.AddConstraint(
            model_name='transfergapclosure',
            constraint=models.CheckConstraint(condition=models.Q(('docstatus__in', [0, 1, 2])), name='outbound_transfergapclosure_docstatus_domain'),
        ),
    ]
