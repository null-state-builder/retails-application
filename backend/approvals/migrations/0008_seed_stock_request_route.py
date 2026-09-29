from django.db import migrations

#: The one chain the system starts with (#172, D10 §3).
#:
#: A store asking another location for stock is first sanity-checked by its
#: *own* manager — the ask came from their counter and it is theirs to stand
#: behind — and then decided by the Operations Head, who is the one person with
#: sight of both ends of a move (#137).
#:
#: Step 2 carries ``later_step_may_short_circuit``, which is the direct-approve
#: ruling in one flag: the Operations Head does not wait for the manager's tap,
#: and the step below closes behind them, recorded as closed *by the Operations
#: Head*. The conversation with the holding store's manager is that step's human
#: work, not a system step — the design says so explicitly, and adding a third
#: step for it would put a gate in front of a phone call.
#:
#: Step 2 names no roles of its own: it reads the live ``stock_request``
#: ``ApprovalPolicy`` (``ho_ops``/``owner``, and its value band). Freezing a
#: second copy of that list here would mean an owner retuning who signs off a
#: stock request in Setup — an audited, maker-checkered change — saw it saved
#: and had it quietly do nothing. One editable source, per Rule 12.
STEPS = [
    {
        "order": 1,
        "roles": ["store_manager"],
        "label": "Store manager",
        "later_step_may_short_circuit": False,
    },
    {
        "order": 2,
        "roles_from_policy": True,
        "label": "Operations Head",
        "later_step_may_short_circuit": True,
    },
]


def seed_route(apps, schema_editor):
    ApprovalRoute = apps.get_model("approvals", "ApprovalRoute")
    ApprovalRoute.objects.get_or_create(kind="stock_request", defaults={"steps": STEPS})


def unseed_route(apps, schema_editor):
    """Take the route away again — but only while no request has walked it.

    ``Approval.route`` is PROTECT, deliberately: a route a request has walked is
    part of that request's audit trail. So once anybody has approved anything
    through this chain the row stays, and the rollback is a no-op rather than
    either an exception (which would strand the whole migration) or a cascade
    that quietly rewrites who approved what. The forward migration is
    ``get_or_create``, so a re-apply finds it exactly where it left it.
    """
    ApprovalRoute = apps.get_model("approvals", "ApprovalRoute")
    Approval = apps.get_model("approvals", "Approval")
    for route in ApprovalRoute.objects.filter(kind="stock_request", steps=STEPS):
        if not Approval.objects.filter(route=route).exists():
            route.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0007_approval_routes"),
    ]

    operations = [migrations.RunPython(seed_route, unseed_route)]
