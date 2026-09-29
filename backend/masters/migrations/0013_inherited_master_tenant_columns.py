# Fix ticket 03 (change PRD §14.2), step 1 of 3: give the inherited masters a
# tenant column. Nullable for now so existing rows keep their identity while
# `0014_backfill_inherited_master_tenant` assigns their verified tenant.
#
# No database default yet, on purpose: PostgreSQL evaluates a column default for
# every existing row when the column is added, so `kdps_current_tenant()` here
# would stamp old rows with whatever tenant the migrating connection happened to
# have bound. The default arrives with the NOT NULL in step 3.

import django.db.models.deletion
from django.db import migrations, models

from core.schema_guards import drop_tenant_wall_sql

TABLES = ("masters_legalentity", "masters_gstin", "masters_store", "masters_brand")


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
        ("core", "0007_goods_roles_extensions"),
        ("masters", "0012_style_productsku_identitypick_skualias_and_more"),
    ]

    operations = [
        migrations.AddField(model_name="legalentity", name="tenant", field=_tenant()),
        migrations.AddField(model_name="gstin", name="tenant", field=_tenant()),
        migrations.AddField(model_name="store", name="tenant", field=_tenant()),
        migrations.AddField(model_name="brand", name="tenant", field=_tenant()),
        # Forward: nothing. Reverse: the wall `migrate` installed must come down
        # before the column it is built on can be removed.
        migrations.RunSQL(migrations.RunSQL.noop, drop_tenant_wall_sql(*TABLES)),
    ]
