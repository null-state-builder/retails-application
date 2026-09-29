"""The plain return (#184, grill Q7): a piece back, a credit note out, no cash.

Three things land together because they are one change. `Return`/`ReturnLine` are
the document and its lines; `SellPolicy.return_window_days` is the window the
grill put in data rather than in code; and the two new `ContinuityFlag` kinds are
what a return that was late, or that gave back a piece the books never priced,
leaves on the store's morning queue.

The FSM trigger is applied to `sell_return` for the same reason `sell.0002`
applied it to the sale and the credit note: without it the table is an ordinary
one with a good docstring, and the immutability a money document rests on is a
Python convention the superuser CI connects as would walk straight through.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.money
from core.documents import document_fsm_reverse_sql, document_fsm_sql


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_external_number_acceptance"),
        ("masters", "0007_dataset_watermarks"),
        ("sell", "0010_registerhandover"),
        ("vendors", "0004_booking_close"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Return",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "doc_number",
                    models.CharField(blank=True, max_length=128, null=True, unique=True),
                ),
                (
                    "idempotency_uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                (
                    "docstatus",
                    models.IntegerField(
                        choices=[(0, "draft"), (1, "submitted"), (2, "cancelled")], default=0
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("fy", models.CharField(max_length=7)),
                (
                    "returned_at",
                    models.DateTimeField(help_text="When the counter took the piece back."),
                ),
                ("customer_name", models.CharField(blank=True, default="", max_length=120)),
                ("customer_mobile", models.CharField(blank=True, default="", max_length=15)),
                (
                    "window_override",
                    models.BooleanField(
                        default=False,
                        help_text="The manager took this back past the return window in SellPolicy. A second, explicit answer - not something the ordinary override covers.",
                    ),
                ),
            ],
            options={
                "db_table": "sell_return",
                "ordering": ["-returned_at", "-id"],
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="ReturnLine",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("line_no", models.IntegerField()),
                ("barcode", models.CharField(db_index=True, max_length=64)),
                ("season", models.CharField(blank=True, default="", max_length=120)),
                ("design", models.CharField(blank=True, default="", max_length=120)),
                ("color", models.CharField(blank=True, default="", max_length=60)),
                ("size", models.CharField(blank=True, default="", max_length=24)),
                ("brand", models.CharField(blank=True, default="", max_length=120)),
                ("item", models.CharField(blank=True, default="", max_length=120)),
                ("hsn", models.CharField(blank=True, default="", max_length=24)),
                ("qty", models.IntegerField()),
                (
                    "refund_paise",
                    core.money.MoneyField(
                        default=0,
                        help_text="What the customer actually paid for this quantity (D2).",
                    ),
                ),
                ("gst_rate", models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                (
                    "gst_paise",
                    core.money.MoneyField(
                        default=0,
                        help_text="The tax inside the refund, at the rate the bill charged.",
                    ),
                ),
                (
                    "unit_cost_paise",
                    core.money.MoneyField(
                        default=0,
                        help_text="The cost frozen on the original line. Zero when it was never priced.",
                    ),
                ),
                (
                    "cost_book",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("own", "KDPS-owned - out of our own inventory"),
                            ("brand", "Brand-owned - against what we owe the brand"),
                        ],
                        default="",
                        max_length=8,
                    ),
                ),
                ("reason", models.CharField(blank=True, default="", max_length=40)),
                (
                    "condition",
                    models.CharField(
                        choices=[
                            ("good", "Good - back on the shelf"),
                            ("damaged", "Damaged - into quarantine"),
                        ],
                        default="good",
                        max_length=8,
                    ),
                ),
            ],
            options={
                "db_table": "sell_return_line",
                "ordering": ["return_doc_id", "line_no"],
            },
        ),
        migrations.AddField(
            model_name="sellpolicy",
            name="return_window_days",
            field=models.IntegerField(
                default=30,
                help_text="How many days after a bill a piece comes back without a manager having to override the window.",
            ),
        ),
        migrations.AlterField(
            model_name="continuityflag",
            name="kind",
            field=models.CharField(
                choices=[
                    ("number_hole", "Bills missing before this one"),
                    ("cn_unverified", "Credit note taken without verification"),
                    ("return_orig_missing", "Returned against a bill we do not hold"),
                    ("offer_mismatch", "Offer applied differs from the rulebook"),
                    ("gst_mismatch", "Tax charged differs from the dated slab"),
                    ("aged_uncosted", "Sold before inward, still unpriced"),
                    ("gstin_invalid", "The buyer's GSTIN is not well formed"),
                    ("return_late", "Taken back after the return window closed"),
                    ("return_uncosted", "Given back before the books could price it"),
                ],
                max_length=24,
            ),
        ),
        migrations.AddConstraint(
            model_name="sellpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(("return_window_days__gte", 0)),
                name="ck_sellpolicy_window_is_not_negative",
            ),
        ),
        migrations.AddField(
            model_name="return",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="returns_taken",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="return",
            name="original_sale",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="plain_returns",
                to="sell.sale",
            ),
        ),
        migrations.AddField(
            model_name="return",
            name="override_by",
            field=models.ForeignKey(
                blank=True,
                help_text="The manager who stood behind this return. PROTECT because the name is the evidence.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="returns_authorised",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="return",
            name="series",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="core.voucherseries",
            ),
        ),
        migrations.AddField(
            model_name="return",
            name="store",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="returns",
                to="masters.store",
            ),
        ),
        migrations.AddField(
            model_name="creditnote",
            name="source_return",
            field=models.ForeignKey(
                blank=True,
                help_text="The plain return that issued it. Exactly one of the two sources is ever set - a note is handed over either at the end of a bill or at the end of a return, and never both.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="credit_notes_issued",
                to="sell.return",
            ),
        ),
        migrations.AddField(
            model_name="returnline",
            name="cost_vendor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="return_lines_reversed",
                to="vendors.vendor",
            ),
        ),
        migrations.AddField(
            model_name="returnline",
            name="original_line",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="plain_returned_by",
                to="sell.saleline",
            ),
        ),
        migrations.AddField(
            model_name="returnline",
            name="return_doc",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="sell.return"
            ),
        ),
        migrations.AddIndex(
            model_name="return",
            index=models.Index(fields=["store", "returned_at"], name="return_store_taken_idx"),
        ),
        migrations.AddConstraint(
            model_name="return",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("docstatus", 0), ("doc_number__isnull", False), _connector="OR"
                ),
                name="sell_return_posted_has_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="return",
            constraint=models.CheckConstraint(
                condition=models.Q(("docstatus__in", [0, 1, 2])),
                name="sell_return_docstatus_domain",
            ),
        ),
        migrations.AddConstraint(
            model_name="returnline",
            constraint=models.UniqueConstraint(
                fields=("return_doc", "line_no"), name="uq_returnline_doc_line_no"
            ),
        ),
        migrations.AddConstraint(
            model_name="returnline",
            constraint=models.CheckConstraint(
                condition=models.Q(("qty__gt", 0)), name="ck_returnline_qty_positive"
            ),
        ),
        migrations.RunSQL(
            sql=document_fsm_sql("sell_return"),
            reverse_sql=document_fsm_reverse_sql("sell_return"),
        ),
    ]
