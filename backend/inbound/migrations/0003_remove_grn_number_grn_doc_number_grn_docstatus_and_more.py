# Reparent GRN onto core.Document (data-preserving): copy the old `number` into the
# gap-free `doc_number`, mark existing receipts SUBMITTED, give each a unique
# idempotency_uuid, then install the docstatus FSM + TRUNCATE guard triggers.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from core.documents import document_fsm_reverse_sql, document_fsm_sql
from core.ledger import truncate_guard_reverse_sql, truncate_guard_sql

TABLE = "inbound_grn"


def forwards(apps, schema_editor):
    Grn = apps.get_model("inbound", "Grn")
    for grn in Grn.objects.all():
        grn.idempotency_uuid = uuid.uuid4()
        grn.doc_number = grn.number
        grn.docstatus = 1  # existing GRNs are posted receipts (immutable facts)
        grn.save(update_fields=["idempotency_uuid", "doc_number", "docstatus"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_glentry"),
        ("files", "0001_initial"),
        ("inbound", "0002_initial"),
        ("masters", "0001_initial"),
        ("vendors", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="grn",
            name="doc_number",
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="grn",
            name="docstatus",
            field=models.IntegerField(
                choices=[(0, "draft"), (1, "submitted"), (2, "cancelled")], default=0
            ),
        ),
        migrations.AddField(
            model_name="grn",
            name="series",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT, to="core.voucherseries",
            ),
        ),
        # idempotency_uuid: add nullable + non-unique first so existing rows can each
        # get their own value, THEN tighten to unique (a unique callable-default add
        # would assign the same uuid to every existing row → collision).
        migrations.AddField(
            model_name="grn",
            name="idempotency_uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name="grn", name="number"),
        migrations.AlterField(
            model_name="grn",
            name="idempotency_uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddConstraint(
            model_name="grn",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("docstatus", 0), ("doc_number__isnull", False), _connector="OR"
                ),
                name="inbound_grn_posted_has_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="grn",
            constraint=models.CheckConstraint(
                condition=models.Q(("docstatus__in", [0, 1, 2])),
                name="inbound_grn_docstatus_domain",
            ),
        ),
        migrations.AlterModelTable(name="grn", table=TABLE),
        migrations.RunSQL(
            sql=document_fsm_sql(TABLE), reverse_sql=document_fsm_reverse_sql(TABLE)
        ),
        migrations.RunSQL(
            sql=truncate_guard_sql(TABLE), reverse_sql=truncate_guard_reverse_sql(TABLE)
        ),
    ]
