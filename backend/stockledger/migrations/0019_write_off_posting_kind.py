"""The ledger learns the write-off's own posting kind, P20 (goods ticket 15C).

A write-off recognises the loss of an established recorded value while the goods
stay physically where they are (quarantine outcomes PRD §7.1; goods PRD §14.10
GSA-R05). So P20 is a value posting that never moves a piece, and the database
says so itself rather than trusting the one service that posts it:

* **no quantity leg** - a write-off alone never records a physical departure;
* **stock -> external only** - value leaves the site's stock, nothing else;
* **once** - a portion of a lot whose loss a P20 already recognised cannot be
  recognised again, by a second batch or by two pairs of the same batch.

The amount check 0012/0015 wrote applies unchanged: a P20 value leg is its
pieces times its origin's own frozen unit cost - the recorded layer cost.

Built from 0017's text rather than restated, the way every amendment to this
function since 0011 has been.
"""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("stockledger.migrations.0017_sale_posting_kind").FORWARD

KINDS = "'P10','P11','P12','P13','P14','P15','P16','P17','P18','P19'"
APPEND = "    PERFORM kdps_append_journalbatch(batches);\n"
GUARDS = """    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_quantityleg, quantity_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        WHERE b.posting_kind = 'P20'
    ) THEN
        RAISE EXCEPTION 'a write-off moves no quantity' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        WHERE b.posting_kind = 'P20' AND (
            (l.side = 'source' AND l.bucket <> 'stock')
            OR (l.side = 'destination' AND l.bucket <> 'external')
        )
    ) THEN
        RAISE EXCEPTION 'a write-off takes value from stock to external only'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        WHERE b.posting_kind = 'P20' AND l.side = 'source' AND (
            EXISTS (
                SELECT 1 FROM stockledger_valueleg e
                JOIN stockledger_journalbatch eb ON eb.id = e.batch_id
                WHERE eb.posting_kind = 'P20' AND e.side = 'source'
                  AND e.tenant_id = l.tenant_id AND e.lot_id = l.lot_id
                  AND e.portion && l.portion
            )
            OR EXISTS (
                SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) o
                WHERE o.batch_id = l.batch_id AND o.side = 'source'
                  AND o.pair_key <> l.pair_key AND o.lot_id = l.lot_id
                  AND o.portion && l.portion
            )
        )
    ) THEN
        RAISE EXCEPTION 'this loss was already recognised' USING ERRCODE = '23514';
    END IF;

"""

for text, expected in ((KINDS, 1), (APPEND, 1)):
    if PREVIOUS.count(text) != expected:  # pragma: no cover - guards the text this builds on
        raise RuntimeError("the batch function is no longer shaped as this migration expects")

FORWARD = PREVIOUS.replace(KINDS, KINDS + ",'P20'").replace(APPEND, GUARDS + APPEND)


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0018_remove_stockledgerentry_pt_file")]
    operations = [migrations.RunSQL(FORWARD, PREVIOUS)]
