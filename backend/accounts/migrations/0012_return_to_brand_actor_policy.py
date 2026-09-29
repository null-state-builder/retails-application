"""Who may create and execute a return to brand (#75).

The RBAC matrix puts a store person and the warehouse on the same rung of the
Return to Brand section — both hold ``operate`` — but the two cells say
different things: "Mark damage only" against "Create & execute". Anand's ruling
of 26 July says the same in words: a store may report damage, the Warehouse
creates and executes the return.

A capability cannot express that, so it is stored actor policy, exactly like
executing a V-flip. IT Admin is on the list because the matrix gives Admin
"All" on this section; the Owner because the Owner sits above everything. Data,
so the business retunes it in Setup without a release (Rule 12).
"""

from __future__ import annotations

from django.db import migrations

ACTION = "outbound.create_return_to_brand"
LABEL = "Create or execute a return to brand"
DESCRIPTION = "Build a return from the returnable pool and post it — the warehouse's job."
ROLE_CODES = ["warehouse", "it_admin", "owner"]


def seed(apps, schema_editor):
    ActorPolicy = apps.get_model("accounts", "ActorPolicy")
    ActorPolicy.objects.get_or_create(
        action=ACTION,
        defaults={"label": LABEL, "roles": ROLE_CODES, "description": DESCRIPTION},
    )


def unseed(apps, schema_editor):
    ActorPolicy = apps.get_model("accounts", "ActorPolicy")
    ActorPolicy.objects.filter(action=ACTION, roles=ROLE_CODES).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0011_rename_staff_section_to_hrms")]

    operations = [migrations.RunPython(seed, unseed)]
