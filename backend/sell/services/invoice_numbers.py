"""The counter's tax invoice numbers in the new series (store operations ticket 04).

Three jobs, one per moment in a number's life:

* **Before the bill** - ``till_numbering`` hands the store's counter a block of
  numbers in the new series, ``XXX/26-27/n``, for this month and the next, so it
  can bill offline. It runs on every online sync. A counter holding fewer than
  one block's worth for a month is given another block; one holding enough is
  given nothing new. A block is sent once: the counter reports which blocks it
  holds and how far into each it is, and a block it no longer reports (a device
  that lost its database, or an answer that never arrived) is never re-sent,
  because its numbers may already be on printed bills. Its unused numbers are
  cancelled at month end instead: a gap, never a reuse.
* **At the bill** - ``resolve_invoice_number`` checks the number a bill arrived
  with before the bill is written (a posted bill cannot be changed), and
  ``record_invoice_number`` writes it into the company's register of used numbers
  once the bill has its own number. A problem is flagged, never refused: the bill
  is already in the customer's hand (acceptance item 6).
* **At month end** - ``close_month`` records every unused number of every past
  month's block as cancelled, ready for GSTR-1 Table 13 (baseline, CA to confirm).

The display number (``DEO-T1-74``, R-POS-005) and ``doc_number`` are untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from core.fiscal import financial_year
from core.tenancy import tenant_context
from masters.document_series import (
    FEATURE_KEY,
    DocumentSeries,
    NumberRefused,
    allocate,
    cancel_unused,
    numbering_setting,
    parse_invoice_number,
    prefix_for_site,
    raise_number_alert,
    record_number,
    render,
)
from masters.models import Store
from masters.store_features import is_feature_on
from sell.models import ContinuityFlag, RegisteredTill, Sale, TillNumberBlock

INVOICE = DocumentSeries.TAX_INVOICE.value

log = logging.getLogger(__name__)

#: What a cancelled number of an offline block says about itself.
UNUSED_BLOCK_REASON = "Unused number from an offline till's block, cancelled at month end."


def month_of(day: date) -> date:
    return day.replace(day=1)


def next_month(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def _block_json(block: TillNumberBlock) -> dict[str, Any]:
    return {
        "id": block.pk,
        "prefix": block.prefix_code,
        "fy": block.fy,
        "month": block.month.isoformat()[:7],
        "first": block.first_n,
        "last": block.last_n,
    }


@dataclass
class TillNumbering:
    """What the counter needs to number bills in the new series offline."""

    on: bool
    new_format_from: date
    prefix: str
    block_size: int
    blocks: list[dict[str, Any]] = field(default_factory=list)
    #: Why the counter was given no block when it needs one, in words.
    problem: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "on": self.on,
            "new_format_from": self.new_format_from.isoformat(),
            "prefix": self.prefix,
            "block_size": self.block_size,
            "blocks": self.blocks,
            "problem": self.problem,
        }


def _in_hand(block: TillNumberBlock, held: dict[int, int]) -> int:
    """How many numbers of ``block`` the counter says it still has."""
    next_n = held.get(block.pk)
    if next_n is None:
        return 0
    return max(0, block.last_n - max(next_n, block.first_n) + 1)


def till_numbering(
    store: Store, till: RegisteredTill | None, held: dict[int, int], today: date | None = None
) -> TillNumbering:
    """Top up this counter's blocks and say what it holds (see the module docstring).

    ``held`` is ``{block id: the next number the counter would take}`` for every
    block the counter still holds. A block for a month the new format has not
    reached is never issued.
    """
    today = today or timezone.localdate()
    setting = numbering_setting(store.tenant_id)
    prefix = prefix_for_site(store)
    answer = TillNumbering(
        on=is_feature_on(store, FEATURE_KEY),
        new_format_from=setting.new_format_from,
        prefix=prefix.code if prefix else "",
        block_size=setting.till_block_size,
    )
    if not answer.on or till is None:
        return answer
    months = [
        month
        for month in (month_of(today), next_month(month_of(today)))
        # A block for a month is needed if any day of it is on or after the start.
        if next_month(month) > setting.new_format_from
    ]
    if not months:
        return answer
    if prefix is None:
        refused = NumberRefused(
            f"{store.code} has no document prefix, so its counter cannot be given invoice "
            "numbers in the new series. Admin sets one in Setup, Document Numbering.",
            reason="no_prefix",
            series=INVOICE,
            store_id=store.pk,
            owner=store.code,
        )
        raise_number_alert(refused)
        answer.problem = refused.message
        return answer
    try:
        with transaction.atomic():
            RegisteredTill.objects.select_for_update().filter(pk=till.pk).first()
            kept: list[TillNumberBlock] = []
            for month in months:
                live = list(
                    TillNumberBlock.objects.filter(
                        till=till, month=month, closed_at__isnull=True, pk__in=list(held)
                    ).order_by("first_n")
                )
                kept += [block for block in live if _in_hand(block, held) > 0]
                if sum(_in_hand(block, held) for block in live) >= setting.till_block_size:
                    continue
                fy = financial_year(month)
                first, last = allocate(prefix, INVOICE, fy, setting.till_block_size)
                kept.append(
                    TillNumberBlock.objects.create(
                        till=till,
                        prefix=prefix,
                        prefix_code=prefix.code,
                        fy=fy,
                        month=month,
                        first_n=first,
                        last_n=last,
                    )
                )
    except NumberRefused as refused:
        # The transaction above has rolled back; the alert outlives it.
        raise_number_alert(refused)
        answer.problem = refused.message
        kept = list(
            TillNumberBlock.objects.filter(
                till=till, month__in=months, closed_at__isnull=True, pk__in=list(held)
            )
        )
    answer.blocks = [_block_json(block) for block in sorted(kept, key=lambda b: b.first_n)]
    return answer


# --- at the bill ---------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceCheck:
    """What the server makes of the invoice number a bill arrived with."""

    #: The number to write on the bill, or None when it cannot vouch for one.
    number: str | None
    block: TillNumberBlock | None = None
    n: int | None = None
    #: A flag to raise once the bill exists: ``(kind, details)``.
    flag: tuple[str, dict[str, Any]] | None = None


def resolve_invoice_number(data: dict[str, Any], store: Store) -> InvoiceCheck:
    """Check the bill's number in the new series before the bill is written."""
    claimed = (data.get("tax_invoice_number") or "").strip()
    billed_on = timezone.localdate(data["billed_at"])
    if not claimed:
        from masters.document_series import new_format_applies

        if new_format_applies(store, billed_on):
            return InvoiceCheck(
                number=None,
                flag=(
                    ContinuityFlag.Kind.INVOICE_NUMBER_MISSING,
                    {"billed_on": billed_on.isoformat()},
                ),
            )
        return InvoiceCheck(number=None)

    def problem(reason: str, message: str) -> InvoiceCheck:
        return InvoiceCheck(
            number=None,
            flag=(
                ContinuityFlag.Kind.INVOICE_NUMBER_PROBLEM,
                {"number": claimed[:40], "reason": reason, "message": message},
            ),
        )

    parsed = parse_invoice_number(claimed)
    if parsed is None:
        return problem("format", f"{claimed[:40]} is not a tax invoice number in the new series.")
    prefix_code, fy, n = parsed
    block = (
        TillNumberBlock.objects.filter(
            till__store=store, prefix_code=prefix_code, fy=fy, first_n__lte=n, last_n__gte=n
        )
        .select_related("prefix")
        .first()
    )
    if block is not None:
        # Lock the block for the rest of the bill's transaction. Month end locks
        # it too before cancelling what is unused, and so does a second bill
        # arriving with the same number, so each waits for the other to commit
        # and then sees what it did: a number is never both on a bill and
        # cancelled, and never on two bills.
        TillNumberBlock.objects.select_for_update().filter(pk=block.pk).first()
    if block is None:
        return problem(
            "not_issued",
            f"{claimed} is not in any block of invoice numbers issued to {store.code}.",
        )
    if month_of(billed_on) != block.month:
        # Kept, because the customer holds it; flagged, because a block belongs to
        # one month and month end cancels what it did not use.
        return InvoiceCheck(
            number=claimed,
            block=block,
            n=n,
            flag=(
                ContinuityFlag.Kind.INVOICE_NUMBER_PROBLEM,
                {
                    "number": claimed,
                    "reason": "other_month",
                    "message": f"{claimed} is from the block for {block.month:%B %Y}, "
                    f"but the bill is dated {billed_on.isoformat()}.",
                },
            ),
        )
    taken = (
        Sale.objects.filter(tax_invoice_number=claimed)
        .exclude(idempotency_uuid=data["idempotency_uuid"])
        .first()
    )
    if taken is not None:
        return problem("duplicate", f"{claimed} is already on bill {taken.doc_number}.")
    return InvoiceCheck(number=claimed, block=block, n=n)


