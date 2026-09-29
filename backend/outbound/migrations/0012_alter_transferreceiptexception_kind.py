"""A damaged arrival is a damage *flag* now, not a quarantining (#138).

Schema-wise this is one label. The question it raises is what becomes of the
quarantine the old receive path already wrote, because #138's returnable pool is
built from *confirmed* quarantine and those legs have no confirmer: they were the
receiver's scan, straight into quarantine, with no damage document behind them.

**Nothing is migrated, deliberately.** Two reasons, in order:

* No such row exists to migrate. The receive path that wrote them (#115) landed
  on ``main`` the day before this, and the alpha deploys ``client-testing``,
  which is many commits behind it — so no database anyone can point at has one.
* If one did, it would stay. Those legs are posted ledger, and the ledger is
  append-only; the way to undo a posting is a reversing document, not an
  ``UPDATE``. Nor is there a confirmer to name: the same reasoning as
  ``0007_backfill_approvals``, which left posted documents alone rather than
  invent a second signature for them after the fact. They would read as
  quarantine confirmed by the receiver under the rule then in force, which is
  what they are.

No MarkDamaged draft can predate this either — before #138 ``mark_damaged``
created and posted in one atomic block, so it never left one behind. That is why
this needs no companion to ``0007``'s backfill.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('outbound', '0011_markdamaged_confirmed_by_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transferreceiptexception',
            name='kind',
            field=models.CharField(choices=[('short', 'Short — sent but not scanned in'), ('extra', 'Extra / wrong item — not on this transfer'), ('damaged', 'Damaged on arrival — raised as a damage flag')], max_length=12),
        ),
    ]
