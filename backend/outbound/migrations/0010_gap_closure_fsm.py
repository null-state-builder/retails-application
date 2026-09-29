"""Install the docstatus-FSM trigger on the gap-closure document table."""

from django.db import migrations

from core.documents import document_fsm_reverse_sql, document_fsm_sql

TABLE = "outbound_transfer_gap_closure"


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0009_receive_exceptions_and_gap_closure"),
    ]

    operations = [
        migrations.RunSQL(
            sql=document_fsm_sql(TABLE),
            reverse_sql=document_fsm_reverse_sql(TABLE),
        ),
    ]
