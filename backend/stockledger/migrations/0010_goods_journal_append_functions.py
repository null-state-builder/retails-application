# Goods-v1 operational journal protection (design §7.1, G17).
#
# The application role cannot INSERT journal rows (see core.schema_guards). These
# four fixed functions, owned by `kdps_ledger`, are the only way in: a fixed
# search_path, no dynamic table names, INSERT only. Row-level security still
# applies inside them, so a batch can only be written for the bound tenant.
#
# Each leg table also checks, once per inserting statement, that every pair is
# exactly one source and one destination leg that cancel out on the same lot and
# portion - so a direct or buggy writer cannot leave half a pair behind.

from django.db import migrations

APPEND = """
CREATE OR REPLACE FUNCTION {name}(rows jsonb) RETURNS void
    LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
    AS $fn$ INSERT INTO {table} SELECT * FROM jsonb_populate_recordset(NULL::{table}, rows) $fn$;
ALTER FUNCTION {name}(jsonb) OWNER TO kdps_ledger;
REVOKE ALL ON FUNCTION {name}(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {name}(jsonb) TO kdps_app;
"""

FUNCTIONS = [
    ("kdps_append_journalbatch", "stockledger_journalbatch"),
    ("kdps_append_quantitylegs", "stockledger_quantityleg"),
    ("kdps_append_valuelegs", "stockledger_valueleg"),
    ("kdps_append_encumbrancelegs", "stockledger_encumbranceleg"),
]

BALANCE = """
CREATE OR REPLACE FUNCTION kdps_quantity_pairs_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM new_rows GROUP BY batch_id, pair_key
        HAVING count(*) <> 2 OR sum(qty) <> 0 OR count(DISTINCT lot_id) <> 1
            OR count(DISTINCT portion) <> 1
            OR bool_and(upper(portion) - lower(portion) = abs(qty)) IS NOT TRUE
    ) THEN
        RAISE EXCEPTION 'unbalanced operational quantity pair' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION kdps_value_pairs_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM new_rows GROUP BY batch_id, pair_key
        HAVING count(*) <> 2 OR sum(amount) <> 0 OR count(DISTINCT origin_id) <> 1
    ) THEN
        RAISE EXCEPTION 'unbalanced operational value pair' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION kdps_encumbrance_pairs_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM new_rows GROUP BY batch_id, pair_key
        HAVING count(*) <> 2 OR sum(qty) <> 0 OR count(DISTINCT lot_id) <> 1
            OR count(DISTINCT portion) <> 1 OR count(DISTINCT axis) <> 1
            OR count(DISTINCT control_key) <> 1 OR count(DISTINCT gate) <> 2
    ) THEN
        RAISE EXCEPTION 'unbalanced operational encumbrance pair' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS stockledger_quantityleg_pairs ON stockledger_quantityleg;
CREATE TRIGGER stockledger_quantityleg_pairs AFTER INSERT ON stockledger_quantityleg
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT
    EXECUTE FUNCTION kdps_quantity_pairs_balance();
DROP TRIGGER IF EXISTS stockledger_valueleg_pairs ON stockledger_valueleg;
CREATE TRIGGER stockledger_valueleg_pairs AFTER INSERT ON stockledger_valueleg
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT
    EXECUTE FUNCTION kdps_value_pairs_balance();
DROP TRIGGER IF EXISTS stockledger_encumbranceleg_pairs ON stockledger_encumbranceleg;
CREATE TRIGGER stockledger_encumbranceleg_pairs AFTER INSERT ON stockledger_encumbranceleg
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT
    EXECUTE FUNCTION kdps_encumbrance_pairs_balance();
"""

REVERSE = "\n".join(f"DROP FUNCTION IF EXISTS {name}(jsonb);" for name, _ in FUNCTIONS)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_goods_roles_extensions"),
        ("stockledger", "0009_acceptancesession_allocationguard_custodylot_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            "".join(APPEND.format(name=name, table=table) for name, table in FUNCTIONS),
            REVERSE,
        ),
        migrations.RunSQL(BALANCE, migrations.RunSQL.noop),
    ]
