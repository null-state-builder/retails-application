"""Move in-flight stock-request approvals onto the asking store, and route them (#172).

D10 puts a two-step route in front of a stock request — the asking store's own
manager, then the Operations Head — so the inbox row now hangs off the
*requesting* store rather than the fulfilling one. Requests raised before this
would otherwise sit in the old place under the old rules: single-step, and
invisible to the very manager the route makes step 1. Two behaviours live at
once is exactly the state the transfer and return backfills (``0017``, ``0021``)
exist to avoid, so the same treatment here.

Only **pending** rows are touched. A decided approval is history — its store, its
approver list and its single-step shape are what was true when someone signed it,
and rewriting that would be forging the record rather than migrating it.

The route is attached at step 0 with the roles its first step reaches, so an
in-flight request restarts its chain at the top: nobody has cleared a step yet,
and the Operations Head can still close both at once exactly as on a new one.

It lives in ``outbound`` and not in ``approvals`` because the fact being applied
is a stock request's — which of its two stores answers for it. ``approvals``
imports no business model (ADR-0002), and its migrations should not either.
"""

from __future__ import annotations

from django.db import migrations

PENDING = "pending"
KIND = "stock_request"


def _first_step_roles(route) -> list[str]:
    """Who may act with the chain at step 0 — its own roles plus any later step
    allowed to close it. The route model's ``active_roles`` in miniature; spelled
    out here because a migration must not call live model methods that may be
    rewritten after it has run.
    """
    steps = sorted(route.steps or [], key=lambda step: step.get("order", 0))
    roles: list[str] = []
    for index, step in enumerate(steps):
        if index > 0 and not step.get("later_step_may_short_circuit"):
            continue
        source = step.get("roles") or []
        if step.get("roles_from_policy"):
            source = _policy_roles(route.kind)
        for role in source:
            if role not in roles:
                roles.append(role)
    return roles


def _policy_roles(kind: str) -> list[str]:
    from django.apps import apps as global_apps

    ApprovalPolicy = global_apps.get_model("approvals", "ApprovalPolicy")
    policy = ApprovalPolicy.objects.filter(kind=kind).first()
    if policy is None:
        return []
    # Value 0 reads as "unknown" and escalates, which is the fail-closed answer
    # and the right one here: this runs once, over rows of every size.
    return list(policy.escalated_roles or [])


def move_and_route(apps, schema_editor):
    Approval = apps.get_model("approvals", "Approval")
    ApprovalRoute = apps.get_model("approvals", "ApprovalRoute")
    StockRequest = apps.get_model("outbound", "StockRequest")

    route = ApprovalRoute.objects.filter(kind=KIND).first()
    roles = _first_step_roles(route) if route else []

    asker = dict(StockRequest.objects.values_list("id", "requesting_store_id"))
    for approval in Approval.objects.filter(kind=KIND, status=PENDING):
        requesting_store_id = asker.get(approval.object_id)
        if requesting_store_id is None:
            # The document is gone; leave the orphan exactly as it is rather
            # than guessing a store for it.
            continue
        approval.store_id = requesting_store_id
        approval.current_step = 0
        if route is not None:
            approval.route_id = route.id
            approval.approver_roles = roles
        approval.save(update_fields=["store_id", "route_id", "current_step", "approver_roles"])


def unmove(apps, schema_editor):
    """Put the pending rows back on the fulfilling store, unrouted."""
    Approval = apps.get_model("approvals", "Approval")
    StockRequest = apps.get_model("outbound", "StockRequest")

    holder = dict(StockRequest.objects.values_list("id", "fulfilling_store_id"))
    for approval in Approval.objects.filter(kind=KIND, status=PENDING, route__isnull=False):
        fulfilling_store_id = holder.get(approval.object_id)
        if fulfilling_store_id is None:
            continue
        approval.store_id = fulfilling_store_id
        approval.route_id = None
        approval.current_step = 0
        approval.approver_roles = _policy_roles(KIND)
        approval.save(update_fields=["store_id", "route_id", "current_step", "approver_roles"])


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0022_stockadjustmentline_reason_recount"),
        ("approvals", "0008_seed_stock_request_route"),
    ]

    operations = [migrations.RunPython(move_and_route, unmove)]
