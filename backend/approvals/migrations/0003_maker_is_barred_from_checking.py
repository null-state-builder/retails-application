"""The document's maker is barred from checking it — even after a re-ask.

``requested_by`` alone was not enough: once a rejected draft can be asked again
by somebody else, that column names the *asker*, and the author of the document
would be free to approve their own work. ``made_by`` records the author once and
never moves, and the check constraint bars them for good.

Existing rows predate re-asking, so on them asker and author are the same
person — backfilled from ``requested_by``.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_made_by(apps, schema_editor):
    Approval = apps.get_model("approvals", "Approval")
    Approval.objects.filter(made_by__isnull=True).update(made_by=models.F("requested_by"))


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0002_tolerance_and_reask"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="approval",
            name="made_by",
            field=models.ForeignKey(
                null=True,
                help_text=(
                    "Who created the document. Snapshotted once and never rewritten, so "
                    "re-asking after a rejection cannot launder the maker out of the way "
                    "and let them approve their own document."
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name="approvals_made",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_made_by, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="approval",
            name="made_by",
            field=models.ForeignKey(
                help_text=(
                    "Who created the document. Snapshotted once and never rewritten, so "
                    "re-asking after a rejection cannot launder the maker out of the way "
                    "and let them approve their own document."
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name="approvals_made",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="approval",
            name="requested_by",
            field=models.ForeignKey(
                help_text=(
                    "Who asked, this time round. Usually the maker; after a rejection it "
                    "may be whoever picked the document back up."
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name="approvals_requested",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="approval",
            name="approval_maker_is_not_checker",
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.CheckConstraint(
                condition=~models.Q(decided_by=models.F("requested_by")),
                name="approval_asker_is_not_checker",
            ),
        ),
        migrations.AddConstraint(
            model_name="approval",
            constraint=models.CheckConstraint(
                condition=~models.Q(decided_by=models.F("made_by")),
                name="approval_maker_is_not_checker",
            ),
        ),
    ]
