"""The merged store role takes the old ``store_manager`` seat in every approval row.

``accounts`` 0020 merged ``store_manager`` and ``store_staff`` into
``store_person`` (22 Sep 2026). Who may approve is *stored*, in three places,
and each still names the old code:

  · ``ApprovalPolicy.band_roles`` / ``escalated_roles`` - the live thresholds
    (the adjustment and write-off bands seeded by 0004);
  · ``ApprovalRoute.steps`` - the stock-request chain's first step (0008);
  · ``Approval.approver_roles`` - frozen onto each pending request. Decided
    rows are history and are never touched.

Anand's ruling: the store manager's band belongs to the one store role now, so
the rename is a straight substitution. Not reversible - the reverse would have
to invent a distinction the data no longer holds.
"""

from __future__ import annotations

from django.db import migrations

OLD = "store_manager"
NEW = "store_person"


def _swap(roles: list[str]) -> list[str]:
    out: list[str] = []
    for role in roles or []:
        role = NEW if role == OLD else role
        if role not in out:
            out.append(role)
    return out


def rename_in_rows(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalRoute = apps.get_model("approvals", "ApprovalRoute")
    Approval = apps.get_model("approvals", "Approval")

    for policy in ApprovalPolicy.objects.all():
        band, escalated = _swap(policy.band_roles), _swap(policy.escalated_roles)
        if (band, escalated) != (list(policy.band_roles), list(policy.escalated_roles)):
            policy.band_roles, policy.escalated_roles = band, escalated
            policy.save(update_fields=["band_roles", "escalated_roles"])

    for route in ApprovalRoute.objects.all():
        changed = False
        steps = []
        for step in route.steps or []:
            step = dict(step)
            if "roles" in step:
                swapped = _swap(step["roles"])
                changed = changed or swapped != list(step["roles"])
                step["roles"] = swapped
            steps.append(step)
        if changed:
            route.steps = steps
            route.save(update_fields=["steps"])

    for approval in Approval.objects.filter(status="pending", approver_roles__contains=[OLD]):
        approval.approver_roles = _swap(approval.approver_roles)
        approval.save(update_fields=["approver_roles"])


def noop_reverse(apps, schema_editor):
    """Deliberately not reversible - see the module docstring."""


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0011_config_periods_and_policy_pins"),
        ("accounts", "0020_merge_store_roles"),
    ]

    operations = [migrations.RunPython(rename_in_rows, noop_reverse)]
