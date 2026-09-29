# Fix ticket 03 (change PRD §14.2), step 3 of 3: ownership is required and the
# business keys become tenant-scoped. The composite same-tenant keys and forced
# row-level security follow from `core.schema_guards` when `migrate` finishes.

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
    ]

    operations = [
        migrations.AlterField(model_name="legalentity", name="tenant", field=_tenant()),
        migrations.AlterField(model_name="gstin", name="tenant", field=_tenant()),
        migrations.AlterField(model_name="store", name="tenant", field=_tenant()),
        migrations.AlterField(model_name="brand", name="tenant", field=_tenant()),
        migrations.AlterField(
            model_name="legalentity", name="code", field=models.SlugField(max_length=24)
        ),
        migrations.AlterField(model_name="gstin", name="gstin", field=models.CharField(max_length=15)),
        migrations.AlterField(model_name="store", name="code", field=models.SlugField(max_length=16)),
        migrations.AlterField(model_name="brand", name="code", field=models.SlugField(max_length=32)),
        migrations.AddConstraint(
            model_name="legalentity",
            constraint=models.UniqueConstraint(
                fields=("tenant", "code"), name="uq_legalentity_tenant_code"
            ),
        ),
        migrations.AddConstraint(
            model_name="legalentity",
            constraint=models.UniqueConstraint(
                condition=models.Q(("pan", ""), _negated=True),
                fields=("tenant", "pan"),
                name="uq_legalentity_tenant_pan",
            ),
        ),
        migrations.AddConstraint(
            model_name="gstin",
            constraint=models.UniqueConstraint(
                fields=("tenant", "gstin"), name="uq_gstin_tenant_gstin"
            ),
        ),
        migrations.AddConstraint(
            model_name="store",
            constraint=models.UniqueConstraint(fields=("tenant", "code"), name="uq_store_tenant_code"),
        ),
        migrations.AddConstraint(
            model_name="brand",
            constraint=models.UniqueConstraint(fields=("tenant", "code"), name="uq_brand_tenant_code"),
        ),
    ]
