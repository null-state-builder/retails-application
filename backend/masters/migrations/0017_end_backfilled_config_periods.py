from django.db import migrations

# 0016 gave every existing configuration version an open-ended period. Where one draft
# line (same tenant, kind and scope key) had several versions, only the latest can still
# be in force: each earlier one ends where the next begins. Versions with equal starts
# are left overlapping and keep failing closed until someone reconciles them.
END_EARLIER_VERSIONS = """
UPDATE masters_effectiveversionperiod p
SET effective_to = n.next_start
FROM (
    SELECT v.id,
           LEAD(v.effective_from) OVER (
               PARTITION BY v.tenant_id, v.kind, v.scope_key
               ORDER BY v.effective_from, v.version
           ) AS next_start
    FROM masters_configversion v
) n
WHERE p.target_kind = 'configuration'
  AND p.target_id = n.id
  AND p.scope_key LIKE 'version:%%'
  AND p.effective_to IS NULL
  AND p.withdrawn_at IS NULL
  AND n.next_start IS NOT NULL
  AND n.next_start > p.effective_from
"""


class Migration(migrations.Migration):
    dependencies = [("masters", "0016_config_periods_and_policy_pins")]

    operations = [migrations.RunSQL(END_EARLIER_VERSIONS, migrations.RunSQL.noop)]
