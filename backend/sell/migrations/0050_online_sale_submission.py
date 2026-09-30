from django.db import migrations, models
import django.db.models.deletion
import uuid
import django.db.models.functions.datetime


class Migration(migrations.Migration):
    dependencies = [("sell", "0049_so03_brand_identity")]
    operations = [migrations.CreateModel(name="OnlineSaleSubmission", fields=[
        ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="masters.tenant")),
        ("created_at", models.DateTimeField(db_default=django.db.models.functions.datetime.Now())),
        ("idempotency_uuid", models.UUIDField()),
        ("payload_fingerprint", models.CharField(max_length=64)),
        ("status", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("rejected", "Rejected")], default="pending", max_length=10)),
        ("rejection_code", models.CharField(blank=True, default="", max_length=50)),
        ("rejection_message", models.CharField(blank=True, default="", max_length=500)),
        ("rejection_status", models.IntegerField(default=422)),
        ("sale", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="sell.sale")),
        ("store", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="masters.store")),
    ], options={"constraints": [models.UniqueConstraint(fields=["tenant", "idempotency_uuid"], name="uq_online_sale_intent")]})]
