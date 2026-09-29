"""Owner and Brand Manager may approve wherever the Operations Head may.

RBAC v1 (``docs/features/rbac/rbac-v1-initial-roles.md``, 22 Sep 2026) defines
six initial roles and ``ho_ops`` is not one of them. It is not removed - nothing
is - but the approvals it alone could clear (stock requests, count variances,
transfers, gap closures) must be reachable from the six. Anand's ruling: Owner
and Brand Manager can both approve there.

Additive only. Every ``ApprovalPolicy`` list that names ``ho_ops`` gains
``owner`` and ``brand_manager`` where they are missing; ``ho_ops`` keeps its
seat. Pending requests are re-snapshotted the same way so the new approvers
see what is already in the inbox; decided rows are history and stay as they
are. The stock-request route's second step reads the policy live, so it
follows without a change of its own.

Reversible: the reverse removes only what this migration could have added,
and only from lists that still name ``ho_ops``.
"""

from __future__ import annotations

from django.db import migrations

ANCHOR = "ho_ops"
JOINERS = ("owner", "brand_manager")


def _widen(roles: list[str]) -> list[str]:
    roles = list(roles or [])
    if ANCHOR not in roles:
        return roles
    for role in JOINERS:
        if role not in roles:
            roles.append(role)
    return roles


def _narrow(roles: list[str]) -> list[str]:
    roles = list(roles or [])
    if ANCHOR not in roles:
        return roles
    return [role for role in roles if role not in JOINERS]


def _apply(apps, fn):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    Approval = apps.get_model("approvals", "Approval")

    for policy in ApprovalPolicy.objects.all():
        band, escalated = fn(policy.band_roles), fn(policy.escalated_roles)
        if (band, escalated) != (list(policy.band_roles), list(policy.escalated_roles)):
            policy.band_roles, policy.escalated_roles = band, escalated
            policy.save(update_fields=["band_roles", "escalated_roles"])

    for approval in Approval.objects.filter(status="pending", approver_roles__contains=[ANCHOR]):
        widened = fn(approval.approver_roles)
        if widened != list(approval.approver_roles):
            approval.approver_roles = widened
            approval.save(update_fields=["approver_roles"])


def widen(apps, schema_editor):
    _apply(apps, _widen)


def narrow(apps, schema_editor):
    _apply(apps, _narrow)


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0012_store_person_in_approval_rows"),
    ]

    operations = [migrations.RunPython(widen, narrow)]
