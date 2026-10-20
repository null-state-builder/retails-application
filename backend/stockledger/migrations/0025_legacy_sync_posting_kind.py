"""The ledger learns the store test run's legacy sync posting kind, P22.

P22 puts a test tenant's test bills back and brings the store to the legacy
software's book (``ptmapper.stock_sync_services`` / ``legacy_sales_services``).
The database admits it on the same terms as every other kind, plus one guard
of its own: a P22 batch is refused unless its tenant is synthetic, so a real
tenant's ledger can never carry a legacy sync whatever the application does.
"""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("stockledger.migrations.0020_disposal_posting_kind").FORWARD

KINDS = "'P10','P11','P12','P13','P14','P15','P16','P17','P18','P19','P20','P21'"
APPEND = "    PERFORM kdps_append_journalbatch(batches);\n"
GUARDS = """    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
        JOIN masters_tenant t ON t.id = b.tenant_id
        WHERE b.posting_kind = 'P22' AND NOT t.synthetic
    ) THEN
        RAISE EXCEPTION 'a legacy sync runs only on a test tenant' USING ERRCODE = '23514';
    END IF;

"""

for text, expected in ((KINDS, 1), (APPEND, 1)):
    if PREVIOUS.count(text) != expected:  # pragma: no cover - guards the text this builds on
        raise RuntimeError("the batch function is no longer shaped as this migration expects")

FORWARD = PREVIOUS.replace(KINDS, KINDS + ",'P22'").replace(APPEND, GUARDS + APPEND)


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0024_stockledgerentry_brand_ref")]
    operations = [migrations.RunSQL(FORWARD, PREVIOUS)]
