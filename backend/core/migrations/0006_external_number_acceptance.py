# D10 slice #176 — the till assigns the sale's number, so the kernel has to be
# able to *accept* a number instead of always handing one out.
#
# Two things, both about the same guarantee:
#
#  * `DocumentProbe.external_seq` — the kernel's own probe document gains the
#    till's shape, so the anti-cheat suite can prove exactly-once acceptance
#    against a real table with a real unique constraint.
#  * the voucher-series guard is re-stated (CREATE OR REPLACE, no table churn):
#    a rewind is now refused outright and named as such, and a jump — which a
#    till accept needs when earlier bills have not synced — is legal only for an
#    externally-numbered series, only when `accept_external()` has declared the
#    sequence it is accepting for the length of its transaction, and only to
#    exactly that sequence plus one. Raw SQL and `QuerySet.update()` declare
#    nothing, so they stay refused.

from django.db import migrations, models

from core.documents import voucher_series_guard_reverse_sql, voucher_series_guard_sql

VOUCHER_SERIES_TABLE = "core_voucher_series"


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_glentry'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentprobe',
            name='external_seq',
            field=models.IntegerField(blank=True, null=True),
        ),
        # Drop and re-create, exactly as 0004 did for the FSM trigger: the
        # function is CREATE OR REPLACE'd with the new branch, and the trigger is
        # re-bound rather than duplicated.
        migrations.RunSQL(
            sql=(
                voucher_series_guard_reverse_sql(VOUCHER_SERIES_TABLE)
                + voucher_series_guard_sql(VOUCHER_SERIES_TABLE)
            ),
            reverse_sql=voucher_series_guard_reverse_sql(VOUCHER_SERIES_TABLE),
        ),
    ]
