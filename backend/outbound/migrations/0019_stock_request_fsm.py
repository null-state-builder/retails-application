"""Install the docstatus-FSM trigger on the stock-request document table (#74)."""

from django.db import migrations

from core.documents import document_fsm_reverse_sql, document_fsm_sql

TABLE = "outbound_stock_request"


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0018_stock_request"),
    ]

    operations = [
        migrations.RunSQL(
            sql=document_fsm_sql(TABLE),
            reverse_sql=document_fsm_reverse_sql(TABLE),
        ),
    ]
