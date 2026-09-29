"""Install the docstatus-FSM trigger on the mark-damaged document table."""

from django.db import migrations

from core.documents import document_fsm_reverse_sql, document_fsm_sql

TABLE = "outbound_mark_damaged"


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0004_markdamaged_markdamagedline_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=document_fsm_sql(TABLE),
            reverse_sql=document_fsm_reverse_sql(TABLE),
        ),
    ]
