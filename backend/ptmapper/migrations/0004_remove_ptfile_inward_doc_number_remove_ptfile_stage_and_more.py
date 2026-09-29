# Reparent PtFile onto core.Document (data-preserving): derive docstatus + doc_number
# from the old stage/inward_doc_number, keep the pre-post sub-stage in draft_stage,
# give each row a unique idempotency_uuid, then install the FSM + TRUNCATE triggers.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from core.documents import document_fsm_reverse_sql, document_fsm_sql
from core.ledger import truncate_guard_reverse_sql, truncate_guard_sql

TABLE = "ptmapper_ptfile"


def forwards(apps, schema_editor):
    PtFile = apps.get_model("ptmapper", "PtFile")
    for pt in PtFile.objects.all():
        pt.idempotency_uuid = uuid.uuid4()
        if pt.stage == "posted" and pt.inward_doc_number:
            pt.docstatus = 1  # submitted (posted fact)
            pt.doc_number = pt.inward_doc_number
            pt.draft_stage = "sent"
        else:
            pt.docstatus = 0  # still a draft (mapping / sent)
            pt.doc_number = None
            pt.draft_stage = "sent" if pt.stage == "sent" else "mapping"
        pt.save(update_fields=["idempotency_uuid", "docstatus", "doc_number", "draft_stage"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_glentry"),
        ("files", "0001_initial"),
        ("ptmapper", "0003_ptfile_booking_ptfile_inward_doc_number"),
        ("vendors", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ptfile",
            name="doc_number",
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="ptfile",
            name="docstatus",
            field=models.IntegerField(
                choices=[(0, "draft"), (1, "submitted"), (2, "cancelled")], default=0
            ),
        ),
        migrations.AddField(
            model_name="ptfile",
            name="draft_stage",
            field=models.CharField(
                choices=[("mapping", "Mapping (Warehouse)"), ("sent", "Sent to Patna")],
                default="mapping", max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="ptfile",
            name="series",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT, to="core.voucherseries",
            ),
        ),
        migrations.AddField(
            model_name="ptfile",
            name="idempotency_uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name="ptfile", name="inward_doc_number"),
        migrations.RemoveField(model_name="ptfile", name="stage"),
        migrations.AlterField(
            model_name="ptfile",
            name="idempotency_uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddConstraint(
            model_name="ptfile",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("docstatus", 0), ("doc_number__isnull", False), _connector="OR"
                ),
                name="ptmapper_ptfile_posted_has_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="ptfile",
            constraint=models.CheckConstraint(
                condition=models.Q(("docstatus__in", [0, 1, 2])),
                name="ptmapper_ptfile_docstatus_domain",
            ),
        ),
        migrations.AlterModelTable(name="ptfile", table=TABLE),
        migrations.RunSQL(
            sql=document_fsm_sql(TABLE), reverse_sql=document_fsm_reverse_sql(TABLE)
        ),
        migrations.RunSQL(
            sql=truncate_guard_sql(TABLE), reverse_sql=truncate_guard_reverse_sql(TABLE)
        ),
    ]
