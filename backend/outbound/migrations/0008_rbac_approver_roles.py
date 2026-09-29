"""Take Accounts off the outbound approvals it may only view (#94).

The code defaults moved to the roles the RBAC matrix actually puts at ``approve``
on each document's own section. A running install does not follow, because who
may approve is *stored*, in two places:

* ``ApprovalPolicy`` — the live thresholds for a document family. Materialised
  lazily on first use, so a row exists only for a kind already exercised. Rows
  that exist are rewritten; rows that do not are left alone, to be created
  correctly by the new defaults.
* ``Approval.approver_roles`` — frozen onto each request when it was raised, and
  what ``can_decide`` reads. Undecided requests are rewritten, so Accounts loses
  the seat on items already sitting in the inbox. Decided rows are history and
  are never touched.

The role lists are written out here rather than imported from
``outbound.maker_checker``: this migration is the record of one correction made
on one day, and must keep saying so after the matrix is next retuned.
"""

from __future__ import annotations

from django.db import migrations

# The matrix at the time of this correction: `stock_count: approve` and
# `stock: approve` respectively, plus the store in-charge inside the
# adjustment band (a declared exception — seniority the ladder cannot express).
COUNT_APPROVERS = ["ho_ops", "it_admin", "owner"]
STOCK_APPROVERS = ["it_admin", "owner", "warehouse"]
ADJUSTMENT_BAND = ["ho_ops", "it_admin", "owner", "store_manager"]

# kind → (who approves within the band, who approves above it)
CORRECTED = {
    "writeoff": (COUNT_APPROVERS, COUNT_APPROVERS),
    "vflip": (STOCK_APPROVERS, STOCK_APPROVERS),
    "adjustment": (ADJUSTMENT_BAND, COUNT_APPROVERS),
}

# The adjustment band as the code defaults it (₹25,000). Needed because a policy
# row is materialised lazily: on an install where nobody has raised an adjustment
# yet, there is no row to read the band from, and the live answer for a pending
# request is the default the next call would create — not zero, which would
# escalate every one of them and quietly strip the store in-charge.
DEFAULT_ADJUSTMENT_BAND_PAISE = 25_00_000


def correct_stored_approvers(apps, schema_editor):
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    Approval = apps.get_model("approvals", "Approval")

    for kind, (band_roles, escalated_roles) in CORRECTED.items():
        ApprovalPolicy.objects.filter(kind=kind).update(
            band_roles=band_roles, escalated_roles=escalated_roles
        )
        # A pending request already knows what it is worth, so it can be routed
        # to the same list the policy would give it now: inside the band the
        # in-charge may still clear it, above the band only HO.
        pending = Approval.objects.filter(kind=kind, status="pending")
        if band_roles == escalated_roles:
            pending.update(approver_roles=escalated_roles)
            continue
        policy = ApprovalPolicy.objects.filter(kind=kind).first()
        band = (policy.band_paise or 0) if policy else DEFAULT_ADJUSTMENT_BAND_PAISE
        pending.filter(value_paise__gt=0, value_paise__lte=band).update(approver_roles=band_roles)
        pending.exclude(value_paise__gt=0, value_paise__lte=band).update(
            approver_roles=escalated_roles
        )


def noop_reverse(apps, schema_editor):
    """Deliberately not reversible in data.

    Putting Accounts back would re-open the hole this closes, and the old lists
    are recoverable from the audit trail if they are ever genuinely wanted.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0007_backfill_approvals"),
        ("approvals", "0003_maker_is_barred_from_checking"),
    ]

    operations = [
        migrations.RunPython(correct_stored_approvers, noop_reverse),
    ]
