# Fix ticket 03 (change PRD §14.2), step 3 of 3 for roles: ownership required,
# `code` unique within the tenant (the name is not identity and may repeat).

import django.db.models.deletion
from django.db import migrations, models

import core.goods_base


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0017_role_tenant_column"),
        ("masters", "0014_backfill_inherited_master_tenant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="role",
            name="tenant",
            field=models.ForeignKey(
                db_default=core.goods_base.CurrentTenant(),
                editable=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="masters.tenant",
            ),
        ),
        migrations.AlterField(model_name="role", name="code", field=models.SlugField(max_length=40)),
        migrations.AddConstraint(
            model_name="role",
            constraint=models.UniqueConstraint(fields=("tenant", "code"), name="uq_role_tenant_code"),
        ),
    ]