def record_invoice_number(sale: Sale, check: InvoiceCheck) -> tuple[str, dict[str, Any]] | None:
    """Write the bill's number into the register of used numbers.

    Answers a flag to raise when the number had been cancelled at month end
    before the bill arrived; the alert goes to head office with it.
    """
    if check.number is None or check.block is None or check.n is None:
        return check.flag
    try:
        with transaction.atomic():
            recorded = record_number(
                check.block.prefix,
                INVOICE,
                check.block.fy,
                check.n,
                on=timezone.localdate(sale.billed_at),
                document_type="sale",
                document_ref=str(sale.pk),
            )
    except NumberRefused as refused:
        return (
            ContinuityFlag.Kind.INVOICE_NUMBER_PROBLEM,
            {"number": check.number, "reason": refused.reason, "message": refused.message},
        )
    if recorded.was_cancelled:
        message = (
            f"{check.number} was cancelled at month end as unused, then arrived on a bill. "
            "If the cancellation is already in GSTR-1, Accounts corrects it."
        )
        raise_number_alert(
            NumberRefused(
                message,
                reason="used_after_cancel",
                series=INVOICE,
                number=check.number,
                store_id=sale.store_id,
                owner=check.number,
            )
        )
        return (
            ContinuityFlag.Kind.INVOICE_NUMBER_PROBLEM,
            {"number": check.number, "reason": "used_after_cancel", "message": message},
        )
    return check.flag


