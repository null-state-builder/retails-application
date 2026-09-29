# Fix ticket 03 (change PRD §14.2), step 1 of 3 for vendors: a nullable tenant
# column on the vendor and on its brand links. The link table Django created for
# `Vendor.brands` becomes the explicit `VendorBrand` model over the very same
# table, so the tenant can live on the link and both ends can be held to it.

import django.db.models.deletion
from django.db import migrations, models

from core.schema_guards import drop_tenant_wall_sql


def _tenant() -> models.ForeignKey:
    return models.ForeignKey(
        editable=False,
        null=True,
        on_delete=django.db.models.deletion.PROTECT,
        related_name="+",
        to="masters.tenant",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("masters", "0013_inherited_master_tenant_columns"),
        ("vendors", "0007_goodsbooking_bookingreceiptlink_and_more"),
    ]

    operations = [
        migrations.AddField(model_name="vendor", name="tenant", field=_tenant()),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="VendorBrand",
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
                        (
                            "brand",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE, to="masters.brand"
                            ),
                        ),
                        (
                            "vendor",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE, to="vendors.vendor"
                            ),
                        ),
                    ],
                    options={
                        "db_table": "vendors_vendor_brands",
                        "unique_together": {("vendor", "brand")},
                    },
                ),
                migrations.AlterField(
                    model_name="vendor",
                    name="brands",
                    field=models.ManyToManyField(
                        blank=True,
                        related_name="vendors",
                        through="vendors.VendorBrand",
                        to="masters.brand",
                    ),
                ),
            ],
            database_operations=[],
        ),
        migrations.AddField(model_name="vendorbrand", name="tenant", field=_tenant()),
        migrations.RunSQL(
            migrations.RunSQL.noop,
            drop_tenant_wall_sql("vendors_vendor", "vendors_vendor_brands"),
        ),
    ]
