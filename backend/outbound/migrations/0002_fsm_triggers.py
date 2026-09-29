"""Install the docstatus-FSM trigger on all outbound document tables."""

from django.db import migrations

from core.documents import document_fsm_reverse_sql, document_fsm_sql

TABLES = [
    "outbound_store_transfer",
    "outbound_return_to_vendor",
    "outbound_stock_adjustment",
    "outbound_write_off",
    "outbound_vflip",
]


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0001_initial"),
        ("core", "0004_docstatus_domain_and_series_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql=document_fsm_sql(table),
            reverse_sql=document_fsm_reverse_sql(table),
        )
        for table in TABLES
    ]