# --- at month end ----------------------------------------------------------------


def close_month(today: date | None = None) -> int:
    """Cancel the unused numbers of every block for a month that has ended.

    Idempotent: a closed block is not looked at again, and a number already used
    or cancelled is left as it is. Answers how many numbers it cancelled.
    """
    today = today or timezone.localdate()
    this_month = month_of(today)
    cancelled = 0
    blocks = (
        TillNumberBlock.objects.filter(month__lt=this_month, closed_at__isnull=True)
        .select_related("till__store", "prefix")
        .order_by("month", "first_n")
    )
    for block in blocks:
        try:
            cancelled += _close_block(block)
        except IntegrityError:
            # One block that cannot close tonight must not stop the others; it
            # stays open and the next run tries again.
            log.exception("Could not close invoice number block %s", block.pk)
    return cancelled


def _close_block(block: TillNumberBlock) -> int:
    with tenant_context(block.till.store.tenant_id), transaction.atomic():
        locked = TillNumberBlock.objects.select_for_update().get(pk=block.pk)
        if locked.closed_at is not None:
            return 0
        count = cancel_unused(
            block.prefix,
            INVOICE,
            block.fy,
            block.first_n,
            block.last_n,
            period=block.month,
            reason=UNUSED_BLOCK_REASON,
        )
        locked.closed_at = timezone.now()
        locked.cancelled_count = count
        locked.save(update_fields=["closed_at", "cancelled_count"])
        return count


def blocks_in_use(store_ids: list[int]) -> list[dict[str, Any]]:
    """Every open block at these stores, for the Document Numbering page."""
    rows = (
        TillNumberBlock.objects.filter(till__store_id__in=store_ids)
        .filter(Q(closed_at__isnull=True))
        .select_related("till__store")
        .order_by("till__store__code", "month", "first_n")
    )
    return [
        {
            **_block_json(block),
            "store": block.till.store.code,
            "counter": block.till.series_prefix,
            "first_number": render(INVOICE, block.prefix_code, block.fy, block.first_n),
            "last_number": render(INVOICE, block.prefix_code, block.fy, block.last_n),
        }
        for block in rows
    ]
