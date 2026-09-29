# Fix ticket 03 (change PRD §14.2), step 3 of 3 for vendors: ownership required
# on vendors and their brand links; `code` unique within the tenant, and a
# nonblank GSTIN identifies at most one vendor there.

import django.db.models.deletion
from django.db import migrations, models

import core.goods_base


def _tenant() -> models.ForeignKey:
    return models.ForeignKey(
        db_default=core.goods_base.CurrentTenant(),
        editable=False,
        on_delete=django.db.models.deletion.PROTECT,
        related_name="+",
        to="masters.tenant",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("masters", "0014_backfill_inherited_master_tenant"),
        ("vendors", "0008_vendor_tenant_columns"),
    ]

    operations = [
        migrations.AlterField(model_name="vendor", name="tenant", field=_tenant()),
        migrations.AlterField(model_name="vendorbrand", name="tenant", field=_tenant()),
        migrations.AlterField(model_name="vendor", name="code", field=models.SlugField(max_length=32)),
        migrations.AddConstraint(
            model_name="vendor",
            constraint=models.UniqueConstraint(fields=("tenant", "code"), name="uq_vendor_tenant_code"),
        ),
        migrations.AddConstraint(
            model_name="vendor",
            constraint=models.UniqueConstraint(
                condition=models.Q(("gstin", ""), _negated=True),
                fields=("tenant", "gstin"),
                name="uq_vendor_tenant_gstin",
            ),
        ),
    ]
