"""Expose one validated operational-journal batch, never per-table append calls."""

from django.db import migrations

FORWARD = """
REVOKE EXECUTE ON FUNCTION kdps_append_journalbatch(jsonb) FROM kdps_app;
REVOKE EXECUTE ON FUNCTION kdps_append_quantitylegs(jsonb) FROM kdps_app;
REVOKE EXECUTE ON FUNCTION kdps_append_valuelegs(jsonb) FROM kdps_app;
REVOKE EXECUTE ON FUNCTION kdps_append_encumbrancelegs(jsonb) FROM kdps_app;

CREATE OR REPLACE FUNCTION kdps_append_operational_batch(
    batches jsonb, quantity_rows jsonb, value_rows jsonb, encumbrance_rows jsonb
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    IF jsonb_array_length(batches) < 1 THEN
        RAISE EXCEPTION 'an operational posting needs at least one journal batch'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
        WHERE b.posting_kind NOT IN (
            'P01','P02','P03','P04','P05','P06','P07','P08','P09',
            'P10','P11','P12','P13','P14','P15','P16','P17','P18'
        )
    ) THEN
        RAISE EXCEPTION 'invalid operational posting kind' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
        LEFT JOIN core_officialversion v ON v.id = b.version_id
        WHERE v.id IS NULL OR v.tenant_id <> b.tenant_id
    ) THEN
        RAISE EXCEPTION 'invalid operational source version' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        GROUP BY l.batch_id, l.pair_key
        HAVING count(*) <> 2 OR sum(l.amount) <> 0 OR count(DISTINCT l.origin_id) <> 1
            OR count(DISTINCT (l.bucket, l.leg_site_id)) <> 2
    ) THEN
        RAISE EXCEPTION 'false operational value pair' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_quantityleg, quantity_rows) l
        LEFT JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id AND b.tenant_id = l.tenant_id
        WHERE b.id IS NULL
    ) OR EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_valueleg, value_rows) l
        LEFT JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id AND b.tenant_id = l.tenant_id
        WHERE b.id IS NULL
    ) OR EXISTS (
        SELECT 1 FROM jsonb_populate_recordset(NULL::stockledger_encumbranceleg, encumbrance_rows) l
        LEFT JOIN jsonb_populate_recordset(NULL::stockledger_journalbatch, batches) b
          ON b.id = l.batch_id AND b.tenant_id = l.tenant_id
        WHERE b.id IS NULL
    ) THEN
        RAISE EXCEPTION 'operational legs belong to another batch' USING ERRCODE = '23514';
    END IF;

    PERFORM kdps_append_journalbatch(batches);
    IF jsonb_array_length(quantity_rows) > 0 THEN
        PERFORM kdps_append_quantitylegs(quantity_rows);
    END IF;
    IF jsonb_array_length(value_rows) > 0 THEN
        PERFORM kdps_append_valuelegs(value_rows);
    END IF;
    IF jsonb_array_length(encumbrance_rows) > 0 THEN
        PERFORM kdps_append_encumbrancelegs(encumbrance_rows);
    END IF;
END $$;

ALTER FUNCTION kdps_append_operational_batch(jsonb, jsonb, jsonb, jsonb) OWNER TO kdps_ledger;
REVOKE ALL ON FUNCTION kdps_append_operational_batch(jsonb, jsonb, jsonb, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kdps_append_operational_batch(jsonb, jsonb, jsonb, jsonb) TO kdps_app;
"""

REVERSE = """
DROP FUNCTION IF EXISTS kdps_append_operational_batch(jsonb, jsonb, jsonb, jsonb);
GRANT EXECUTE ON FUNCTION kdps_append_journalbatch(jsonb) TO kdps_app;
GRANT EXECUTE ON FUNCTION kdps_append_quantitylegs(jsonb) TO kdps_app;
GRANT EXECUTE ON FUNCTION kdps_append_valuelegs(jsonb) TO kdps_app;
GRANT EXECUTE ON FUNCTION kdps_append_encumbrancelegs(jsonb) TO kdps_app;
"""


class Migration(migrations.Migration):
    dependencies = [("stockledger", "0010_goods_journal_append_functions")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
