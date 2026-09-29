"""Backfill the approvals inbox for transfers that predate the gate (#137).

Same shape, and the same reason, as ``0007_backfill_approvals``: the wall in
``outbound.posting.post_transfer_dispatch`` applies to old drafts too, so every
transfer already sitting as a draft on the alpha would otherwise hit "This
document has no approval request; it cannot post." for good — and could not be
sent to the inbox either, because ``maker_checker.ask_again`` is the
rejected-only route and refuses a document that was never asked about. Bricked
in both directions, with nobody able to see why.

Transfers differ from that earlier backfill in two ways, both from #137:

* the approval hangs off the **source** store, because the sender is answerable
  for the pieces until the receiver scans them in; and
* there is no old approver column to clear — ``approved_by`` is new in 0016 and
  is NULL on every one of these rows, so nothing here has to undo a self-stamp.

Posted and cancelled transfers are left alone: they are immutable, they moved
their stock under the old rules, and inventing a signature for them now would be
a worse lie than the missing one. A draft with no ``created_by`` is skipped —
there is nobody to attribute the request to, so its maker raises it by hand.
"""

from __future__ import annotations

from django.db import migrations

DRAFT = 0

#: The seeded transfer policy's approvers at the time of writing
#: (``approvals.0004_seed_approval_policies``, band 0 so every transfer is
#: escalated). Spelled out rather than read from the live row: a migration must
#: not drift with data a business may since have retuned.
APPROVER_ROLES = ["ho_ops", "owner"]

#: Stamped on every row this migration raises, so the reverse can find exactly
#: those rows again — and so an Operations Head opening one sees why it appeared
#: without anybody having pressed a button.
BACKFILL_NOTE = (
    "Raised automatically when the transfer approval gate was switched on: "
    "this draft predates it."
)


def raise_pending_approvals(apps, schema_editor):
    Approval = apps.get_model("approvals", "Approval")
    ContentType = apps.get_model("contenttypes", "ContentType")
    StoreTransfer = apps.get_model("outbound", "StoreTransfer")
    content_type = ContentType.objects.get_for_model(StoreTransfer)

    drafts = StoreTransfer.objects.filter(docstatus=DRAFT).select_related(
        "source_store", "destination_store"
    )
    for transfer in drafts:
        if Approval.objects.filter(content_type=content_type, object_id=transfer.pk).exists():
            continue
        if transfer.created_by_id is None:
            continue

        lines = list(transfer.lines.all())
        pieces = sum(abs(line.qty_planned or 0) for line in lines)
        # Priced from what is frozen on the line and nothing else. The books are
        # deliberately not read here: a historical migration cannot import the
        # live costing helper without drifting with it, and an unpriced plan
        # totalling 0 reads as "unknown", which escalates — the fail-closed
        # answer either way.
        value = sum(abs(line.qty_planned or 0) * (line.unit_cost_paise or 0) for line in lines)
        destination = transfer.destination_store.code
        subject = f"To {destination} · cross-state" if transfer.is_cross_state else f"To {destination}"

        Approval.objects.create(
            kind="transfer",
            kind_label="Transfer",
            title=(
                f"{subject} · {transfer.source_store.code} · {len(lines)} "
                f"line{'' if len(lines) == 1 else 's'} · {pieces} pcs"
            ),
            content_type=content_type,
            object_id=transfer.pk,
            store=transfer.source_store,
            value_paise=value,
            approver_roles=list(APPROVER_ROLES),
            # Maker and asker are the same person: nobody re-asked, this is the
            # request the draft should have been born with.
            made_by_id=transfer.created_by_id,
            requested_by_id=transfer.created_by_id,
            reason=BACKFILL_NOTE,
        )


def drop_backfilled_approvals(apps, schema_editor):
    """Reverse: remove the approvals this migration raised, and only those.

    Matched on ``BACKFILL_NOTE`` rather than on kind + status, because by the
    time anyone reverses this people will have raised their own transfer
    requests, and deleting those would erase real audit rows and leave live
    drafts blocked with nothing in anybody's inbox.
    """
    Approval = apps.get_model("approvals", "Approval")
    Approval.objects.filter(kind="transfer", status="pending", reason=BACKFILL_NOTE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0016_storetransfer_approved_by"),
        # Pinned to the latest approvals migration, not to 0001: this writes
        # Approval rows, so the historical model must already carry every column
        # and constraint the insert relies on (0003 makes ``made_by`` non-null,
        # 0004 seeds the transfer policy this backfill mirrors).
        ("approvals", "0004_seed_approval_policies"),
    ]

    operations = [
        migrations.RunPython(raise_pending_approvals, drop_backfilled_approvals),
    ]
