"""The ledger learns the disposal's own posting kind, P21 (goods ticket 15D).

A disposal records the actual physical removal of goods from company custody -
destruction or handover for scrap/recycling (quarantine outcomes PRD §7.1-§7.2;
goods PRD §14.9.2, §14.10 GSA-R01/GSA-R05). It removes exactly the disposed
pieces, and it recognises their value loss only when no write-off (P20) already
did. The database says so itself rather than trusting the one service that
posts it:

* **to the disposed boundary only** - a P21 quantity leg ends at ``disposed``;
* **stock -> external only** - value leaves the site's stock, nothing else;
* **once** - a portion whose loss a P20 or a P21 already recognised cannot be
  recognised again by a P21, and a P20 cannot recognise again what a P21 did
  (nor two pairs of the same P21 batch the same portion).

The amount check 0012/0015 wrote applies unchanged: a P21 value leg is its
pieces times its origin's own frozen unit cost - the recorded layer cost.

Built from 0019's text rather than restated, the way every amendment to this
function since 0011 has been.
"""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("stockledger.migrations.0019_write_off_posting_kind").FORWARD

KINDS = "'P10','P11','P12','P13','P14','P15','P16','P17','P18','P19','P20'"
APPEND = "    PERFORM kdps_append_journalbatch(batches);\n"
GUARDS = """    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_quantityleg, quantity_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        WHERE b.posting_kind = 'P21' AND l.side = 'destination'
          AND coalesce(l.position->>'boundary', '') <> 'disposed'
    ) THEN
        RAISE EXCEPTION 'a disposal moves goods to the disposed boundary only'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        WHERE b.posting_kind = 'P21' AND (
            (l.side = 'source' AND l.bucket <> 'stock')
            OR (l.side = 'destination' AND l.bucket <> 'external')
        )
    ) THEN
        RAISE EXCEPTION 'a disposal takes value from stock to external only'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        WHERE b.posting_kind IN ('P20', 'P21') AND l.side = 'source' AND (
            EXISTS (
                SELECT 1 FROM stockledger_valueleg e
                JOIN stockledger_journalbatch eb ON eb.id = e.batch_id
                WHERE eb.posting_kind IN ('P20', 'P21') AND e.side = 'source'
                  AND (b.posting_kind = 'P21' OR eb.posting_kind = 'P21')
                  AND e.tenant_id = l.tenant_id AND e.lot_id = l.lot_id
                  AND e.portion && l.portion
            )
            OR (b.posting_kind = 'P21' AND EXISTS (
                SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) o
                WHERE o.batch_id = l.batch_id AND o.side = 'source'
                  AND o.pair_key <> l.pair_key AND o.lot_id = l.lot_id
                  AND o.portion && l.portion
            ))
        )
    ) THEN
        RAISE EXCEPTION 'this loss was already recognised' USING ERRCODE = '23514';
    END IF;

"""

for text, expected in ((KINDS, 1), (APPEND, 1)):
    if PREVIOUS.count(text) != expected:  # pragma: no cover - guards the text this builds on
        raise RuntimeError("the batch function is no longer shaped as this migration expects")

FORWARD = PREVIOUS.replace(KINDS, KINDS + ",'P21'").replace(APPEND, GUARDS + APPEND)


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0019_write_off_posting_kind")]
    operations = [migrations.RunSQL(FORWARD, PREVIOUS)]
