"""Partner (accounts-receivable) ledger — payments received from a partner
store, offset against `StoreTransfer.partner_billing_value_paise` at the
report layer (`outbound.PartnerDuesView`). Append-only (ADR-0004), mirroring
`VendorLedgerEntry`.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.money
from core.ledger import append_only_reverse_sql, append_only_sql, truncate_guard_reverse_sql, truncate_guard_sql

TABLE = "finledger_partner_entry"


class Migration(migrations.Migration):

    dependencies = [
        ("finledger", "0004_alter_cashledgerentry_account"),
        ("masters", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PartnerLedgerEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("amount", core.money.MoneyField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("payment", "Payment received"),
                            ("reversal", "Reversal"),
                        ],
                        max_length=12,
                    ),
                ),
                ("doc_number", models.CharField(db_index=True, max_length=128)),
                (
                    "description",
                    models.CharField(blank=True, default="", max_length=240),
                ),
                ("reference", models.CharField(blank=True, default="", max_length=120)),
                ("mode", models.CharField(blank=True, default="", max_length=24)),
                (
                    "posted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reverses",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reversed_by",
                        to="finledger.partnerledgerentry",
                    ),
                ),
                (
                    "store",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="partner_ledger_entries",
                        to="masters.store",
                    ),
                ),
            ],
            options={
                "db_table": "finledger_partner_entry",
                "ordering": ["-created_at", "-id"],
            },
        ),
        # Append-only enforcement (ADR-0004) + TRUNCATE guard (ADR-0006), same
        # treatment every other finledger ledger table gets.
        migrations.RunSQL(
            sql=append_only_sql(TABLE),
            reverse_sql=append_only_reverse_sql(TABLE),
        ),
        migrations.RunSQL(
            sql=truncate_guard_sql(TABLE),
            reverse_sql=truncate_guard_reverse_sql(TABLE),
        ),
    ]
