"""Backfill the approvals inbox for drafts that predate maker-checker (#70).

Without this, every write-off / V-flip / adjustment already sitting as a draft
on the alpha would hit "This document has no approval request; it cannot post."
forever — the gate in ``outbound.posting`` applies to old drafts too.

For each such draft we do two things:

* raise the pending approval it should have been born with, attributed to the
  person who made it; and
* clear the approver column, because on old drafts it holds the *creator* —
  exactly the self-stamping this slice removes. It is a lie, not a record.

Posted and cancelled documents are left alone: they are immutable, they already
posted under the old rules, and inventing a second signature for them after the
fact would be a worse lie than the one we are clearing.

A draft with no ``created_by`` is skipped — there is nobody to attribute the
request to. It stays unpostable, and its owner raises it again.

Every row raised here carries ``BACKFILL_NOTE`` as its reason, which is what
makes this migration reversible without collateral damage: the reverse deletes
only the rows *it* wrote, never a request a person has raised since.
"""

from __future__ import annotations

from django.db import migrations

DRAFT = 0

# (model, kind code, label, the document's own approver column)
WIRED = [
    ("WriteOff", "writeoff", "Write-off", "approved_by"),
    ("VFlip", "vflip", "V-flip", "authorized_by"),
    ("StockAdjustment", "adjustment", "Stock adjustment", "approved_by"),
]

# Mirrors outbound.permissions.OUTBOUND_ADMIN_ROLES at the time of writing.
# Spelled out rather than imported: a migration must not drift with live code.
APPROVER_ROLES = ["accounts", "ho_ops", "it_admin", "owner"]

#: Stamped on every row this migration raises, so the reverse can find exactly
#: those rows again — and so a checker opening one of these sees why it appeared
#: without a person having asked.
BACKFILL_NOTE = "Raised automatically when maker-checker was switched on: this draft predates it."


def _line_totals(doc):
    lines = list(doc.lines.all())
    pieces = 0
    value = 0
    for line in lines:
        qty = abs(line.adj_qty) if hasattr(line, "adj_qty") else line.qty
        pieces += qty
        value += qty * (line.unit_cost_paise or 0)
    return len(lines), pieces, value


def raise_pending_approvals(apps, schema_editor):
    Approval = apps.get_model("approvals", "Approval")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for model_name, kind, label, approver_col in WIRED:
        model = apps.get_model("outbound", model_name)
        content_type = ContentType.objects.get_for_model(model)
        for doc in model.objects.filter(docstatus=DRAFT).select_related("store"):
            if Approval.objects.filter(content_type=content_type, object_id=doc.pk).exists():
                continue
            if doc.created_by_id is None:
                continue
            line_count, pieces, value = _line_totals(doc)
            Approval.objects.create(
                kind=kind,
                kind_label=label,
                title=(
                    f"{doc.store.code} · {line_count} "
                    f"line{'' if line_count == 1 else 's'} · {pieces} pcs"
                ),
                content_type=content_type,
                object_id=doc.pk,
                store=doc.store,
                value_paise=value,
                approver_roles=list(APPROVER_ROLES),
                # Maker and asker are the same person here: nobody re-asked, the
                # request is the one the draft should have been born with.
                made_by_id=doc.created_by_id,
                requested_by_id=doc.created_by_id,
                reason=BACKFILL_NOTE,
            )
            # Drop the old self-stamp: nobody has approved this yet.
            setattr(doc, f"{approver_col}_id", None)
            doc.save(update_fields=[approver_col])


def drop_backfilled_approvals(apps, schema_editor):
    """Reverse: remove the approvals this migration raised, and only those.

    Matched on ``BACKFILL_NOTE``, not on kind + status: by the time anyone
    reverses this, people will have raised their own requests against these same
    document kinds, and deleting those would erase real audit records and leave
    live drafts blocked with nothing in anybody's inbox.

    The cleared approver columns are not restored — the values were the
    creator's own name, which is the defect, not data worth putting back.
    """
    Approval = apps.get_model("approvals", "Approval")
    Approval.objects.filter(
        kind__in=[kind for _, kind, _, _ in WIRED],
        status="pending",
        reason=BACKFILL_NOTE,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("outbound", "0006_alter_stockadjustment_approved_by_and_more"),
        # Pinned to the *latest* approvals migration, not to 0001: this writes
        # rows, and 0003 makes ``made_by`` non-null. Django plans the approvals
        # leaf before the outbound one, so without this the insert below would
        # meet a column the historical model does not know about and every
        # deployment carrying old drafts would fail.
        ("approvals", "0003_maker_is_barred_from_checking"),
    ]

    operations = [
        migrations.RunPython(raise_pending_approvals, drop_backfilled_approvals),
    ]
