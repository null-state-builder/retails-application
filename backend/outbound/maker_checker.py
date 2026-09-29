"""Which outbound documents need a second person — and who that person may be.

This is the whole of outbound's knowledge about approvals: a small table of
document kinds (Rule 12 — variation is data, not code) plus two calls, one to
put a fresh draft in the inbox and one to refuse a post that hasn't cleared it.
The rules themselves (maker ≠ checker, reason on reject, one decision) live in
``approvals.services`` and are shared with every other module that wires in.

Wired: write-offs, V-flips, stock adjustments — the three that used to stamp
their own creator as approver — plus transfer gap closures (#71), damage
flags (#138) and store transfers (#137), which reach the inbox the same way as
everything else.

Damage is the one family where the second person is asked for on a *rung*
rather than on a value: a store person may report damage but not move the
stock, so their document waits, while a warehouse or HO person's own document
clears itself (``self_clearing``).

A document may also override the value question from its own side, by answering
``approval_is_mandatory``. One does: a stock correction a recount produced
(#78), which is never small enough to clear itself however the recount came out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import models

from approvals.models import Approval, ApprovalPolicy, ApprovalStatus
from approvals.services import (
    AlreadyPendingError,
    ApprovalRequired,
    approval_for,
    assert_approved,
    holds_approver_role,
    record_no_approval_needed,
    request_approval,
)
from core.documents import DocStatus
from outbound.costing import OutboundPostingError, book_unit_cost
from outbound.models import (
    MarkDamaged,
    ReturnToVendor,
    StockAdjustment,
    StockRequest,
    StoreTransfer,
    TransferGapClosure,
    VFlip,
    WriteOff,
)


@dataclass(frozen=True)
class ApprovalKind:
    """The stable wiring between a model and its stored approval policy."""

    code: str
    label: str
    #: The document's own approver column, stamped from the approval at post time.
    approver_field: str
    #: Which location the document — and so its inbox row and its store scope —
    #: hangs off. Every family but one calls it ``store``; a transfer has two
    #: ends and hangs off the *source*, because the sender is answerable for the
    #: pieces until the receiver scans them in (#137).
    store_field: str = "store"
    #: Which location's books price the document, where that is *not* the
    #: location its inbox row hangs off. Only a stock request needs the two to
    #: differ: the ask belongs to the store that raised it — that is whose
    #: manager clears step 1 of the route (#172) — while the only books that can
    #: say what the pieces are worth are the ones holding them. Blank means the
    #: two are the same location, which is every other family.
    pricing_store_field: str = ""
    #: Which brand the decision is about, where it is about one. Only the return
    #: families set it, and only they need to: the approver a return's policy
    #: names is the brand manager, whose scope is brands rather than stores, so
    #: without this snapshot the inbox would scope them out of the one thing they
    #: are there to decide (#75).
    brand_field: str = ""
    #: Whether this family asks for a second person on the **rung** instead of
    #: on the value: a maker who already holds the approving rung clears their
    #: own document, and everyone else waits however little is at stake. Who
    #: holds it stays one list, read from the live policy row (Rule 12), never
    #: frozen a second time here. The tolerance and band below do not apply.
    self_clearing: bool = False


KINDS: dict[type[models.Model], ApprovalKind] = {
    WriteOff: ApprovalKind("writeoff", "Write-off", "approved_by"),
    VFlip: ApprovalKind("vflip", "V-flip", "authorized_by"),
    StockAdjustment: ApprovalKind("adjustment", "Stock adjustment", "approved_by"),
    # No tolerance and no band: a gap closure decides what became of pieces that
    # went missing between two locations, and it is HO/warehouse work whatever it
    # is worth. The rule that the *receiving* store cannot be either party is
    # about entitlement rather than role, so it lives in
    # ``posting._refuse_self_closure`` — this table only says who is senior.
    TransferGapClosure: ApprovalKind("gap_closure", "Gap closure", "approved_by"),
    # Every transfer, whatever it is worth and whichever way it goes: the PRD
    # (#104) puts the Operations Head in front of all of them, because a
    # cross-state move is a taxable supply between two distinct persons and a
    # within-state one still empties a shelf on one person's say-so. The
    # seeded policy row says so with a zero tolerance; who approves and above
    # what value stay retunable in Setup (Rule 12) — this table only says the
    # family exists and which end of the move answers for it.
    StoreTransfer: ApprovalKind("transfer", "Transfer", "approved_by", store_field="source_store"),
    # The ask, not the move. The row hangs off the *requesting* store, because
    # D10 routes it through that store's own manager first and then the
    # Operations Head (#172) — the ask came off their counter and it is theirs
    # to stand behind. The holding store's manager is not a system step: the
    # design makes that conversation the Operations Head's human work.
    # It is still priced from the *fulfilling* store's books, since a request
    # line carries no cost of its own and only the location holding the pieces
    # can say what they are worth. The transfer this later pre-fills answers to
    # the Operations Head separately, on its own gate (#137). Zero tolerance:
    # whatever it is worth, another store's stock does not leave on the asker's
    # say-so alone (#74).
    StockRequest: ApprovalKind(
        "stock_request",
        "Stock request",
        "approved_by",
        store_field="requesting_store",
        pricing_store_field="fulfilling_store",
    ),
    # No tolerance: a flag is a report about a piece, and how much that piece is
    # worth has nothing to do with whether the store may take it off the shelf
    # on its own say-so. The rung does — hence ``self_clearing``, which lets a
    # warehouse or HO person flag and confirm in the one action.
    MarkDamaged: ApprovalKind(
        "damage",
        "Damage flag",
        "confirmed_by",
        self_clearing=True,
    ),
    # A return to brand is the one family whose approver is bounded by brands
    # rather than by stores: the PRD (#104) gives the brand manager sign-off on
    # their own brands up to a value and the owner above it, which is exactly
    # what the seeded policy row says. Hence ``brand_field`` — the decision is
    # about a brand, and the inbox has to be able to see that (#75).
    ReturnToVendor: ApprovalKind(
        "return_to_brand",
        "Return to brand",
        "approved_by",
        brand_field="brand",
    ),
}


def approver_field(doc_class: type[models.Model]) -> str:
    """Which column on ``doc_class`` names its approver."""
    kind = KINDS.get(doc_class)
    return kind.approver_field if kind else "approved_by"


def _line_qty(line: Any) -> int:
    """How many pieces this line puts at stake — magnitude, never sign.

    Three spellings, because the quantity a document is *sized* by is the one it
    knows at draft time: an adjustment carries a signed ``adj_qty``, a transfer
    carries its plan (``qty_planned``, NULL on a scan-to-build draft, where the
    pieces are not chosen until the scanner is in someone's hand), and everything
    else a plain ``qty``.
    """
    for field in ("adj_qty", "qty", "qty_planned"):
        if hasattr(line, field):
            qty: int = getattr(line, field) or 0
            return abs(qty)
    return 0


def _line_totals(doc: Any, store_id: int) -> tuple[int, int, int]:
    """(line count, pieces, value in paise) over the document's lines.

    Value is what is at stake, so the magnitude is what counts, and ``store_id``
    is the location whose books price it — the source for a transfer.

    The unit cost is the one **frozen onto the line** when the draft was made,
    which is the same number the posting engine will use — read it rather than
    deriving it again, or the books can move between drafting and approving and
    the approver is shown one figure while the engine posts another (#103).

    A line made outside the API — a shell, a management command, a draft left
    over from before that fix — carries no frozen cost, so the books are read
    for it instead. Either way it never comes from the payload: the value
    decides whether a second person is asked at all, so a maker who could type
    it could type their way out of being checked. A line neither route can
    price contributes nothing, leaving the total at 0 — read everywhere as
    "unknown", and unknown escalates.
    """
    lines = list(doc.lines.all())
    pieces = 0
    value = 0
    for line in lines:
        qty = _line_qty(line)
        pieces += qty
        frozen = getattr(line, "unit_cost_paise", 0) or 0
        value += qty * (
            frozen or book_unit_cost(store_id, line.sku_code, getattr(line, "season", ""))
        )
    return len(lines), pieces, value


def _inr(paise: int) -> str:
    """Whole rupees with Lakh/Crore grouping — ``200000`` → ``₹2,000``.

    Local on purpose: the shared ₹ formatter is K9's, and this is one frozen
    sentence on an audit row, not a display path. Thresholds are set in whole
    rupees, so the paise are dropped rather than rounded.
    """
    rupees = str(abs(paise) // 100)
    if len(rupees) > 3:
        head, tail = rupees[:-3], rupees[-3:]
        # Indian grouping: every two digits above the last three.
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        rupees = ",".join(filter(None, [head, *groups, tail]))
    return f"₹{rupees}"


def _policy(kind: ApprovalKind) -> ApprovalPolicy:
    """Read the live row; missing policy is a closed gate, never a code fallback."""
    try:
        return ApprovalPolicy.objects.get(kind=kind.code)
    except ApprovalPolicy.DoesNotExist as exc:
        raise ApprovalRequired(
            f"No approval policy is configured for {kind.label.lower()}."
        ) from exc


def request_document_approval(doc: Any, *, requested_by: Any) -> Approval | None:
    """Put a freshly created draft in the approvals inbox — or record that it is
    small enough not to need one.

    Called at draft-creation time so the document is *born* with its answer: a
    maker cannot forget to ask, and cannot post in the gap before asking. Also
    the way back from a rejection — the maker fixes the draft and asks again,
    and the rejected request stays on the document's record.
    """
    kind = KINDS.get(type(doc))
    if kind is None:
        return None

    store = getattr(doc, kind.store_field)
    pricing_store = getattr(doc, kind.pricing_store_field) if kind.pricing_store_field else store
    line_count, pieces, value = _line_totals(doc, pricing_store.id)
    title = f"{store.code} · {line_count} line{'' if line_count == 1 else 's'} · {pieces} pcs"
    # A document may add what the store code alone cannot say — a gap closure is
    # meaningless in the inbox without naming the transfer it closes. Optional,
    # so nothing else has to know this hook exists.
    if subject := getattr(doc, "approval_subject", ""):
        title = f"{subject} · {title}"
    policy = _policy(kind)
    # Against the same list the request would have gone to, so retuning the
    # policy row moves both halves together (Rule 12).
    holds_the_rung = kind.self_clearing and holds_approver_role(
        requested_by, policy.approver_roles_for(value)
    )
    # The brand *name*, because that is how every brand travels through the
    # scoping gates (a ledger row carries the printed name, not a key), and
    # ``approvals`` must not learn what a Brand is (ADR-0002).
    brand = getattr(doc, kind.brand_field, None) if kind.brand_field else None
    common = {
        "kind": kind.code,
        "kind_label": kind.label,
        "title": title,
        "brand": getattr(brand, "name", "") or "",
        # The document's creator, not whoever is asking this time — see
        # ``ask_again``. Falls back to the asker for a document made outside
        # the API (a shell or a management command), which has no creator.
        "made_by": doc.created_by or requested_by,
        "requested_by": requested_by,
        "store": store,
        "value_paise": value,
    }

    if kind.self_clearing:
        # A rung family answers on the rung alone, and never falls through to
        # the value question below. A tolerance is business data (Rule 12), so
        # if it could clear this too, someone retuning ₹0 to ₹500 on the policy
        # row would quietly hand every store person the posting rung the ruling
        # took away from them — the one thing this family exists to prevent.
        if holds_the_rung:
            return record_no_approval_needed(
                doc,
                reason=(
                    f"Raised by someone who may decide a {kind.label.lower()} themselves — "
                    "posted and logged, nobody else asked."
                ),
                **common,
            )
        return request_approval(doc, approver_roles=policy.approver_roles_for(value), **common)

    # A document may say the tolerance is not its to be cleared by. A stock
    # correction a recount produced does (#78): the line was big enough to make a
    # second person count it, and letting the *recounted* total decide would hand
    # that second person a way to post it alone — count it down to just under the
    # tolerance and no approver is ever asked. Optional, so nothing else has to
    # know this hook exists.
    #
    # Asked of the *class* and then read off the instance, never through a
    # defaulting ``getattr``: this is an approval gate, and a ``getattr`` default
    # would turn any error raised inside the property into a silent "no approver
    # needed" — the one direction a gate may not fail.
    if hasattr(type(doc), "approval_is_mandatory") and doc.approval_is_mandatory:
        return request_approval(doc, approver_roles=policy.approver_roles_for(value), **common)

    if not policy.needs_checker(value):
        return record_no_approval_needed(
            doc,
            reason=f"Within the {_inr(policy.tolerance)} tolerance for "
            f"{kind.label.lower()}s — posted and logged, no second person asked.",
            **common,
        )

    return request_approval(doc, approver_roles=policy.approver_roles_for(value), **common)


def ask_again(doc: Any, *, requested_by: Any) -> Approval:
    """Send a rejected draft back for approval, as ``requested_by``.

    Without this a rejection is a dead end: the kernel FSM has no
    draft → cancelled move, so a refused document could never post and could
    never be closed — the only route would be to abandon it and type the whole
    thing again, losing why it was refused.

    The *asker* is recorded as whoever pressed the button, but the document's
    maker is carried across unchanged, so re-asking can never move the author
    out of the way and let them approve their own document.
    """
    kind = KINDS.get(type(doc))
    if kind is None:
        raise OutboundPostingError("This document type does not need approval.")
    if doc.docstatus != DocStatus.DRAFT:
        raise OutboundPostingError("Only a draft can be sent for approval.")

    live = approval_for(doc)
    if live is None:
        raise OutboundPostingError("This document has no approval request.")
    if live.status == ApprovalStatus.PENDING:
        raise OutboundPostingError("This document is already waiting for approval.")
    if live.status != ApprovalStatus.REJECTED:
        raise OutboundPostingError("This document has already cleared approval.")

    try:
        approval = request_document_approval(doc, requested_by=requested_by)
    except AlreadyPendingError as exc:  # lost a race with another asker
        raise OutboundPostingError(str(exc)) from exc
    assert approval is not None  # kind is wired, checked above
    return approval


def require_approved(doc: Any) -> Approval | None:
    """Refuse to post a wired document that no second person has approved, and
    stamp the approver onto the document itself.

    Called from the *posting* layer, so every caller — API, shell, management
    command — hits the same wall, and the stamp happens exactly once, on the
    one path that matters. A rejected document never reaches here, so it can
    never be marked as approved by the person who refused it.

    Unwired document types pass through untouched.
    """
    kind = KINDS.get(type(doc))
    if kind is None:
        return None
    try:
        approval = assert_approved(doc)
    except ApprovalRequired as exc:
        raise OutboundPostingError(str(exc)) from exc

    approver = approval.decided_by
    if approver is None and kind.self_clearing and approval.status == ApprovalStatus.NOT_REQUIRED:
        # Nobody else was asked because the asker already held the deciding
        # rung, so the inbox has no decider to read — they are it (#138).
        approver = approval.requested_by

    # The document is still a draft here (posting hasn't flipped it), so its own
    # approver column is writable — and after this it is frozen with the rest.
    if getattr(doc, f"{kind.approver_field}_id", None) != getattr(approver, "pk", None):
        setattr(doc, kind.approver_field, approver)
        doc.save(update_fields=[kind.approver_field])
    return approval
