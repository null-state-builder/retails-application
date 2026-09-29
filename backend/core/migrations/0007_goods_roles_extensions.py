# Goods-v1 foundation (design §4.2, §4.4, §5.1).
#
#  * `btree_gist`, so exclusion constraints can combine equality on tenant/scope
#    columns with range overlap;
#  * the three database roles and the application role's base grants, plus the
#    `kdps_current_tenant()` function every row-level security policy reads;
#  * retire the JWT token blacklist tables. The application now authenticates
#    with server-side sessions only; the rows held refresh tokens, not business
#    evidence, and their app is no longer installed.

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations

from core.dbroles import create_roles_sql, drop_roles_reverse_sql

DROP_JWT_BLACKLIST = """
DROP TABLE IF EXISTS token_blacklist_blacklistedtoken CASCADE;
DROP TABLE IF EXISTS token_blacklist_outstandingtoken CASCADE;
DELETE FROM django_migrations WHERE app = 'token_blacklist';
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_external_number_acceptance"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.RunSQL(create_roles_sql(), drop_roles_reverse_sql()),
        migrations.RunSQL(DROP_JWT_BLACKLIST, migrations.RunSQL.noop),
    ]
