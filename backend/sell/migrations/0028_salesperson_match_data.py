"""Ticket 07, step 2 of 3: freeze the old salesperson rows.

1. Every row of the old salesperson table is copied, id for id, into
   `sell_salesperson_match` - store, code, name and whether it was active.
2. Every past sale line that names an old row now also names its frozen copy,
   with the seller's code and name written onto the line as they were. The line
   keeps its old column too until 0029; nothing about who sold it changes.

Then it checks its own work and refuses (rolling everything back) if any line
lost its seller, or if any bill's recorded default seller is not the first
seller on its lines - which is what the bill's default is derived from once
0029 drops that column.

Matching is not done here: a match is a change with an audit record, before and
after, which a migration cannot write. ``manage.py match_salespeople`` runs the
rule afterwards, one audited command per match, and Admin resolves the rest on
Setup, Salesperson Matches.

Plain SQL throughout: a year of bills is hundreds of thousands of lines, and
one UPDATE ... FROM is what keeps this inside a deploy (rehearsal:
``manage.py rehearse_salesperson_move``).
"""

from __future__ import annotations

from django.db import migrations

COPY_ROWS = """
    INSERT INTO sell_salesperson_match
        (id, store_id, code, name, was_active, staff_id, rule, matched_at, revision)
    SELECT id, store_id, code, name, is_active, NULL, '', NULL, 1
    FROM sell_salesman
    ON CONFLICT (id) DO NOTHING
"""

FREEZE_LINES = """
    UPDATE sell_sale_line l
    SET salesperson_match_id = m.id,
        salesperson_code = m.code,
        salesperson_name = m.name
    FROM sell_salesperson_match m
    WHERE l.salesman_id = m.id
"""

LINES_LOST = """
    SELECT count(*) FROM sell_sale_line
    WHERE salesman_id IS NOT NULL AND salesperson_match_id IS DISTINCT FROM salesman_id
"""

# The bill's default seller was always written as the first line that named one
# (`accept._write_sale`). 0029 drops the column and derives it from the lines,
# so every bill where the two would disagree is counted here first.
DEFAULTS_THAT_DIFFER = """
    WITH firsts AS (
        SELECT DISTINCT ON (sale_id) sale_id, salesman_id
        FROM sell_sale_line WHERE salesman_id IS NOT NULL ORDER BY sale_id, id
    )
    SELECT count(*) FROM sell_sale s
    LEFT JOIN firsts f ON f.sale_id = s.id
    WHERE s.salesman_default_id IS NOT NULL
      AND s.salesman_default_id IS DISTINCT FROM f.salesman_id
"""


def forward(apps, schema_editor):  # type: ignore[no-untyped-def]
    with schema_editor.connection.cursor() as cursor:
        # Fresh statistics, so the joins below are planned for the volume that
        # is really there (a restored or newly loaded database may have none).
        cursor.execute("ANALYZE sell_sale")
        cursor.execute("ANALYZE sell_sale_line")
        cursor.execute(COPY_ROWS)
        cursor.execute(FREEZE_LINES)
        cursor.execute(LINES_LOST)
        lost = int(cursor.fetchone()[0])
        if lost:
            raise RuntimeError(
                f"{lost} sale line(s) would lose their salesperson; nothing was changed."
            )
        cursor.execute(DEFAULTS_THAT_DIFFER)
        differ = int(cursor.fetchone()[0])
        if differ:
            raise RuntimeError(
                f"{differ} bill(s) record a default salesperson that is not the first seller "
                "on their lines; dropping that column would lose it. Nothing was changed."
            )


def backward(apps, schema_editor):  # type: ignore[no-untyped-def]
    """Put the old rows and the old line column back (a rehearsal rolls back through here).

    Only lines sold before the move can go back: a line sold since names a staff
    record the old table never had, and 0027's check refuses it on the way down.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO sell_salesman (id, store_id, code, name, is_active, created_at, "
            "updated_at) SELECT id, store_id, code, name, was_active, now(), now() "
            "FROM sell_salesperson_match ON CONFLICT (id) DO NOTHING"
        )
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence('sell_salesman', 'id'), "
            "GREATEST((SELECT max(id) FROM sell_salesman), 1))"
        )
        cursor.execute(
            "UPDATE sell_sale_line SET salesman_id = salesperson_match_id "
            "WHERE salesperson_match_id IS NOT NULL AND salesman_id IS NULL"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("sell", "0027_salesperson_match"),
        ("masters", "0025_document_series"),
    ]

    operations = [migrations.RunPython(forward, backward)]
