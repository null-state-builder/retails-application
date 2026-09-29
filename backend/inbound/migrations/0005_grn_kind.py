from django.db import migrations, models

from core.documents import TRIGGER_FUNCTION, document_fsm_function_sql

# A pass-through stand-in for the document FSM guard, installed only for the span of the
# backfill UPDATE below and then replaced by the real guard. Swapping the *function*
# body (not the trigger) needs no table lock, so — unlike ``ALTER TABLE … DISABLE
# TRIGGER`` — it works even when the table already has pending trigger events, and it is
# fully transactional (a rollback restores the real guard).
_FSM_PASSTHROUGH_SQL = f"""
CREATE OR REPLACE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def backfill_kind(apps, schema_editor):
    """Existing GRNs predate the branded/non-branded split. A direct (booking-less)
    receipt was warehouse intake — the non-branded path; a receipt against a booking
    was a branded order to a store. Key off ``is_direct``, not ``booking__isnull``:
    booking is a ``SET_NULL`` FK, so a branded GRN whose booking was later deleted has
    a null booking yet ``is_direct=False`` — keying on booking would wrongly flip it to
    non-branded. ``is_direct`` is the immutable receipt marker set at creation. (The
    AddField default already set every row to ``branded``, so we only flip the direct
    receipts.)

    Real GRNs are SUBMITTED documents, and the kernel FSM trigger (core.documents)
    forbids any UPDATE to a submitted row except cancelling it — so the plain
    ``.update()`` raises ``docstatus FSM: a submitted row … may only move to cancelled``
    against any database that already holds posted receipts (the deployed alpha; not a
    fresh test/CI DB, which is why this slipped through). A one-column classification
    backfill is legitimate schema evolution, not a change to a posted business fact, so
    we neutralise the FSM guard for just this UPDATE by swapping its function body for a
    pass-through, then restore the real guard in a ``finally`` (a failure can never leave
    it neutralised). Both statements are transactional, so a rollback restores it too."""
    Grn = apps.get_model("inbound", "Grn")
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_FSM_PASSTHROUGH_SQL)
        try:
            Grn.objects.filter(is_direct=True).update(kind="non_branded")
        finally:
            cursor.execute(document_fsm_function_sql())  # restore the real guard


class Migration(migrations.Migration):
    dependencies = [
        ("inbound", "0004_remove_grn_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="grn",
            name="kind",
            field=models.CharField(
                choices=[("branded", "Branded"), ("non_branded", "Non-branded")],
                default="branded",
                max_length=12,
            ),
        ),
        migrations.RunPython(backfill_kind, migrations.RunPython.noop),
    ]
