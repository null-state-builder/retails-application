# Fix ticket 03 (change PRD §14.2), step 1 of 3 for roles: a nullable tenant
# column with no default yet (see masters/0013 for why). See
# `masters/0014_backfill_inherited_master_tenant` for how existing rows get theirs.

import django.db.models.deletion
from django.db import migrations, models

from core.schema_guards import drop_tenant_wall_sql


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0016_user_tenant_authenticationfailure_tenant_and_more"),
        ("masters", "0013_inherited_master_tenant_columns"),
    ]

    operations = [
        migrations.AddField(
            model_name="role",
            name="tenant",
            field=models.ForeignKey(
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="masters.tenant",
            ),
        ),
        migrations.RunSQL(migrations.RunSQL.noop, drop_tenant_wall_sql("accounts_role")),
    ]
