"""The validated operational batch also refuses balanced but false value (fix ticket 02).

0011 checked that each value pair balances, names one origin and moves between two
different places, but not that the amount is true. A caller holding EXECUTE could
post a balanced value pair on a GRN count, or a pair worth more than its pieces. Now:

* posting kinds whose catalogue entry carries no value (design §7.2: P01, P02, P07,
  P11, P12, P16, P18) may not carry value legs;
* every value leg names its custody lot and portion, and its amount is exactly the
  portion's pieces times the origin's frozen unit value - unit cost, or the evidenced
  MRP for a P03 ``value_damage`` decision (overall PRD §15.2.1 rule 4).
"""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("stockledger.migrations.0011_validated_operational_batch").FORWARD

VALUE_TRUTH = """
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        WHERE b.posting_kind IN ('P01','P02','P07','P11','P12','P16','P18')
    ) THEN
        RAISE EXCEPTION 'value on a posting kind that carries none' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id
        LEFT JOIN stockledger_origin o ON o.id = l.origin_id AND o.tenant_id = l.tenant_id
        WHERE o.id IS NULL OR l.lot_id IS NULL OR l.portion IS NULL OR isempty(l.portion)
            OR abs(l.amount) <> (upper(l.portion) - lower(l.portion))
                * CASE WHEN b.posting_kind = 'P03' THEN o.mrp ELSE o.unit_cost END
    ) THEN
        RAISE EXCEPTION 'false operational value amount' USING ERRCODE = '23514';
    END IF;
"""

ANCHOR = """        RAISE EXCEPTION 'false operational value pair' USING ERRCODE = '23514';
    END IF;
"""

if PREVIOUS.count(ANCHOR) != 1:  # pragma: no cover - guards the text this builds on
    raise RuntimeError("0011's batch function no longer has its value-pair check")

FORWARD = PREVIOUS.replace(ANCHOR, ANCHOR + VALUE_TRUTH)


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0011_validated_operational_batch")]
    operations = [migrations.RunSQL(FORWARD, PREVIOUS)]
