"""Outbound posting engine — stock movements out of a location + balanced GL.

Follows the kernel pattern: every event writes append-only stock entries AND
(where applicable) balanced value GL vouchers via `core.post_entries`.
Stock-on-hand is updated atomically within the same transaction.

All amounts are integer paise. Debit is positive, credit is negative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.utils import timezone

from approvals.models import CLEARED_STATUSES
from core.gl import GLAccount
from core.posting import PostingRef, assert_pt_or_vflip_actor, cr, dr, post_entries
from masters.ownership import brand_is_owned

# Re-exported on purpose (see ``outbound.costing``'s module note): for a caller,
# the error still belongs to posting. The `as` spelling is what makes that a
# deliberate re-export rather than an incidental import.
from outbound.costing import OutboundPostingError as OutboundPostingError
from outbound.costing import book_unit_cost
from outbound.maker_checker import request_document_approval, require_approved
from stockledger.models import (
    MERCH_DIM_FIELDS,
    InTransitStock,
    QuarantineStock,
    StockLedgerEntry,
    StockOnHand,
    merch_dims,
)

if TYPE_CHECKING:
    from accounts.models import User
    from masters.models import Gstin, Store
    from outbound.models import (
        MarkDamaged,
        ReturnToVendor,
        StockAdjustment,
        StockRequest,
        StockRequestClosure,
        StoreTransfer,
        StoreTransferLine,
        TransferGapClosure,
        TransferGapClosureLine,
        TransferReceiptException,
        VFlip,
        WriteOff,
    )


def _check_stock(store_id: int, sku_code: str, required_qty: int) -> None:
    """Block if insufficient stock at the source location.

    Row-locks the on-hand row (``select_for_update``) so two concurrent
    dispatches of the same pieces serialize instead of both passing the check
    and overselling — callers run inside ``transaction.atomic``.
    """
    try:
        on_hand = StockOnHand.objects.select_for_update().get(store_id=store_id, sku_code=sku_code)
        available = on_hand.net_qty
    except StockOnHand.DoesNotExist:
        available = 0
    if available < required_qty:
        raise OutboundPostingError(
            f"Insufficient stock for {sku_code} at store {store_id}: "
            f"available={available}, required={required_qty}"
        )


def _line_amount(line: Any, qty: int, doc_number: str) -> int:
    """Value one leg of a stock movement — qty × the line's frozen unit cost.

    Refuses a line the books never priced (#103). Not an exception to
    flag-don't-block but Rule 5's own "unless it is truly dangerous" clause,
    which the rules page now defines for money: a zero-value stock movement is
    not a flagged approximation, it is a wrong number presented as a right one
    — the pieces leave and the rupees stay, and every quantity-based check,
    including a physical count, passes over it.
    """
    unit_cost_paise = getattr(line, "unit_cost_paise", 0) or 0
    if unit_cost_paise <= 0:
        raise OutboundPostingError(
            f"{line.sku_code} on {doc_number} carries no unit cost, so this movement "
            "cannot be valued. Stock never moves at zero value — re-make the document "
            "so its cost is read from the books."
        )
    return qty * unit_cost_paise


def _write_stock_entry(
    *,
    store: Store,
    gstin: Gstin,
    line: Any,
    qty: int,
    kind: str,
    doc_number: str,
    line_no: int,
    posted_by: Any = None,
) -> StockLedgerEntry:
    """Create a single stock ledger entry and update StockOnHand."""
    amount_paise = _line_amount(line, qty, doc_number)
    entry = StockLedgerEntry.objects.create(
        store=store,
        gstin=gstin,
        sku_code=line.sku_code,
        design=getattr(line, "design", ""),
        color=getattr(line, "color", ""),
        size=getattr(line, "size", ""),
        brand=getattr(line, "brand", ""),
        season=getattr(line, "season", ""),
        item=getattr(line, "item", ""),
        hsn=getattr(line, "hsn", ""),
        qty=qty,
        amount=amount_paise,
        kind=kind,
        doc_number=doc_number,
        line_no=line_no,
        posted_by=posted_by,
    )
    # Update materialised StockOnHand
    obj, _ = StockOnHand.objects.get_or_create(
        store=store,
        sku_code=line.sku_code,
        defaults={
            "gstin": gstin,
            "design": getattr(line, "design", ""),
            "color": getattr(line, "color", ""),
            "size": getattr(line, "size", ""),
            "brand": getattr(line, "brand", ""),
            "season": getattr(line, "season", ""),
            "item": getattr(line, "item", ""),
            "hsn": getattr(line, "hsn", ""),
            "net_qty": 0,
            "net_value_paise": 0,
        },
    )
    obj.net_qty += qty
    obj.net_value_paise += amount_paise
    # Refresh descriptive fields on inward (positive qty)
    if qty > 0:
        obj.design = getattr(line, "design", "") or obj.design
        obj.color = getattr(line, "color", "") or obj.color
        obj.size = getattr(line, "size", "") or obj.size
        obj.brand = getattr(line, "brand", "") or obj.brand
        obj.season = getattr(line, "season", "") or obj.season
        obj.item = getattr(line, "item", "") or obj.item
        obj.hsn = getattr(line, "hsn", "") or obj.hsn
        obj.gstin = gstin
    obj.save()
    return entry


# ---------------------------------------------------------------------------
# 1. Store Transfer — scanned dispatch / receive with an in-transit bucket
# ---------------------------------------------------------------------------
#
# Dispatch: source −qty (transfer_out) + in-transit +qty (transit_in), both
# under the transfer's doc number. Receive: in-transit −qty (transit_out) +
# destination +qty (transfer_in). The pieces are never in no location, and
# free-to-sell (StockOnHand) excludes in-transit throughout.
#
# Scan-is-truth (#68): the scanned quantities are the only quantities that
# post. A scanned-vs-plan gap is flagged, never blocked (Rule 5); a barcode
# outside the plan (or, at receive, outside the dispatched lines) is rejected.

# line_no bands — one doc number carries every leg type this transfer will ever
# write, so each type gets its own band (dispatch writes OUT+TRANSIT_IN, receive
# TRANSIT_OUT+IN and the extras' TRANSFER_IN; a gap closure drains what is left).
# Damage on arrival writes nothing under the transfer's number any more: it goes
# through a DMG document of its own (#138), which carries its own legs.
LINE_NO_TRANSFER_OUT = 0
LINE_NO_TRANSIT_IN = 500
LINE_NO_TRANSFER_IN = 1000
LINE_NO_TRANSIT_OUT = 1500
LINE_NO_EXTRA_IN = 2500
LINE_NO_GAP_DRAIN = 3000


def _validate_scans(scans: dict[str, int], allowed: set[str] | None, context: str) -> None:
    """Shared scan-payload guard: non-empty, qty ≥ 1, and (when a line set is
    given) every scanned barcode on it — the wrong-piece beep, server-side."""
    if not scans or any(q < 1 for q in scans.values()):
        raise OutboundPostingError(f"{context} needs scanned lines (barcode × qty ≥ 1).")
    if allowed is not None:
        unknown = sorted(set(scans) - allowed)
        if unknown:
            raise OutboundPostingError(
                f"Not on this transfer: {', '.join(unknown)}. "
                "Scanned pieces must match the transfer receipt."
            )


def _write_transit_entry(
    *,
    transfer: StoreTransfer,
    line: Any,
    qty: int,
    kind: str,
    line_no: int,
    posted_by: Any = None,
) -> StockLedgerEntry:
    """One in-transit ledger leg + the InTransitStock projection update.

    Transit legs ride on the *source* store (the sender is answerable until
    the receiver scans in) and deliberately do NOT touch StockOnHand — the
    bucket lives in InTransitStock, keyed to the transfer's doc number.
    """
    # ``Document.doc_number`` is NULL only while a document is a draft: `post()`
    # mints it, and a DB check constraint refuses a posted row without one. Every
    # read of it in this module happens after the document has posted, so the
    # Optional on the field is a fact about drafts, not about the value here —
    # which is what the casts below say, throughout.
    amount_paise = _line_amount(line, qty, cast(str, transfer.doc_number))
    entry = StockLedgerEntry.objects.create(
        store=transfer.source_store,
        gstin=transfer.source_store.gstin,
        sku_code=line.sku_code,
        **merch_dims(line),
        qty=qty,
        amount=amount_paise,
        kind=kind,
        doc_number=cast(str, transfer.doc_number),
        line_no=line_no,
        posted_by=posted_by,
    )
    bucket, _ = InTransitStock.objects.get_or_create(
        transfer_doc_number=cast(str, transfer.doc_number),
        sku_code=line.sku_code,
        defaults={
            "source_store": transfer.source_store,
            "destination_store": transfer.destination_store,
            "gstin": transfer.source_store.gstin,
            **merch_dims(line),
            "qty": 0,
            "value_paise": 0,
        },
    )
    bucket.qty += qty
    bucket.value_paise += amount_paise
    if bucket.qty == 0:
        # A fully-received transfer leaves no bucket row — matching the rebuild
        # command, which also emits no row for a zeroed (doc, sku) pair.
        bucket.delete()
    else:
        bucket.save()
    return entry


def _cannot_price(store_id: int, sku_code: str, season: str) -> str:
    """Why the books could not price this piece, in words the maker can act on.

    Two different problems wear the same "cost is unknown" answer, and they need
    opposite fixes: a piece the books never bought has to be brought in first,
    while a piece bought in several seasons only needs the maker to say which
    one. Telling them apart costs one query on the refusal path.
    """
    from masters.models import Cohort

    if not season and not StockOnHand.objects.filter(store_id=store_id, sku_code=sku_code).exists():
        seasons = sorted(Cohort.objects.filter(barcode=sku_code).values_list("season", flat=True))
        if len(seasons) > 1:
            return (
                f"{sku_code} was bought in more than one season ({', '.join(seasons)}), each at "
                "its own cost, and the store holds none of it — so the books cannot tell which "
                "buy this piece came from. Say which season it belongs to and make this "
                "document again."
            )
    return (
        f"The books cannot price {sku_code} at this location, so it cannot be "
        "valued. Bring the piece in on a GRN/PT first (or correct its cost), "
        "then make this document again."
    )


def resolve_line_cost(store_id: int, sku_code: str, season: str = "") -> int:
    """The books' unit cost for one line — or a refusal (#103).

    The single derive-or-refuse seam for every value-bearing outbound line. The
    cost is *always* read here and never accepted from the caller: a maker who
    could type it could type a piece out of the books for nothing, and the same
    number decides who has to approve the document.

    Where the books cannot price the piece, the line is refused rather than
    defaulted to free — see ``_line_amount`` for why zero is the one thing a
    money posting may not fall back to.
    """
    cost = book_unit_cost(store_id, sku_code, season)
    if cost <= 0:
        raise OutboundPostingError(_cannot_price(store_id, sku_code, season))
    return cost


@dataclass
class _ValueByOwner:
    """A lot's value split by whose stock it is — KDPS's own, or a brand's on SOR.

    A named pair rather than a ``dict[bool, int]`` because ownership is a *three*
    -state answer: ``brand_is_owned`` returns ``None`` when the masters cannot
    say, and a bool-keyed dict invites ``bucket[bool(brand_is_owned(...))]``,
    which files an unknown brand under "brand-owned" and posts a memo pair for
    stock that may well be ours. ``add`` refuses the undecided case outright, so
    the contract ``brand_is_owned`` documents — treat ``None`` as a reason to
    refuse, never to assume — is enforced where the money is bucketed rather
    than left to each caller to remember.
    """

    own: int = 0
    sor: int = 0

    def add(self, owned: bool | None, paise: int) -> None:
        if owned is None:
            raise OutboundPostingError(
                "The books cannot say who owns these pieces, so their value "
                "cannot be booked either way."
            )
        if owned:
            self.own += paise
        else:
            self.sor += paise


def resolve_line_identity(store_id: int, sku_code: str, season: str = "") -> dict[str, int | str]:
    """Merchandising dims + unit cost for one document line, read from the books.

    What ``_resolve_scan_identity`` does for a scan, for the typed documents
    (write-off, adjustment, RTV, V-flip) — with one difference: it also answers
    for a piece the location holds none of, which is how a stocktake surplus is
    priced from its cohort. Dims the books do not know are left to the caller's
    payload; the cost never is.
    """
    on_hand = StockOnHand.objects.filter(store_id=store_id, sku_code=sku_code).first()
    dims = {k: v for k, v in merch_dims(on_hand).items() if v} if on_hand is not None else {}
    return {**dims, "unit_cost_paise": resolve_line_cost(store_id, sku_code, season)}


def _resolve_scan_identity(store_id: int, barcode: str) -> dict[str, int | str]:
    """Merchandising dims + unit cost for a scanned barcode, from the source
    location's stock (the ledger is self-describing) and the cohort's frozen
    P-RATE cost where one exists (falling back to the on-hand average).

    Unlike ``resolve_line_identity`` a scan must *be there* to be scanned, so a
    location holding none of the piece is the wrong-piece beep, not a pricing
    question."""
    if not StockOnHand.objects.filter(store_id=store_id, sku_code=barcode).exists():
        raise OutboundPostingError(f"Barcode {barcode} has no stock at the source location.")
    return resolve_line_identity(store_id, barcode)


def _generate_transfer_pt(transfer: StoreTransfer, user: Any = None) -> None:
    """Freeze the transfer's PT onto it, inside the dispatch transaction (#72).

    Every transfer generates one — never conditionally, never on request — so
    "the carton went without its paper" is not a state the system can reach. It
    is a packing document: it writes no ledger and raises no liability, and no
    inbound-PT right is consulted to produce it.
    """
    from outbound.models import TransferPT
    from outbound.transfer_pt import build_transfer_pt_rows

    TransferPT.objects.create(
        transfer=transfer,
        rows=build_transfer_pt_rows(transfer),
        generated_by=user if getattr(user, "is_authenticated", False) else None,
    )


@transaction.atomic
def post_transfer_dispatch(
    transfer: StoreTransfer, scans: dict[str, int], user: Any = None
) -> list[StockLedgerEntry]:
    """Post the dispatch side from scanned quantities only.

    ``scans`` maps barcode → scanned qty. With plan lines, every scanned
    barcode must be on the plan (wrong-piece beep otherwise); a quantity gap
    is flagged, never blocked. With no plan lines (store→store scan-to-build)
    the scans *become* the lines, enriched from the source stock.

    Stock move only, no GL for a normal move (cross-state IGST invoice is manual
    by decision) — except a partner-store destination, which is always billed at
    Purchase Price and, per the chain's `BillingPolicy` dial, may also raise a
    receivable (see `_bill_partner_store`).

    Refuses unless the Operations Head (or the Owner, overriding) has approved
    the transfer — ``require_approved``, at the posting layer rather than the
    view, so a shell, a management command and the API all hit the same wall
    (#137). Nothing leaves the shelf on the sender's own say-so.
    """
    from core.documents import DocStatus
    from masters.models import Sku
    from outbound.models import StoreTransfer, StoreTransferLine

    # Serialize on the transfer row: a concurrent dispatch of the same draft
    # blocks here, then sees SUBMITTED and fails instead of double-posting.
    locked = StoreTransfer.objects.select_for_update().get(pk=transfer.pk)
    if locked.docstatus != DocStatus.DRAFT:
        raise OutboundPostingError("Transfer already dispatched.")

    # Before a single piece is validated: an unapproved transfer is not a
    # dispatch with a problem, it is not a dispatch.
    require_approved(transfer)

    plan_lines = {line.sku_code: line for line in transfer.lines.all()}
    _validate_scans(scans, set(plan_lines) if plan_lines else None, "Dispatch")

    # Validate stock + resolve identity/cost from the source location
    for barcode, qty in scans.items():
        _check_stock(transfer.source_store_id, barcode, qty)

    # The ticketed price of each scanned piece, snapshotted with the cost so the
    # transfer's PT stays true to the moment of dispatch (Rule 2, #72).
    mrps = dict(
        Sku.objects.filter(barcode__in=scans).values_list("barcode", "mrp_paise"),
    )

    dispatch_lines = []
    for barcode, qty in sorted(scans.items()):
        identity = _resolve_scan_identity(transfer.source_store_id, barcode)
        line = plan_lines.get(barcode)
        if line is None:
            line = StoreTransferLine(transfer=transfer, sku_code=barcode, qty_planned=None)
        line.qty_dispatched = qty
        line.mrp_paise = mrps.get(barcode)
        for field, value in identity.items():
            setattr(line, field, value)
        line.save()
        dispatch_lines.append(line)

    # Set dispatch metadata BEFORE post() (submitted docs are DB-immutable), and
    # persist it: `post()` writes only the four FSM columns with `update_fields`,
    # so an in-memory-only assignment here left `dispatch_date` and
    # `dispatched_by` NULL for good — the screen has always shown a blank
    # "dispatched by" on every transfer ever sent.
    #
    # `partner_billing_value_paise` joins the same save for the same reason: a
    # partner store is billed at Purchase Price on every transfer it receives
    # (#see `_bill_partner_store`), and the figure must be computed from the
    # scanned lines while they are still in hand — not after `post()`, when the
    # document itself can no longer be written.
    transfer.dispatch_date = timezone.now()
    transfer.dispatched_by = user
    update_fields = ["dispatch_date", "dispatched_by"]
    if transfer.destination_store.is_partner:
        transfer.partner_billing_value_paise = sum(
            line.qty_dispatched * line.unit_cost_paise for line in dispatch_lines
        )
        update_fields.append("partner_billing_value_paise")
    transfer.save(update_fields=update_fields)

    # Post the document (mint number, set SUBMITTED — saves everything atomically)
    transfer.post()

    _generate_transfer_pt(transfer, user)

    entries = []
    for i, line in enumerate(dispatch_lines, start=1):
        # Stock OUT at source (free-to-sell drops immediately)
        entries.append(
            _write_stock_entry(
                store=transfer.source_store,
                gstin=transfer.source_store.gstin,
                line=line,
                qty=-line.qty_dispatched,
                kind="transfer_out",
                doc_number=cast(str, transfer.doc_number),
                line_no=LINE_NO_TRANSFER_OUT + i,
                posted_by=user,
            )
        )
        # Stock INTO the in-transit bucket under this transfer
        entries.append(
            _write_transit_entry(
                transfer=transfer,
                line=line,
                qty=line.qty_dispatched,
                kind="transit_in",
                line_no=LINE_NO_TRANSIT_IN + i,
                posted_by=user,
            )
        )

    _bill_partner_store(transfer, user)

    return entries


def _bill_partner_store(transfer: StoreTransfer, user: Any = None) -> None:
    """The other half of partner billing: raising the receivable, if the
    chain-wide `BillingPolicy` dial says to.

    `partner_billing_value_paise` is already computed and saved (before
    `post()` — a submitted document cannot be written again), so this only
    ever reads it. Every other transfer stays "stock move only, no GL" exactly
    as before — this only ever fires when the destination itself is flagged a
    partner and holds a positive billed value.
    """
    from outbound.models import BillingPolicy

    value_paise = transfer.partner_billing_value_paise or 0
    if value_paise <= 0 or BillingPolicy.current().mode != BillingPolicy.Mode.GL_POSTING:
        return

    doc_ref = PostingRef(
        doc_type="STO",
        doc_number=cast(str, transfer.doc_number),
        store=transfer.destination_store,
        gstin=transfer.destination_store.gstin,
        posted_by=user,
    )
    post_entries(
        doc_ref,
        [
            dr(
                GLAccount.PARTNER_RECEIVABLE,
                value_paise,
                memo=f"Partner billing: {transfer.destination_store.code} billed at PP",
            ),
            cr(GLAccount.INVENTORY, value_paise, memo="Partner billing: inventory exit at PP"),
        ],
        posted_by=user,
    )


def _resolve_extra_identity(transfer: StoreTransfer, barcode: str) -> dict[str, int | str]:
    """Dims + book cost for a piece that arrived but was never sent (#71).

    An extra is, by definition, a piece the destination was not expecting, so
    ``_resolve_scan_identity``'s "it must be there to be scanned" rule cannot
    apply. The books are asked at the destination first, then at the sender —
    the two places that plausibly know this barcode — and the item master fills
    in dims where neither location holds any.

    A cost of 0 means *the books cannot price this piece*, which is not the same
    as free (#103). The caller records the extra and refuses to post it into
    stock rather than inventing a value for it.
    """
    from masters.models import Sku

    sku = Sku.objects.filter(barcode=barcode).first()
    master_dims = {k: v for k, v in merch_dims(sku).items() if v} if sku is not None else {}
    for store_id in (transfer.destination_store_id, transfer.source_store_id):
        cost = book_unit_cost(store_id, barcode)
        if cost <= 0:
            continue
        on_hand = StockOnHand.objects.filter(store_id=store_id, sku_code=barcode).first()
        dims = {k: v for k, v in merch_dims(on_hand).items() if v} if on_hand is not None else {}
        return {**master_dims, **dims, "unit_cost_paise": cost}
    return {**master_dims, "unit_cost_paise": 0}


def _validate_receive(
    lines: dict[str, StoreTransferLine],
    good: dict[str, int],
    damaged: dict[str, int],
    extras: dict[str, int],
) -> None:
    """Guard the receive payload before a single leg is written.

    The three buckets divide the scanned carton between them, so they are
    validated together: good and damaged pieces must be *on* the transfer (and
    together no more than was sent), an extra must be a barcode the transfer
    never carried, and something must have arrived for this to be a receipt.
    """
    if not (good or damaged or extras):
        raise OutboundPostingError("Receive needs scanned lines (barcode × qty ≥ 1).")
    if any(q < 1 for q in (*good.values(), *damaged.values(), *extras.values())):
        raise OutboundPostingError("Receive needs scanned lines (barcode × qty ≥ 1).")

    unknown = sorted((set(good) | set(damaged)) - set(lines))
    if unknown:
        raise OutboundPostingError(
            f"Not on this transfer: {', '.join(unknown)}. A piece the transfer never sent "
            "is an extra — record it as one so it is flagged, not counted as received."
        )
    misfiled = sorted(set(extras) & set(lines))
    if misfiled:
        raise OutboundPostingError(
            f"On this transfer, so not an extra: {', '.join(misfiled)}. "
            "Scan it as received (or damaged) instead."
        )
    for barcode, line in lines.items():
        arrived = good.get(barcode, 0) + damaged.get(barcode, 0)
        if arrived > line.qty_dispatched:
            raise OutboundPostingError(
                f"Scanned more than was sent for {barcode}: "
                f"sent={line.qty_dispatched}, scanned={arrived}."
            )


@transaction.atomic
def post_transfer_receipt(
    transfer: StoreTransfer,
    scans: dict[str, int],
    user: Any = None,
    *,
    damaged: dict[str, int] | None = None,
    extras: dict[str, int] | None = None,
    notes: str = "",
) -> list[StockLedgerEntry]:
    """Post the receive side from scanned quantities only, exceptions included (#71).

    Four things come off the scan screen and all four are structured:

    * ``scans`` — pieces that arrived intact: in-transit → destination, sellable.
    * ``damaged`` — pieces that arrived broken. They come in like the rest, and
      a **damage document** is raised for them (#138): the warehouse receiving
      its own carton holds the confirming rung, so they go on to quarantine here
      and now; a store person's word is a flag, so they wait in the store's
      stock until a warehouse or HO person confirms it.
    * ``extras`` — pieces that arrived but the transfer never sent. Accepted in
      with a flag where the books can price them (a surplus, so it posts a
      balanced Dr INVENTORY / Cr SUSPENSE voucher like a stocktake surplus);
      recorded but *not* posted into stock where they cannot, since a movement
      valued at zero is a wrong number dressed as a right one (#103). Either way
      the piece is on the record — never silently swallowed.
    * ``notes`` — what the receiver typed about the shortfall, which until now
      was dropped from the payload before the server ever saw it.

    Damaged pieces count as **received**: they arrived, so they leave the
    in-transit bucket. Only pieces that never turned up are short, and those stay
    in the bucket on the still-open transfer until a senior closes the gap
    (``post_gap_closure``) — which is why this function keeps refusing a second
    receive: the way out of a shortfall is the closure, not another scan.
    """
    from core.documents import DocStatus
    from outbound.models import (
        ReceiptExceptionKind,
        ReceiptStatus,
        StoreTransfer,
        TransferReceipt,
        TransferReceiptException,
    )

    if transfer.docstatus != DocStatus.SUBMITTED:
        raise OutboundPostingError("Transfer must be dispatched (submitted) before receipt.")
    # Serialize on the transfer row: a concurrent receive of the same transfer
    # blocks here, then the already-received check below sees the first receipt.
    StoreTransfer.objects.select_for_update().get(pk=transfer.pk)
    if TransferReceipt.objects.filter(transfer=transfer).exists():
        raise OutboundPostingError(
            "Transfer already received. Missing pieces are closed from the gaps "
            "list by a senior, not by receiving the transfer twice."
        )

    good = dict(scans or {})
    damaged = dict(damaged or {})
    extras = dict(extras or {})
    lines = {line.sku_code: line for line in transfer.lines.all() if line.qty_dispatched > 0}
    _validate_receive(lines, good, damaged, extras)

    dest = transfer.destination_store
    entries: list[StockLedgerEntry] = []
    exceptions: list[TransferReceiptException] = []
    # The damaged subset of ``exceptions``, same objects — they all take the same
    # note once the damage document below says where the decision got to.
    broken: list[TransferReceiptException] = []
    has_shortfall = False

    for i, (barcode, line) in enumerate(sorted(lines.items()), start=1):
        qty_good = good.get(barcode, 0)
        qty_damaged = damaged.get(barcode, 0)
        arrived = qty_good + qty_damaged
        line.qty_received = arrived
        line.save(update_fields=["qty_received"])

        short = line.qty_dispatched - arrived
        if short > 0:
            has_shortfall = True
            exceptions.append(
                TransferReceiptException(
                    kind=ReceiptExceptionKind.SHORT,
                    sku_code=barcode,
                    **merch_dims(line),
                    qty=short,
                    unit_cost_paise=line.unit_cost_paise,
                    note="Stays in transit until the gap is closed.",
                )
            )
        if arrived == 0:
            continue

        # OUT of the in-transit bucket — everything that turned up, damaged or not
        entries.append(
            _write_transit_entry(
                transfer=transfer,
                line=line,
                qty=-arrived,
                kind="transit_out",
                line_no=LINE_NO_TRANSIT_OUT + i,
                posted_by=user,
            )
        )
        # …and everything that turned up INTO the destination. Broken pieces
        # arrive too: taking them off the floor is a damage decision now, and
        # the receiver may not be senior enough to make it (#138).
        entries.append(
            _write_stock_entry(
                store=dest,
                gstin=dest.gstin,
                line=line,
                qty=arrived,
                kind="transfer_in",
                doc_number=cast(str, transfer.doc_number),
                line_no=LINE_NO_TRANSFER_IN + i,
                posted_by=user,
            )
        )
        if qty_damaged:
            broken_row = TransferReceiptException(
                kind=ReceiptExceptionKind.DAMAGED,
                sku_code=barcode,
                **merch_dims(line),
                qty=qty_damaged,
                unit_cost_paise=line.unit_cost_paise,
            )
            exceptions.append(broken_row)
            broken.append(broken_row)

    if broken:
        # The whole receipt's breakages go through one damage door, so the note
        # saying where that got to is the same on every one of these rows. The
        # loop kept hold of them rather than leaving them to be picked back out
        # of ``exceptions`` by kind — it already knew which ones were damaged.
        note = _mark_arrivals_damaged(transfer, damaged, user)
        for exception in broken:
            exception.note = note

    entries += _post_receive_extras(transfer, extras, exceptions, user)

    receipt = TransferReceipt.objects.create(
        transfer=transfer,
        received_by=user,
        receipt_status=(ReceiptStatus.SHORTFALL if has_shortfall else ReceiptStatus.COMPLETE),
        shortfall_notes=notes,
    )
    for exception in exceptions:
        exception.receipt = receipt
    TransferReceiptException.objects.bulk_create(exceptions)

    return entries


def _mark_arrivals_damaged(
    transfer: StoreTransfer, damaged: dict[str, int], user: User | None = None
) -> str:
    """Send the broken arrivals through the one damage door (#138), and say on
    the receipt where that got to.

    Receiving used to write them straight into quarantine, which is a decision
    the receiver may not hold: a store person reports damage, a warehouse or HO
    person confirms it. So a DMG document is raised for them here — it posts on
    the spot if the receiver holds the confirming rung, and otherwise waits in
    the approvals inbox while the pieces sit in the destination's stock, flagged.

    Either way the receipt's own damaged rows still say what arrived broken;
    they are receipt history, and the returned note says where the decision got
    to.
    """
    from core.documents import DocStatus

    mark = mark_damaged(
        transfer.destination_store,
        damaged,
        user=user,
        note=f"Damaged on arrival — {transfer.doc_number}",
    )
    if mark.docstatus == DocStatus.SUBMITTED:
        return f"Damaged on arrival — quarantined ({mark.doc_number})."
    return "Damaged on arrival — flagged, waiting to be confirmed before it leaves stock."


def _post_receive_extras(
    transfer: StoreTransfer,
    extras: dict[str, int],
    exceptions: list[TransferReceiptException],
    user: Any = None,
) -> list[StockLedgerEntry]:
    """Bring the extras in — the priceable ones as stock, all of them on the record.

    A piece that arrived without being sent is real inventory appearing at a
    location with no document behind it, so on the value side it is a surplus:
    one balanced Dr INVENTORY / Cr SUSPENSE voucher for the lot, exactly as a
    stocktake surplus posts. Where the books cannot price the piece, the
    exception row is the whole outcome and its note says so — visible on the
    transfer and in the gaps list, so somebody has to deal with it.
    """
    from outbound.models import ReceiptExceptionKind, TransferReceiptException

    if not extras:
        return []

    dest = transfer.destination_store
    entries: list[StockLedgerEntry] = []
    surplus = _ValueByOwner()
    for i, (barcode, qty) in enumerate(sorted(extras.items()), start=1):
        identity = _resolve_extra_identity(transfer, barcode)
        owned = brand_is_owned(str(identity.get("brand", "")))
        note = _extra_note(int(identity["unit_cost_paise"]), owned)
        exceptions.append(
            TransferReceiptException(
                kind=ReceiptExceptionKind.EXTRA,
                sku_code=barcode,
                qty=qty,
                note=note,
                **{k: v for k, v in identity.items() if k != "unit_cost_paise"},
                unit_cost_paise=cast(int, identity["unit_cost_paise"]),
            )
        )
        if int(identity["unit_cost_paise"]) <= 0 or owned is None:
            continue
        line = _ExtraLine(barcode, identity)
        entries.append(
            _write_stock_entry(
                store=dest,
                gstin=dest.gstin,
                line=line,
                qty=qty,
                kind="transfer_in",
                doc_number=cast(str, transfer.doc_number),
                line_no=LINE_NO_EXTRA_IN + i,
                posted_by=user,
            )
        )
        surplus.add(owned, qty * int(identity["unit_cost_paise"]))

    ref = PostingRef(
        doc_type="STO",
        doc_number=cast(str, transfer.doc_number),
        store=dest,
        gstin=dest.gstin,
        posted_by=user,
    )
    if surplus.own:
        # KDPS's own stock appearing with no document behind it — a surplus, the
        # same shape a stocktake surplus posts.
        _post_value_pair(
            ref,
            GLAccount.INVENTORY,
            GLAccount.SUSPENSE,
            surplus.own,
            "Receive: extra pieces accepted in",
            "Receive: extra pieces contra",
            user,
        )
    if surplus.sor:
        # The brand's stock, held on SOR/consignment: counted by quantity, value
        # off-book behind the memo pair, and NOT a payable — nothing has been
        # sold, so nothing is owed yet.
        _post_value_pair(
            ref,
            GLAccount.SOR_STOCK,
            GLAccount.SOR_CONTRA,
            surplus.sor,
            "Receive: extra brand-owned pieces",
            "Receive: extra brand-owned contra",
            user,
        )
    return entries


def _post_value_pair(
    ref: PostingRef,
    debit: str,
    credit: str,
    paise: int,
    debit_memo: str,
    credit_memo: str,
    user: User | None = None,
) -> None:
    """One balanced two-leg voucher — the shape every value posting here takes.

    Written once so the four ownership branches in this module differ only in the
    account pair and the memo, which is the whole of what actually differs
    between them.
    """
    post_entries(
        ref,
        [dr(debit, paise, memo=debit_memo), cr(credit, paise, memo=credit_memo)],
        posted_by=user,
    )


def _extra_note(unit_cost_paise: int, owned: bool | None) -> str:
    """What happened to this extra, in a sentence the receiver's manager can act on.

    Two different unknowns keep a piece out of stock and they need different
    fixes, so they must not wear the same words: the books cannot *price* it, or
    the books cannot say *whose* it is. Either way the piece is on the record —
    the whole point of the exception row — but neither is guessed at.
    """
    if unit_cost_paise <= 0:
        return (
            "Not on this transfer, and the books cannot price it — recorded but NOT "
            "brought into stock. Bring it in on a GRN/PT."
        )
    if owned is None:
        return (
            "Not on this transfer, and its brand is not in the masters, so the books "
            "cannot say whether it is ours or the brand's — recorded but NOT brought "
            "into stock. Add the brand, then bring it in on a GRN/PT."
        )
    return "Not on this transfer — accepted into stock, flagged."


class _ExtraLine:
    """The line-shaped object ``_write_stock_entry`` needs for an extra piece.

    An extra has no ``StoreTransferLine`` behind it — that is what makes it an
    extra — so its dims and cost are carried here instead of being faked onto
    the transfer's plan.
    """

    def __init__(self, barcode: str, identity: dict[str, int | str]) -> None:
        self.sku_code = barcode
        for field in MERCH_DIM_FIELDS:
            setattr(self, field, identity.get(field, ""))
        self.unit_cost_paise = int(identity["unit_cost_paise"])


# ---------------------------------------------------------------------------
# 1a. Gap closure — draining what a short receive left in transit
# ---------------------------------------------------------------------------
#
# A short receive is honest but unfinished: the pieces nobody scanned in are
# still in the in-transit bucket, and the transfer stays open in a gap state.
# The Operations Head has to say what became of them — and the reason they give
# is not a note, it is the instruction to the ledger:
#
#   found later      the pieces did turn up  → in-transit → destination stock
#   wrongly scanned  they never left         → in-transit → back to the sender
#   lost in transit  they are gone           → in-transit → off the books (loss)
#
# **The "found later" sub-decision (#80, undecided until now): closure posts the
# entries directly; there is no second scan-receive against a received transfer.**
# One route, one document, one place the numbers come from — and, decisively, a
# second receive would put the closing act back in the receiving store's hands,
# which is the one thing this whole flow exists to prevent.
#
# The drain leg deliberately carries the *transfer's* doc number, not the
# closure's: ``InTransitStock`` is keyed by the transfer that holds the pieces,
# and the ledger rebuild groups transit legs the same way, so a drain filed
# under the closure's number would open a second bucket instead of emptying the
# first. The resolving legs and the GL voucher carry the closure's own number.


def _ensure_gap_series(closure: TransferGapClosure) -> None:
    """Make sure the gap-free GAP voucher series exists before ``post()``.

    Same reason the GRN path does it: a series row missing for a (FY, store) is
    a setup gap, and failing the closure over it would leave the pieces stuck in
    transit for a bookkeeping reason.
    """
    from core.documents import VoucherSeries

    fy, store_code, doc_type = closure.series_lookup()
    VoucherSeries.objects.get_or_create(fy=fy, store_code=store_code, doc_type=doc_type)


def _refuse_self_closure(closure: TransferGapClosure, *people: User | None) -> None:
    """The receiving store never closes its own gap (#71).

    Maker ≠ checker is not enough on its own: two people at the same store are
    still two people, and the store that scanned short is the one with something
    to explain. So the rule is about *entitlement*, not identity — anybody whose
    scope includes the destination is barred from both ends of this decision, and
    it is checked in the posting layer so no caller can route around it.

    ``None`` is accepted per person because a document made outside the API — a
    shell, a management command — has no recorded maker, and an unapproved one no
    checker. A missing person cannot be entitled to anything, so they are skipped
    here; that they are *required* is ``require_approved``'s rule, not this one.
    """
    from masters.scoping import actionable_store_ids

    destination_id = closure.transfer.destination_store_id
    for person in people:
        if person is None:
            continue
        allowed = actionable_store_ids(person)
        if allowed is not None and destination_id in allowed:
            raise OutboundPostingError(
                "A gap is closed by the Operations Head at HO, never by the store that "
                "received short — this decision has to sit with somebody outside "
                f"{closure.transfer.destination_store.code}."
            )


def build_gap_closure_lines(closure: TransferGapClosure) -> list[TransferGapClosureLine]:
    """The closure's lines, read off the still-open transfer — never typed.

    A closure resolves exactly what the ledger still holds, so its lines are the
    transfer's own in-transit remainders at the *frozen dispatch cost*. That cost
    is the number the bucket was filled at (itself derived from the books at
    dispatch, never from a payload), so draining at it empties the bucket to
    zero; re-deriving it here would leave value stranded behind an emptied
    quantity.
    """
    from outbound.models import TransferGapClosureLine

    lines = [
        TransferGapClosureLine(
            closure=closure,
            sku_code=line.sku_code,
            **merch_dims(line),
            qty=line.qty_in_transit,
            unit_cost_paise=line.unit_cost_paise,
        )
        for line in closure.transfer.lines.all()
        if line.qty_in_transit > 0
    ]
    if not lines:
        raise OutboundPostingError("This transfer has no gap — nothing is still in transit.")
    return lines


@transaction.atomic
def raise_gap_closure(
    transfer: StoreTransfer, *, reason: str, note: str = "", user: Any = None
) -> TransferGapClosure:
    """Raise the closure for a transfer's gap and put it in the approvals inbox.

    Nothing is resolved here — this is the *asking*. One transaction, so a
    closure that cannot be built (no gap, or the asker is the receiving store)
    leaves no orphan draft behind, and one that can is never left sitting
    without a checker.
    """
    from outbound.models import TransferGapClosure, TransferGapClosureLine

    if TransferGapClosure.objects.filter(transfer=transfer).exists():
        raise OutboundPostingError(
            "This transfer already has a gap closure — correct the one it has "
            "rather than raising a second."
        )

    closure = TransferGapClosure(
        transfer=transfer,
        store=transfer.source_store,
        reason=reason,
        note=note,
        created_by=user,
    )
    _refuse_self_closure(closure, user)
    closure.save()
    TransferGapClosureLine.objects.bulk_create(build_gap_closure_lines(closure))
    request_document_approval(closure, requested_by=user)
    return closure


@transaction.atomic
def amend_gap_closure(
    closure: TransferGapClosure,
    *,
    reason: str,
    note: str = "",
    user: User | None = None,
) -> TransferGapClosure:
    """Correct a draft closure and ask again.

    One gap gets one closure document — but until it posts, that document can be
    wrong: the wrong reason chosen, a checker who rejected it, or numbers the
    bucket has moved on from. Without a way back, any of those strands the short
    pieces in transit for good, which is the dead end this slice exists to close.

    The lines are rebuilt from the transfer rather than kept, so a correction
    cannot carry a number the maker typed or one the bucket has moved past.

    **A closure waiting for a decision cannot be corrected at all**, and that
    restriction carries the security of the whole thing rather than being a
    convenience: while a request is pending, the checker is reading the reason on
    this very document. Editable underneath them, the maker raises "found later",
    lets the checker approve *that*, and posts a write-off on an approval given
    for bringing the pieces back — the reason is an instruction to the ledger, so
    changing it changes what posts. A pending closure therefore waits for its
    answer, and correcting it afterwards always asks again, the old decision
    staying on the record beside the new request.
    """
    from approvals.models import ApprovalStatus
    from approvals.services import approval_for
    from core.documents import DocStatus
    from outbound.models import TransferGapClosureLine

    if closure.docstatus != DocStatus.DRAFT:
        raise OutboundPostingError("This gap closure has already been posted.")
    # The same entitlement bar as raising one: correcting a closure decides what
    # became of the pieces just as much as raising it did.
    _refuse_self_closure(closure, user)

    live = approval_for(closure)
    if live is not None and live.status == ApprovalStatus.PENDING:
        raise OutboundPostingError(
            "This closure is waiting for a decision, so it cannot be changed yet — "
            "a checker is reading the reason on it. Once they have approved it or "
            "turned it down, correct it and it goes back to them."
        )

    closure.reason = reason
    closure.note = note
    closure.save(update_fields=["reason", "note"])

    rebuilt = build_gap_closure_lines(closure)
    closure.lines.all().delete()
    TransferGapClosureLine.objects.bulk_create(rebuilt)

    # Not ``maker_checker.ask_again``: that is the rejection-only route, and an
    # *approved* closure has to go back to the inbox too. Asking directly still
    # carries the document's maker across, so a re-ask cannot move the author
    # aside and let them clear their own correction.
    if live is not None:
        request_document_approval(closure, requested_by=user)
    return closure


def _lock_and_read_gap_lines(closure: TransferGapClosure) -> list[Any]:
    """Take the lock this closure posts under, and read the lines it will drain.

    The lock is on the *transfer*, not the closure: two closures of the same gap
    are the collision that matters, and the second one has to wait here so it
    finds the bucket already drained rather than draining it twice.
    """
    from core.documents import DocStatus
    from outbound.models import StoreTransfer

    if closure.docstatus != DocStatus.DRAFT:
        raise OutboundPostingError("This gap closure has already been posted.")
    StoreTransfer.objects.select_for_update().get(pk=closure.transfer_id)

    lines = list(closure.lines.all())
    if not lines:
        raise OutboundPostingError("Gap closure has no lines.")
    return lines


def _refuse_stale_gap(closure: TransferGapClosure, lines: list[Any]) -> None:
    """The bucket is the source of truth for what is still owed.

    If a rebuild, a correction or a racing closure moved it since the draft was
    made, the numbers on this document are stale and it must be made again.
    """
    held = dict(
        InTransitStock.objects.filter(transfer_doc_number=closure.transfer.doc_number).values_list(
            "sku_code", "qty"
        )
    )
    for line in lines:
        if held.get(line.sku_code, 0) != line.qty:
            raise OutboundPostingError(
                f"{line.sku_code} no longer has {line.qty} piece(s) in transit "
                f"(the bucket holds {held.get(line.sku_code, 0)}). "
                "The gap moved since this closure was drafted — correct it, "
                "which re-reads what is still owed."
            )


@transaction.atomic
def post_gap_closure(closure: TransferGapClosure, user: Any = None) -> list[StockLedgerEntry]:
    """Post a gap closure: drain the in-transit remainder, per the stated reason.

    Refuses unless a second, senior person has approved it (``require_approved``)
    and neither of them is entitled to the receiving store. Then the pieces leave
    the bucket and land where the reason says — at the destination, back at the
    sender, or off the books as a loss (Dr SUSPENSE / Cr INVENTORY, the same
    shape as a write-off, because that is what a lost carton is).
    """
    from outbound.models import GapReason

    lines = _lock_and_read_gap_lines(closure)

    approval = require_approved(closure)  # maker ≠ checker, senior-gated (#70)
    _refuse_self_closure(
        closure, closure.created_by, approval.decided_by if approval else None, user
    )

    _refuse_stale_gap(closure, lines)

    _ensure_gap_series(closure)
    closure.post()

    transfer = closure.transfer
    transfer_lines = {line.sku_code: line for line in transfer.lines.all()}
    entries: list[StockLedgerEntry] = []
    #: Value of the lost pieces, split by whose stock it was.
    loss = _ValueByOwner()

    if closure.reason == GapReason.LOST_IN_TRANSIT:
        # Write-off is where ownership decides everything, so it is settled
        # before a single leg is written: KDPS's own loss hits INVENTORY, the
        # brand's does not (it was never on our books), and a piece the masters
        # cannot place must not be guessed either way.
        unknown = sorted({line.sku_code for line in lines if brand_is_owned(line.brand) is None})
        if unknown:
            raise OutboundPostingError(
                f"The books cannot say who owns {', '.join(unknown)} "
                f"(brand not in the masters), so a loss cannot be booked against it. "
                "Add the brand, then correct this closure."
            )

    for i, line in enumerate(lines, start=1):
        # OUT of the in-transit bucket, under the TRANSFER's number (see above)
        entries.append(
            _write_transit_entry(
                transfer=transfer,
                line=line,
                qty=-line.qty,
                kind="transit_out",
                line_no=LINE_NO_GAP_DRAIN + i,
                posted_by=user,
            )
        )
        # …and the transfer line stops claiming those pieces are still on the road.
        transfer_line = transfer_lines[line.sku_code]
        transfer_line.qty_resolved += line.qty
        transfer_line.save(update_fields=["qty_resolved"])
        if closure.reason == GapReason.LOST_IN_TRANSIT:
            # Nowhere to land — the value comes off the books instead.
            loss.add(brand_is_owned(line.brand), line.qty * line.unit_cost_paise)
            continue
        landing = (
            transfer.destination_store
            if closure.reason == GapReason.FOUND_LATER
            else transfer.source_store
        )
        entries.append(
            _write_stock_entry(
                store=landing,
                gstin=landing.gstin,
                line=line,
                qty=line.qty,
                kind="transfer_in",
                doc_number=cast(str, closure.doc_number),
                line_no=i,
                posted_by=user,
            )
        )

    ref = PostingRef(
        doc_type="GAP",
        doc_number=cast(str, closure.doc_number),
        store=transfer.source_store,
        gstin=transfer.source_store.gstin,
        posted_by=user,
    )
    if loss.own:
        # Our own stock, gone: a loss, the same shape as a write-off.
        _post_value_pair(
            ref,
            GLAccount.SUSPENSE,
            GLAccount.INVENTORY,
            loss.own,
            "Gap closure: lost in transit",
            "Gap closure: stock written off",
            user,
        )
    if loss.sor:
        # The brand's stock, gone. Its value was never inside INVENTORY, so the
        # only thing to undo is the SOR memo pair. Whether KDPS owes the brand
        # for a carton it lost is a settlement claim, not a stock posting — it
        # belongs to the payments design, and inventing a payable here would be
        # exactly the model-blind liability the 30 June review found.
        _post_value_pair(
            ref,
            GLAccount.SOR_CONTRA,
            GLAccount.SOR_STOCK,
            loss.sor,
            "Gap closure: reverse SOR contra",
            "Gap closure: brand stock lost",
            user,
        )

    return entries


# ---------------------------------------------------------------------------
# 1b. Mark damaged — global action; a confirmed one moves a piece to quarantine
# ---------------------------------------------------------------------------
#
# A mark-damaged document posts *once confirmed* (#138), under its own DMG
# voucher number, two legs at the store: damage_out (−qty, drops free-to-sell in
# StockOnHand) + quarantine_in (+qty, into the QuarantineStock bucket). The piece
# never leaves the store and stays owned — it is just no longer sellable. No GL:
# the two legs net to zero, so total inventory value is unchanged (the same shape
# as the in-transit pair).

# line_no bands — one DMG doc number carries both leg types.
LINE_NO_DAMAGE_OUT = 0
LINE_NO_QUARANTINE_IN = 500


def _check_quarantine(store_id: int, sku_code: str, required_qty: int) -> None:
    """Block if the quarantine bucket holds too few of these pieces (#75).

    The ``_check_stock`` of the other bucket, and there for the same reason: a
    return that drains more than the bucket holds would push a projection
    negative and send the brand pieces the books say were never quarantined.
    Row-locked, so two returns of the same damaged pieces serialize.
    """
    try:
        bucket = QuarantineStock.objects.select_for_update().get(
            store_id=store_id, sku_code=sku_code
        )
        available = bucket.qty
    except QuarantineStock.DoesNotExist:
        available = 0
    if available < required_qty:
        raise OutboundPostingError(
            f"Quarantine at store {store_id} holds {available} of {sku_code}, "
            f"and this return needs {required_qty}. Only confirmed-damaged pieces "
            "go back as defective."
        )


def _write_quarantine_entry(
    *,
    store: Store,
    gstin: Gstin,
    line: Any,
    qty: int,
    doc_number: str,
    line_no: int,
    kind: str = StockLedgerEntry.Kind.QUARANTINE_IN,
    posted_by: Any = None,
) -> StockLedgerEntry:
    """One quarantine ledger leg + the QuarantineStock projection update.

    Deliberately does NOT touch StockOnHand — the quarantine bucket lives in
    QuarantineStock, keyed by (store × barcode). ``marked_by``/``marked_at`` on
    the bucket record the most-recent mark-damaged actor + time for the
    inventory quarantine filter, so they are stamped only on the way *in*: a
    return draining the bucket (#75) must not rewrite who found the damage.
    """
    amount_paise = _line_amount(line, qty, doc_number)
    entry = StockLedgerEntry.objects.create(
        store=store,
        gstin=gstin,
        sku_code=line.sku_code,
        **merch_dims(line),
        qty=qty,
        amount=amount_paise,
        kind=kind,
        doc_number=doc_number,
        line_no=line_no,
        posted_by=posted_by,
    )
    QuarantineStock.objects.get_or_create(
        store=store,
        sku_code=line.sku_code,
        defaults={
            "gstin": gstin,
            **merch_dims(line),
            "qty": 0,
            "value_paise": 0,
        },
    )
    # Re-read under a row lock before the read-modify-write. Both writers of this
    # bucket have to serialize on *this* row: mark-damaged locks the shelf and a
    # return locks the bucket, so without this a confirm and a return of the same
    # barcode take different locks, never conflict, and one of the two updates is
    # lost — leaving the projection saying something the ledger does not.
    bucket = QuarantineStock.objects.select_for_update().get(store=store, sku_code=line.sku_code)
    bucket.qty += qty
    bucket.value_paise += amount_paise
    if qty > 0:
        bucket.marked_by = posted_by
        bucket.marked_at = entry.created_at
    if bucket.qty == 0:
        # A fully-cleared quarantine (the whole bucket returned to the brand)
        # leaves no row — matching the rebuild command, which emits no zero-qty
        # row. Its value goes with it, which is the point: the pieces are gone.
        bucket.delete()
    else:
        bucket.save()
    return entry


@transaction.atomic
def mark_damaged(
    store: Store, scans: dict[str, int], user: Any = None, note: str = ""
) -> MarkDamaged:
    """The global mark-damaged action, end to end (Rule 1 — every event is a
    document): create a DMG document and enrich each scanned piece from the
    store's stock (dims + frozen cost, never typed).

    Whether it *posts* depends on who is doing it (#138). A store person is
    reporting damage, so the document stays a draft in the approvals inbox and
    the piece stays on the shelf; a warehouse or HO person holds the confirming
    rung, so their own document clears itself and posts here and now. Either
    way it is one transaction, so a bad scan rolls the whole thing back and
    leaves no orphan draft.
    """
    from outbound.models import MarkDamaged, MarkDamagedLine

    if not scans:
        raise OutboundPostingError("Mark-damaged needs scanned pieces.")

    mark = MarkDamaged.objects.create(store=store, note=note, created_by=user)
    for barcode, qty in sorted(scans.items()):
        identity = _resolve_scan_identity(store.id, barcode)
        MarkDamagedLine.objects.create(mark=mark, sku_code=barcode, qty=qty, **identity)

    approval = request_document_approval(mark, requested_by=user)
    if approval is not None and approval.status in CLEARED_STATUSES:
        post_mark_damaged(mark, user=user)
    return mark


def confirm_mark_damaged(mark: MarkDamaged, *, actor: User | None) -> None:
    """The warehouse's confirmation, which is what posts a damage flag (#138).

    Registered with the approvals inbox in ``outbound.apps`` and called from
    inside the decision's transaction, so the stock movement and the decision
    commit together — or roll back together, which is what happens when the
    flagged piece was sold while it waited.
    """
    from approvals.services import ApprovalError

    try:
        post_mark_damaged(mark, user=actor)
    except OutboundPostingError as exc:
        # Re-raised in the inbox's own language so the decide endpoint answers
        # 400 with the reason, rather than 500 with a traceback.
        raise ApprovalError(str(exc)) from exc


@transaction.atomic
def post_mark_damaged(mark: MarkDamaged, user: Any = None) -> list[StockLedgerEntry]:
    """Post a mark-damaged document: move each line's pieces from free-to-sell
    into quarantine at the store.

    Refuses unless the flag has been confirmed by someone who holds the rung
    (``require_approved``) — the ledger seam behind #138, so no caller, API or
    shell, can turn a store's report into a stock movement.

    Blocks (Rule 5's dangerous-exception) if the store has too little sellable
    stock of a piece — you cannot quarantine stock that isn't there, which is
    also what happens when a flagged piece is sold while it waits. Stock move
    only, no GL.
    """
    lines = list(mark.lines.all())
    if not lines:
        raise OutboundPostingError("Mark-damaged has no lines.")

    # Also what stamps ``confirmed_by`` — on every route in, not just this one.
    require_approved(mark)  # a store report is not a stock movement (#138)

    # Row-lock + validate sellable stock at the store before minting a number.
    for line in lines:
        _check_stock(mark.store_id, line.sku_code, line.qty)

    mark.post()

    entries: list[StockLedgerEntry] = []
    for i, line in enumerate(lines, start=1):
        # Out of free-to-sell at the store…
        entries.append(
            _write_stock_entry(
                store=mark.store,
                gstin=mark.store.gstin,
                line=line,
                qty=-line.qty,
                kind=StockLedgerEntry.Kind.DAMAGE_OUT,
                doc_number=cast(str, mark.doc_number),
                line_no=LINE_NO_DAMAGE_OUT + i,
                posted_by=user,
            )
        )
        # …and into quarantine at the same store
        entries.append(
            _write_quarantine_entry(
                store=mark.store,
                gstin=mark.store.gstin,
                line=line,
                qty=line.qty,
                doc_number=cast(str, mark.doc_number),
                line_no=LINE_NO_QUARANTINE_IN + i,
                posted_by=user,
            )
        )

    return entries


# ---------------------------------------------------------------------------
# 2. Return to Vendor (RTV) — stock out + conditional GL
# ---------------------------------------------------------------------------
#
# A return drains whichever bucket its lines came out of (#75). A quarantine
# line writes ``quarantine_out`` against QuarantineStock — the pieces already
# left free-to-sell when the damage was confirmed, so taking them off the shelf
# a second time would destroy stock that isn't there. A season-end line is
# ordinary free-to-sell stock leaving, ``seasonal_ret`` against StockOnHand. One
# RTV doc number carries both, so each gets its own line_no band.
#
# Which retires ``rtv_out``: it was the kind a defective return used to write
# against free-to-sell stock, and a defective return now drains quarantine
# instead. The bucket's rebuild reads ``quarantine_in``/``quarantine_out`` and
# nothing else (``rebuild_stock_on_hand``), so a return that drained it under any
# other name would rebuild to the wrong position. The kind stays in the choices
# for the entries already posted under it.
LINE_NO_RETURN_OUT = 0
LINE_NO_QUARANTINE_RETURN = 500


def _consolidating_transfer(route: str, via_transfer: Any, store: Any) -> Any:
    """Check the movement a consolidated return claims already happened (#75).

    "Via warehouse" is not a shipping preference, it is an assertion that these
    pieces were transferred here first — and an assertion with nothing behind it
    is how stock goes missing between two documents. So the transfer must exist,
    must have actually posted, and must have ended *here*. The other two routes
    carry no transfer at all, and quietly keeping one somebody sent would leave
    a return pointing at a movement it has nothing to do with.
    """
    from core.documents import DocStatus
    from outbound.models import LogisticsRoute

    if route != LogisticsRoute.WAREHOUSE_CONSOLIDATION:
        return None
    if via_transfer is None:
        raise OutboundPostingError(
            "A consolidated return has to name the store-to-warehouse transfer "
            "that brought the pieces here."
        )
    if via_transfer.destination_store_id != store.id:
        raise OutboundPostingError(
            f"{via_transfer.doc_number or 'That transfer'} did not arrive at "
            f"{store.code}, so this return cannot be the consolidation of it."
        )
    if via_transfer.docstatus != DocStatus.SUBMITTED:
        raise OutboundPostingError(
            f"{via_transfer.doc_number or 'That transfer'} has not been dispatched, "
            "so the pieces are not here to consolidate."
        )
    return via_transfer


@transaction.atomic
def raise_return_to_brand(
    *,
    store: Any,
    vendor: Any,
    brand: Any,
    return_type: str,
    route: str,
    scans: dict[str, int],
    user: User | None = None,
    via_transfer: Any = None,
    notes: str = "",
) -> ReturnToVendor:
    """Build a return to brand from scans, against the brand's returnable pool.

    The whole document is server-built (Rule 6): the client sends barcodes and
    quantities, and every other field on every line — dims, source bucket, unit
    cost — is read off the pool row the server matched the scan to. Nothing that
    decides money comes from the payload.

    Born with its answer to the approval question, like every other wired
    family, and born with the allowance snapshot the approver will be shown.
    One transaction, so a scan the pool refuses leaves no half-built draft.
    """
    from outbound.models import ReturnToVendor, ReturnToVendorLine
    from outbound.returnable import NotReturnable, allocate, cap_for, pool_for, returnable_pool

    if not brand.takes_returns:
        raise OutboundPostingError(
            f"{brand.name} is {brand.commercial_label} — KDPS bought this stock "
            "outright, so none of it goes back to the brand."
        )

    via_transfer = _consolidating_transfer(route, via_transfer, store)

    try:
        pool = pool_for(
            returnable_pool(user, brand, store_id=store.id, acting=True), return_type, store.id
        )
        allocations = allocate(scans, pool)
    except NotReturnable as exc:
        raise OutboundPostingError(str(exc)) from exc

    this_return_paise = sum(a.value_paise for a in allocations)
    cap = cap_for(brand)
    # The earliest deadline on the return: a carton goes back as one carton, so
    # the date that matters is the first one to expire, not the last.
    deadlines = [a.row.window_date for a in allocations if a.row.window_date]
    seasons = {a.row.dims.get("season", "") for a in allocations}

    rtv = ReturnToVendor.objects.create(
        store=store,
        vendor=vendor,
        brand=brand,
        return_type=return_type,
        logistics_route=route,
        via_transfer=via_transfer,
        season=seasons.pop() if len(seasons) == 1 else "",
        return_window_date=min(deadlines) if deadlines else None,
        cap_percent=cap.percent,
        cap_allowance_paise=cap.allowance_paise,
        cap_used_before_paise=cap.used_paise,
        cap_exceeded_by_paise=cap.exceeded_by(this_return_paise),
        notes=notes,
        created_by=user,
    )
    for allocation in allocations:
        ReturnToVendorLine.objects.create(rtv=rtv, **allocation.as_line_kwargs())

    request_document_approval(rtv, requested_by=user)
    return rtv


@transaction.atomic
def post_rtv(rtv: ReturnToVendor, user: Any = None) -> list[StockLedgerEntry]:
    """Post an RTV: stock exits its bucket, GL posts for owned stock only.

    - Owned (Outright/Correction): Dr VENDOR_PAYABLE / Cr INVENTORY
    - Brand-owned (SOR/Consignment): stock out only, no GL

    Refuses unless a second person has cleared it (``require_approved``) — the
    ledger seam behind the return's approval, so no caller, API or shell, can
    send a brand's stock back on one person's say-so (#75).
    """
    from masters.models import Brand
    from outbound.models import ReturnSource

    lines = list(rtv.lines.all())
    if not lines:
        raise OutboundPostingError("RTV has no lines.")

    # Also what stamps ``approved_by`` — on every route in, not just the API.
    require_approved(rtv)

    for line in lines:
        if line.source == ReturnSource.QUARANTINE:
            _check_quarantine(rtv.store_id, line.sku_code, line.qty)
        else:
            _check_stock(rtv.store_id, line.sku_code, line.qty)

    # Block RTVs on V-flipped stock: ownership has transferred to KDPS,
    # so returning it to the brand is no longer valid.
    vflipped_skus = list(
        StockOnHand.objects.filter(
            store_id=rtv.store_id,
            sku_code__in=[line.sku_code for line in lines],
            brand__startswith="V ",
        ).values_list("sku_code", flat=True)
    )
    if vflipped_skus:
        raise OutboundPostingError(
            f"Cannot RTV V-flipped stock (ownership transferred to KDPS): "
            f"{', '.join(vflipped_skus)}"
        )

    rtv.post()

    entries = []
    total_value_paise = 0

    for i, line in enumerate(lines, start=1):
        if line.source == ReturnSource.QUARANTINE:
            entry = _write_quarantine_entry(
                store=rtv.store,
                gstin=rtv.store.gstin,
                line=line,
                qty=-line.qty,
                kind=StockLedgerEntry.Kind.QUARANTINE_OUT,
                doc_number=cast(str, rtv.doc_number),
                line_no=LINE_NO_QUARANTINE_RETURN + i,
                posted_by=user,
            )
        else:
            entry = _write_stock_entry(
                store=rtv.store,
                gstin=rtv.store.gstin,
                line=line,
                qty=-line.qty,
                kind=StockLedgerEntry.Kind.SEASONAL_RET,
                doc_number=cast(str, rtv.doc_number),
                line_no=LINE_NO_RETURN_OUT + i,
                posted_by=user,
            )
        entries.append(entry)
        total_value_paise += line.qty * line.unit_cost_paise

    # GL posting: only for KDPS-owned stock (Outright / Correction)
    is_owned = False
    if rtv.brand:
        is_owned = rtv.brand.ownership == Brand.Ownership.OWNED

    # NOTE (#75, for a human ruling): brand-owned stock going home leaves the
    # SOR memo pair written at inward standing, and nothing else takes it down —
    # so SOR_STOCK overstates what KDPS holds for the brand by the value of every
    # return ever made. `post_gap_closure` reverses exactly this pair when
    # brand-owned stock is lost in transit, so the posting exists; whether a
    # *return* should make it is a money decision the design corpus has not taken
    # and four existing tests currently assert the opposite. Left alone here on
    # purpose rather than changed under a slice that was not asked to change it.
    if is_owned and total_value_paise > 0:
        doc_ref = PostingRef(
            doc_type="RTV",
            doc_number=cast(str, rtv.doc_number),
            store=rtv.store,
            gstin=rtv.store.gstin,
            posted_by=user,
        )
        vendor_code = str(rtv.vendor_id) if rtv.vendor_id else ""
        post_entries(
            doc_ref,
            [
                dr(
                    GLAccount.VENDOR_PAYABLE,
                    total_value_paise,
                    party_type="vendor",
                    party_code=vendor_code,
                    memo=f"RTV {rtv.return_type}: reduces payable",
                ),
                cr(
                    GLAccount.INVENTORY,
                    total_value_paise,
                    memo=f"RTV {rtv.return_type}: stock returned",
                ),
            ],
            posted_by=user,
        )

        # Vendor subledger mirror — reduces what we owe (negative amount).
        # GL is already posted above via post_entries, so gl=False to avoid
        # double-booking the payable (same pattern as post_pt_vendor_bill).
        from finledger.posting import post_vendor_bill

        post_vendor_bill(
            rtv.vendor,
            -total_value_paise,
            f"RTV {rtv.return_type}: credit for {rtv.doc_number}",
            user,
            reference=cast(str, rtv.doc_number),
            gl=False,
        )

    return entries


# ---------------------------------------------------------------------------
# 3. Stock Adjustment — count vs book
# ---------------------------------------------------------------------------


@transaction.atomic
def post_adjustment(adj: StockAdjustment, user: Any = None) -> list[StockLedgerEntry]:
    """Post a stock adjustment: +/− qty at frozen cost.

    GL: Dr/Cr INVENTORY vs Cr/Dr SUSPENSE.
    Reductions are blocked if insufficient stock.
    """
    lines = list(adj.lines.all())
    if not lines:
        raise OutboundPostingError("Adjustment has no lines.")

    require_approved(adj)  # maker ≠ checker (#70)

    # Block if any line reduces stock below zero
    for line in lines:
        if line.adj_qty < 0:
            _check_stock(adj.store_id, line.sku_code, abs(line.adj_qty))

    adj.post()

    entries = []
    total_debit_paise = 0  # net: + means inventory increases, - means decreases

    for i, line in enumerate(lines, start=1):
        if line.adj_qty == 0:
            continue
        entry = _write_stock_entry(
            store=adj.store,
            gstin=adj.store.gstin,
            line=line,
            qty=line.adj_qty,
            kind="adjustment",
            doc_number=cast(str, adj.doc_number),
            line_no=i,
            posted_by=user,
        )
        entries.append(entry)
        total_debit_paise += line.adj_qty * line.unit_cost_paise

    # GL posting: balanced voucher
    if total_debit_paise != 0:
        doc_ref = PostingRef(
            doc_type="ADJ",
            doc_number=cast(str, adj.doc_number),
            store=adj.store,
            gstin=adj.store.gstin,
            posted_by=user,
        )
        if total_debit_paise > 0:
            # Surplus: Dr INVENTORY / Cr SUSPENSE
            legs = [
                dr(GLAccount.INVENTORY, total_debit_paise, memo="Adjustment: surplus"),
                cr(GLAccount.SUSPENSE, total_debit_paise, memo="Adjustment: surplus contra"),
            ]
        else:
            # Shrinkage: Dr SUSPENSE / Cr INVENTORY
            legs = [
                dr(GLAccount.SUSPENSE, abs(total_debit_paise), memo="Adjustment: shrinkage"),
                cr(
                    GLAccount.INVENTORY, abs(total_debit_paise), memo="Adjustment: shrinkage contra"
                ),
            ]
        post_entries(doc_ref, legs, posted_by=user)

    return entries


# ---------------------------------------------------------------------------
# 4. Write-off — owner-approved stock exit
# ---------------------------------------------------------------------------


@transaction.atomic
def post_writeoff(wo: WriteOff, user: Any = None) -> list[StockLedgerEntry]:
    """Post a write-off: stock exits, loss booked.

    GL: Dr SUSPENSE (loss) / Cr INVENTORY.
    """
    lines = list(wo.lines.all())
    if not lines:
        raise OutboundPostingError("Write-off has no lines.")

    require_approved(wo)  # maker ≠ checker (#70)

    for line in lines:
        _check_stock(wo.store_id, line.sku_code, line.qty)

    wo.post()

    entries = []
    total_value_paise = 0

    for i, line in enumerate(lines, start=1):
        entry = _write_stock_entry(
            store=wo.store,
            gstin=wo.store.gstin,
            line=line,
            qty=-line.qty,
            kind="write_off",
            doc_number=cast(str, wo.doc_number),
            line_no=i,
            posted_by=user,
        )
        entries.append(entry)
        total_value_paise += line.qty * line.unit_cost_paise

    if total_value_paise > 0:
        doc_ref = PostingRef(
            doc_type="WRO",
            doc_number=cast(str, wo.doc_number),
            store=wo.store,
            gstin=wo.store.gstin,
            posted_by=user,
        )
        post_entries(
            doc_ref,
            [
                dr(GLAccount.SUSPENSE, total_value_paise, memo="Write-off: loss"),
                cr(GLAccount.INVENTORY, total_value_paise, memo="Write-off: stock exit"),
            ],
            posted_by=user,
        )

    return entries


# ---------------------------------------------------------------------------
# 4b. Stock request fulfilment — the ask becoming a transfer draft (#74)
# ---------------------------------------------------------------------------


@transaction.atomic
def fulfil_stock_request(
    stock_request: StockRequest, lines: list[dict[str, int]], user: Any = None
) -> StoreTransfer:
    """Turn an approved ask into a draft transfer, pre-filled from its lines.

    ``lines`` is ``[{"line_id": ..., "qty": ...}]`` — how much of *this* pass
    the fulfilling store is committing, which may be less than what remains
    requested (a partial fulfilment; another pass can follow later against the
    same request). The new transfer is a bare plan — sku + qty only, like any
    scan-to-build draft — because dims and cost come from the scan at
    dispatch, never invented here.

    Approving the request approves the ask, never the move: the transfer this
    creates is still only a draft, and asks the Operations Head separately,
    same as every other transfer (#137). Refuses unless the request itself has
    cleared its own approval — ``require_approved``, at the posting layer so a
    shell hits the same wall as the API.
    """
    from core.documents import DocStatus
    from outbound.models import (
        StockRequestStatus,
        StoreTransfer,
        StoreTransferLine,
        TransferReason,
        TransferType,
    )

    locked = type(stock_request).objects.select_for_update().get(pk=stock_request.pk)
    require_approved(locked)  # unapproved requests cannot be fulfilled

    if locked.status == StockRequestStatus.CLOSED:
        raise OutboundPostingError("This request is already closed.")
    if hasattr(locked, "closure"):
        raise OutboundPostingError("This request has been closed; no more can be fulfilled.")

    by_id = {line.id: line for line in locked.lines.all()}
    to_build: list[tuple[Any, int]] = []
    # Running total per line across *this* payload — two entries for the same
    # line_id in one call must share the same "left to fulfil" ceiling, or a
    # duplicated line_id would let one call over-commit past what remains.
    claimed_this_call: dict[int, int] = {}
    for item in lines:
        line = by_id.get(item["line_id"])
        if line is None:
            raise OutboundPostingError("That line is not on this request.")
        qty = item["qty"]
        already_this_call = claimed_this_call.get(line.id, 0)
        # ``qty_committed``, not ``qty_fulfilled`` — an earlier pass's transfer
        # may still be an unreceived draft, and its promise must count here or
        # a second pass made before the first is received could over-commit.
        remaining = line.qty - line.qty_committed - already_this_call
        if qty < 1 or qty > remaining:
            raise OutboundPostingError(
                f"{line.sku_code}: at most {remaining} left to fulfil on this line."
            )
        claimed_this_call[line.id] = already_this_call + qty
        to_build.append((line, qty))
    if not to_build:
        raise OutboundPostingError("Select at least one line to fulfil.")

    # Mint the request's own number on the first pass that commits to
    # fulfilling it — not at raise, and not at approval, because until now
    # nothing has actually happened yet that a Tally-facing number should mark.
    if locked.docstatus == DocStatus.DRAFT:
        locked.post()

    transfer_type = (
        TransferType.STORE_SPLIT
        if locked.fulfilling_store.store_type == locked.fulfilling_store.StoreType.WAREHOUSE
        else TransferType.INTER_STORE
    )
    transfer = StoreTransfer.objects.create(
        source_store=locked.fulfilling_store,
        destination_store=locked.requesting_store,
        transfer_type=transfer_type,
        reason=TransferReason.SISTER_STORE_REQUEST,
        fulfils_request=locked,
        created_by=user,
    )
    for line, qty in to_build:
        StoreTransferLine.objects.create(
            transfer=transfer, sku_code=line.sku_code, qty_planned=qty, request_line=line
        )
    request_document_approval(transfer, requested_by=user)
    return transfer


@transaction.atomic
def close_stock_request(
    stock_request: StockRequest, *, user: Any = None, note: str = ""
) -> StockRequestClosure:
    """The fulfilling store says no more is coming.

    The only way a partly-covered request stops reading as "being fulfilled"
    forever — see ``StockRequest.status``. Refuses on a request nothing has
    been approved for yet (there is nothing to stop being fulfilled) and on one
    already closed (one closure per request, like a transfer's one receipt).
    """
    from outbound.models import StockRequestClosure, StockRequestStatus

    locked = type(stock_request).objects.select_for_update().get(pk=stock_request.pk)
    require_approved(locked)
    if locked.status == StockRequestStatus.CLOSED:
        raise OutboundPostingError("This request is already closed.")
    if hasattr(locked, "closure"):
        raise OutboundPostingError("This request has already been closed.")
    return StockRequestClosure.objects.create(request=locked, closed_by=user, note=note)


# ---------------------------------------------------------------------------
# 5. V-flip — brand-owned → KDPS-owned (GL reclass, no physical move)
# ---------------------------------------------------------------------------


@transaction.atomic
def post_vflip(vflip: VFlip, user: Any = None) -> list[StockLedgerEntry]:
    """Post a V-flip: ownership changes, stock stays.

    GL: reverse SOR pair, book as owned.
      Dr SOR_CONTRA / Cr SOR_STOCK (remove memo)
      Dr INVENTORY / Cr SUSPENSE (now owned, settlement claim in Sprint 8)

    Stock: write -qty (old brand) and +qty (V-prefixed brand) entries
    to leave an audit trail of the ownership change.
    """
    lines = list(vflip.lines.all())
    if not lines:
        raise OutboundPostingError("V-flip has no lines.")

    require_approved(vflip)  # maker ≠ checker (#70)
    # Runs even when legacy/malformed lines total ₹0 and therefore produce no GL
    # legs. Ownership itself cannot move past the immutable people floor.
    assert_pt_or_vflip_actor(user)

    for line in lines:
        _check_stock(vflip.store_id, line.sku_code, line.qty)

    vflip.post()

    entries = []
    total_value_paise = 0

    for i, line in enumerate(lines, start=1):
        # Stock OUT from old brand identity
        _write_stock_entry(
            store=vflip.store,
            gstin=vflip.store.gstin,
            line=line,
            qty=-line.qty,
            kind="vflip_out",
            doc_number=cast(str, vflip.doc_number),
            line_no=i,
            posted_by=user,
        )
        # Stock IN as "V <brand>" (KDPS-owned)
        original_brand_name = line.brand or (
            vflip.original_brand.name if vflip.original_brand else "KDPS"
        )
        v_brand_name = f"V {original_brand_name}"

        class _VLine:
            sku_code: str
            design: str
            color: str
            size: str
            brand: str
            season: str
            item: str
            hsn: str
            unit_cost_paise: int

        vline = _VLine()
        vline.sku_code = line.sku_code
        vline.design = line.design
        vline.color = line.color
        vline.size = line.size
        vline.brand = v_brand_name
        vline.season = line.season
        vline.item = line.item
        vline.hsn = line.hsn
        vline.unit_cost_paise = line.unit_cost_paise

        entry_in = _write_stock_entry(
            store=vflip.store,
            gstin=vflip.store.gstin,
            line=vline,
            qty=line.qty,
            kind="vflip_in",
            doc_number=cast(str, vflip.doc_number),
            line_no=1000 + i,
            posted_by=user,
        )
        entries.append(entry_in)
        total_value_paise += line.qty * line.unit_cost_paise

    # GL: reverse SOR pair + book as owned
    if total_value_paise > 0:
        doc_ref = PostingRef(
            doc_type="VFL",
            doc_number=cast(str, vflip.doc_number),
            store=vflip.store,
            gstin=vflip.store.gstin,
            posted_by=user,
        )
        post_entries(
            doc_ref,
            [
                # Reverse the SOR memo pair
                dr(GLAccount.SOR_CONTRA, total_value_paise, memo="V-flip: reverse SOR contra"),
                cr(GLAccount.SOR_STOCK, total_value_paise, memo="V-flip: reverse SOR stock memo"),
                # Book as KDPS-owned
                dr(GLAccount.INVENTORY, total_value_paise, memo="V-flip: now KDPS-owned"),
                cr(
                    GLAccount.SUSPENSE,
                    total_value_paise,
                    memo="V-flip: settlement claim (Sprint 8)",
                ),
            ],
            posted_by=user,
        )

    return entries
