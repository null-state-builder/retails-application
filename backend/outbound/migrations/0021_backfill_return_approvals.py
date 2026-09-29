"""Backfill the approvals inbox for returns that predate the gate (#75).

The same shape and the same reason as ``0017_backfill_transfer_approvals``: once
``outbound.posting.post_rtv`` refuses to post without an approval, every return
already sitting as a draft on the alpha is stuck for good — it cannot post, and
``maker_checker.ask_again`` is the rejected-only route, so it cannot be sent to
the inbox either. Bricked in both directions, with nobody able to see why.

Posted and cancelled returns are left alone: they are immutable, they moved
their stock under the old rules, and inventing a signature for them now would be
a worse lie than the missing one. A draft with no ``created_by`` is skipped —
there is nobody to attribute the request to, so its maker raises it by hand.
"""

from __future__ import annotations

from django.db import migrations

DRAFT = 0

#: The seeded return-to-brand approvers at the time of writing
#: (``approvals.0004_seed_approval_policies``: the brand manager inside a
#: ₹25,000 band, the owner above it). Spelled out rather than read from the live
#: row: a migration must not drift with data a business may since have retuned.
BAND_PAISE = 25_00_000
IN_BAND = ["brand_manager", "owner"]
ESCALATED = ["owner"]

#: Stamped on every row this migration raises, so the reverse can find exactly
#: those rows again — and so a brand manager opening one sees why it appeared
#: without anybody having pressed a button.
BACKFILL_NOTE = (
    "Raised automatically when the return-to-brand approval gate was switched on: "
    "this draft predates it."
)


def raise_pending_approvals(apps, schema_editor):
    Approval = apps.get_model("approvals", "Approval")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ReturnToVendor = apps.get_model("outbound", "ReturnToVendor")
    content_type = ContentType.objects.get_for_model(ReturnToVendor)

    drafts = ReturnToVendor.objects.filter(docstatus=DRAFT).select_related("store", "brand")
    for rtv in drafts:
        if Approval.objects.filter(content_type=content_type, object_id=rtv.pk).exists():
            continue
        if rtv.created_by_id is None:
            continue

        lines = list(rtv.lines.all())
        pieces = sum(abs(line.qty or 0) for line in lines)
        # Priced from what is frozen on the line and nothing else — a historical
        # migration cannot import the live costing helper without drifting with
        # it, and a total of 0 reads as "unknown", which escalates. Fail-closed
        # either way.
        value = sum(abs(line.qty or 0) * (line.unit_cost_paise or 0) for line in lines)
        brand = rtv.brand.name if rtv.brand_id else "Return"

        Approval.objects.create(
            kind="return_to_brand",
            kind_label="Return to brand",
            title=(
                f"{brand} · {rtv.store.code} · {len(lines)} "
                f"line{'' if len(lines) == 1 else 's'} · {pieces} pcs"
            ),
            content_type=content_type,
            object_id=rtv.pk,
            store=rtv.store,
            brand=brand if rtv.brand_id else "",
            value_paise=value,
            approver_roles=list(IN_BAND if 0 < value <= BAND_PAISE else ESCALATED),
            # Maker and asker are the same person: nobody re-asked, this is the
            # request the draft should have been born with.
            made_by_id=rtv.created_by_id,
            requested_by_id=rtv.created_by_id,
            reason=BACKFILL_NOTE,
        )


def drop_backfilled_approvals(apps, schema_editor):
    """Reverse: remove the approvals this migration raised, and only those.

    Matched on ``BACKFILL_NOTE`` rather than on kind + status, because by the
    time anyone reverses this people will have raised their own return requests,
    and deleting those would erase real audit rows and leave live drafts blocked
    with nothing in anybody's inbox.
    """
    Approval = apps.get_model("approvals", "Approval")
    Approval.objects.filter(kind="return_to_brand", status="pending", reason=BACKFILL_NOTE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0020_return_to_brand_pool"),
        # Pinned to the approvals migration that adds the brand column this
        # backfill writes, so the historical model already carries it.
        ("approvals", "0006_approval_brand"),
    ]

    operations = [
        migrations.RunPython(raise_pending_approvals, drop_backfilled_approvals),
    ]
