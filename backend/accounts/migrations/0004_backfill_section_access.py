"""Backfill Role.section_access on already-seeded databases (issue #85).

Migration 0003 added the field with `default=dict`, so on a DB that predates
this slice every existing role carries `{}` — which fail-closes real users out
of the newly section-gated admin APIs until a re-seed runs. Backfill each empty
role from the SIDEBAR RBAC default so the deploy is safe regardless of whether
`seed_foundation` re-runs. Roles a live admin has already tuned (non-empty) are
left untouched.
"""

from django.db import migrations


def backfill_section_access(apps, schema_editor):
    from accounts.rbac_matrix import section_access_for

    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.all():
        if not role.section_access:
            role.section_access = section_access_for(role.code)
            role.save(update_fields=["section_access"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_role_section_access"),
    ]

    operations = [
        migrations.RunPython(backfill_section_access, migrations.RunPython.noop),
    ]
