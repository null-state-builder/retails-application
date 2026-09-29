# Store operations ticket 21 (ST-ORD-2): special orders. Their advance uses ticket
# 20's receipt voucher and advance rows, which now name a reservation or a special
# order (never both), and a bill's advance tender names the one it collects.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_customer_reservation'),
        ('masters', '0030_customer_merge_and_numbers'),
        ('offers', '0004_seed_offer_approval_policy'),
        ('outbound', '0045_merge_0044_size_balancing_0044_transfer_document'),
        ('sell', '0042_merge_20260928_1558'),
        ('vendors', '0010_bookingline_cost'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SpecialOrder',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('ref', models.CharField(max_length=32)),
                ('customer_name', models.CharField(max_length=120)),
                ('customer_mobile', models.CharField(db_index=True, max_length=15)),
                ('style', models.CharField(max_length=80)),
                ('size', models.CharField(max_length=24)),
                ('colour', models.CharField(max_length=40)),
                ('note', models.CharField(blank=True, default='', max_length=200)),
                ('status', models.CharField(choices=[('asked', 'Asked'), ('ordered', 'Ordered'), ('arrived', 'Arrived'), ('told', 'Customer told'), ('collected', 'Collected'), ('cancelled', 'Cancelled')], default='asked', max_length=10)),
                ('asked_on', models.DateField()),
                ('advance_policy', models.CharField(choices=[('refund', 'Refunded'), ('keep', 'Kept')], max_length=8)),
                ('terms', models.TextField()),
                ('route', models.CharField(blank=True, choices=[('transfer', 'Transfer request'), ('booking', 'Booking line')], default='', max_length=10)),
                ('ordered_barcode', models.CharField(blank=True, default='', max_length=64)),
                ('booking_line_key', models.UUIDField(blank=True, null=True)),
                ('ordered_at', models.DateTimeField(blank=True, null=True)),
                ('arrived_barcode', models.CharField(blank=True, default='', max_length=64)),
                ('arrived_at', models.DateTimeField(blank=True, null=True)),
                ('told_how', models.CharField(blank=True, choices=[('call', 'Phone call'), ('message', 'Message'), ('in_person', 'In person')], default='', max_length=10)),
                ('told_at', models.DateTimeField(blank=True, null=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('close_reason', models.CharField(blank=True, choices=[('collected', 'Collected on a bill'), ('customer_cancelled', 'Cancelled by the customer'), ('store_cancelled', 'Cancelled by the store')], default='', max_length=20)),
            ],
            options={
                'db_table': 'sell_special_order',
                'ordering': ['created_at'],
            },
        ),
        migrations.RemoveConstraint(
            model_name='saletender',
            name='ck_saletender_reservation_iff_advance',
        ),
        migrations.AlterField(
            model_name='advancemovement',
            name='reservation',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='advance_movements', to='sell.customerreservation'),
        ),
        migrations.AlterField(
            model_name='receiptvoucher',
            name='reservation',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='voucher', to='sell.customerreservation'),
        ),
        migrations.AlterField(
            model_name='saletender',
            name='mode',
            field=models.CharField(choices=[('cash', 'Cash'), ('card', 'Card'), ('upi', 'UPI'), ('credit_note', 'Credit note'), ('bank_offer', 'Bank offer'), ('advance', 'Advance paid earlier')], max_length=12),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='arrived_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='booking',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='vendors.goodsbooking'),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='brand',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='masters.brand'),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='closed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='ordered_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='sale',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='sell.sale'),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='store',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='masters.store'),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='told_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='specialorder',
            name='transfer_request',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='outbound.transferrequest'),
        ),
        migrations.AddField(
            model_name='advancemovement',
            name='special_order',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='advance_movements', to='sell.specialorder'),
        ),
        migrations.AddField(
            model_name='receiptvoucher',
            name='special_order',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='voucher', to='sell.specialorder'),
        ),
        migrations.AddField(
            model_name='saletender',
            name='special_order',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='sell.specialorder'),
        ),
        migrations.AddConstraint(
            model_name='advancemovement',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('reservation__isnull', False), ('special_order__isnull', True)), models.Q(('reservation__isnull', True), ('special_order__isnull', False)), _connector='OR'), name='ck_advance_one_holder'),
        ),
        migrations.AddConstraint(
            model_name='receiptvoucher',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('reservation__isnull', False), ('special_order__isnull', True)), models.Q(('reservation__isnull', True), ('special_order__isnull', False)), _connector='OR'), name='ck_receiptvoucher_one_holder'),
        ),
        migrations.AddConstraint(
            model_name='saletender',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('mode', 'advance'), models.Q(models.Q(('reservation__isnull', False), ('special_order__isnull', True)), models.Q(('reservation__isnull', True), ('special_order__isnull', False)), _connector='OR')), models.Q(models.Q(('mode', 'advance'), _negated=True), ('reservation__isnull', True), ('special_order__isnull', True)), _connector='OR'), name='ck_saletender_holder_iff_advance'),
        ),
        migrations.AddIndex(
            model_name='specialorder',
            index=models.Index(fields=['store', 'status'], name='sell_sord_store_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='specialorder',
            constraint=models.UniqueConstraint(fields=('store', 'ref'), name='uq_special_order_store_ref'),
        ),
        migrations.AddConstraint(
            model_name='specialorder',
            constraint=models.CheckConstraint(condition=models.Q(('advance_policy__in', ['refund', 'keep'])), name='ck_special_order_policy'),
        ),
        migrations.AddConstraint(
            model_name='specialorder',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('booking__isnull', True), ('route', ''), ('transfer_request__isnull', True)), models.Q(('booking__isnull', True), ('route', 'transfer'), ('transfer_request__isnull', False)), models.Q(('booking__isnull', False), ('booking_line_key__isnull', False), ('route', 'booking'), ('transfer_request__isnull', True)), _connector='OR'), name='ck_special_order_route_links'),
        ),
        migrations.AddConstraint(
            model_name='specialorder',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('status__in', ['ordered', 'arrived', 'told', 'collected']), _negated=True), models.Q(('route', ''), _negated=True), _connector='OR'), name='ck_special_order_ordered_has_route'),
        ),
        migrations.AddConstraint(
            model_name='specialorder',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('status__in', ['arrived', 'told', 'collected']), _negated=True), models.Q(('arrived_barcode', ''), _negated=True), _connector='OR'), name='ck_special_order_arrived_has_piece'),
        ),
        migrations.AddConstraint(
            model_name='specialorder',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('closed_at__isnull', False), ('status__in', ['collected', 'cancelled']), models.Q(('close_reason', ''), _negated=True)), models.Q(('close_reason', ''), ('closed_at__isnull', True), ('status__in', ['asked', 'ordered', 'arrived', 'told'])), _connector='OR'), name='ck_special_order_closed_iff_ended'),
        ),
        migrations.AddConstraint(
            model_name='specialorder',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('sale__isnull', True), ('status__in', ['asked', 'ordered', 'arrived', 'told', 'cancelled'])), models.Q(('sale__isnull', False), ('status', 'collected')), _connector='OR'), name='ck_special_order_sale_iff_collected'),
        ),
    ]
