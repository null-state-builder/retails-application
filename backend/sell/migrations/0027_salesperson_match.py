"""Ticket 07, step 1 of 3: room for the staff list on every sale line.

Adds the frozen copy of the old salesperson table (`SalespersonMatch`) and, on
each sale line, the staff record who sold it, or the old row it was sold under,
and the seller's code and name as they were. Schema only; the data moves in
0028 and the old table goes in 0029.

The old "a sold line names a salesman" check is lifted here and put back in
0029 over the new columns, once every line has them.
"""

from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sell", "0026_override_self_approved_flag"),
        ("accounts", "0020_merge_store_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SalespersonMatch",
            fields=[
                ("id", models.IntegerField(primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=16)),
                ("name", models.CharField(max_length=120)),
                ("was_active", models.BooleanField(default=True)),
                (
                    "rule",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "Not matched yet"),
                            ("code", "Same code at this store"),
                            ("name", "Same name at this store"),
                            ("admin", "Chosen by Admin"),
                        ],
                        default="",
                        max_length=8,
                    ),
                ),
                ("matched_at", models.DateTimeField(blank=True, null=True)),
                ("revision", models.IntegerField(default=1)),
                (
                    "matched_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "staff",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="accounts.staff",
                    ),
                ),
                (
                    "store",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="masters.store",
                    ),
                ),
            ],
            options={
                "db_table": "sell_salesperson_match",
                "ordering": ["store_id", "code", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("rule", ""), ("staff__isnull", True)),
                            models.Q(("staff__isnull", False), models.Q(("rule", ""), _negated=True)),
                            _connector="OR",
                        ),
                        name="ck_salespersonmatch_rule_iff_staff",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="saleline",
            name="salesperson",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.staff",
            ),
        ),
        migrations.AddField(
            model_name="saleline",
            name="salesperson_match",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="lines",
                to="sell.salespersonmatch",
            ),
        ),
        migrations.AddField(
            model_name="saleline",
            name="salesperson_code",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="saleline",
            name="salesperson_name",
            field=models.CharField(blank=True, default="", max_length=160),
        ),
        migrations.RemoveConstraint(
            model_name="saleline",
            name="ck_saleline_sale_has_salesman",
        ),
    ]
