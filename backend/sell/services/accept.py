"""The server accept pipeline - the one writer of a Sale (api-contract steps 1-13).

A bill reaches this function already printed and already in a customer's hand.
That single fact decides the shape of everything below:

* **Idempotent, always.** The till replays from a durable queue, so the same
  `idempotency_uuid` arriving twice must produce one bill and the same answer,
  with nothing written the second time. Two arriving *at once* is the same
  requirement with a lock in it: the loser blocks on the unique index, wakes to an
  `IntegrityError`, and reads back what the winner wrote (the Shopify pattern,
  grill Q5).
* **Flag, never block.** Things can be wrong with a bill that has already
  happened - a gap in the numbering, a return
  against a bill from the paper era, tax that disagrees with the dated slab - and
  none of them is a reason to refuse a sale the store already made. They become
  `ContinuityFlag` rows on the store's queue and the bill lands (Rule 8).
* **Refuse only what a human must actually resolve.** The six contract error
  codes are all of that shape: the payload contradicts itself, the number belongs
  to somebody else, a discount nobody authorised. The till halts its queue and
  shows an exception card; it never silently drops a bill.

The value side of a bill - the two balanced GL events, the vendor accrual and the
cash-ledger collections - lives in `sell.services.postings` (#178). This module
decides *what happened*; that one decides what it is worth and where it posts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.documents import DocStatus
from masters.models import Customer, Sku, Store
from masters.store_feature_registry import ALTERATIONS
from masters.store_features import is_feature_on, is_real_store
from masters.tax_settings import LEGACY_VERSION
from offers.models import Offer
from offers.resolution import Resolution
from sell.alteration_models import (
    ALTERATION_CODE,
    ALTERATION_DESCRIPTION,
    ALTERATION_GST_RATE,
    ALTERATION_SAC,
)
from sell.gift_voucher_models import GiftVoucher
from sell.gstin import describe as gstin_describe
from sell.gstin import normalise as gstin_normalise
from sell.gstin import state_code as gstin_state_code
from sell.models import (
    ContinuityFlag,
    DeferredCosting,
    IrnQueueItem,
    QuarantinedUpload,
    Sale,
    SaleLine,
    SaleLineShare,
    SaleTender,
    SellPolicy,
)
from sell.pricing import base_from_inclusive, split_inclusive
from sell.services import after_discount_check, exchange_tax, gift_vouchers
from sell.services.customers import normalise_mobile, upsert_customer
from sell.services.discount_funding import record_funding
from sell.services.gift_stock import Candidate as GiftCandidate
from sell.services.gift_stock import tag_gifts
from sell.services.goods_sale import Allocation as GoodsAllocation
from sell.services.goods_sale import (
    GoodsSaleError,
    ReturnLeg,
    plan_sale,
    post_goods_sale,
    returned_allocations,
)
from sell.services.goods_sale import Plan as GoodsPlan
from sell.services.goods_stock import Piece as GoodsPiece
from sell.services.goods_stock import is_goods_site
from sell.services.invoice_numbers import record_invoice_number, resolve_invoice_number
from sell.services.margin_share import record_margin_share
from sell.services.movements import post_stock_move
from sell.services.no_bill_caps import record_caps as record_no_bill_caps
from sell.services.overrides import can_sign, pin_rules_on, record_override
from sell.services.postings import (
    CostedLine,
    CostPlan,
    plan_from_original,
    post_sale_value,
    resolve_cost_plan,
)
from sell.services.recompute import (
    BillLine,
    credit_from_cited_rule,
    gst_offenders,
    offer_offenders,
    resolve_bill,
    rule_missing_lines,
)
from sell.services.refunds import entitled_refund, returned_so_far
from sell.services.reservations import record_pickup, release_for_bill
from sell.services.resolve import (
    ResolvedPiece,
    line_dims,
    manager_for_override,
    resolve_piece,
)
from sell.services.salespeople import Seller, SellerBook
from sell.services.saved_sizes import learn_from_bill
from sell.services.special_orders import check_for_bill, record_collection
from sell.services.split_shares import (
    PlannedShare,
    audit_shares,
    split_on,
    split_problem,
    split_returned,
    split_value,
)
from sell.services.tax_rulebook import RULE_UNKNOWN, StoreTaxBooks, TaxRulebook

logger = logging.getLogger(__name__)

#: The e-invoice clock: a B2B bill must carry an IRN within 30 days (grill Q8).
IRN_DUE_DAYS = 30

#: The two unique constraints that mean "this number is already somebody else's
#: bill" - the till's own key, and the rendered Tally join key the kernel mints
#: from it. Matched by name, so no other integrity failure can borrow the answer.
_BILL_NUMBER_CONSTRAINTS = frozenset({"uq_sale_store_fy_seq", "sell_sale_doc_number_key"})


class AcceptError(Exception):
    """A bill the server will not take, carrying the code the till routes on."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class AcceptResult:
    sale: Sale
    created: bool
    flags: list[str]


@dataclass
class _PreparedLine:
    """One payload line, resolved against the books and ready to be written."""

    payload: dict[str, Any]
    piece: ResolvedPiece
    original: SaleLine | None = None
    original_missing: bool = False
    seller: Seller | None = None
    override_needed: bool = False
    row: SaleLine | None = None
    #: How the piece describes itself on the bill (Rule 3), and the season it was
    #: placed in. Settled before the row is written, because the cost plan is
    #: decided from the brand snapshot and the row's `costing_status` from that.
    dims: dict[str, str] = field(default_factory=dict)
    season: str = ""
    cost: CostPlan = field(default_factory=CostPlan)
    #: At a goods-v1 store, the exact pieces this line consumes or gives back, and
    #: what each cost. Empty at a legacy store, where the cohort carries the cost.
    goods: list[GoodsAllocation] = field(default_factory=list)
    #: What the scan resolved to in the goods masters; `None` at a legacy store.
    goods_piece: GoodsPiece | None = None
    #: Ticket 08: each salesperson's share, where the line is split between two
    #: (a sold line), or follows a split line back (a return leg). Empty otherwise.
    shares: list[PlannedShare] = field(default_factory=list)
    #: Ticket 48: the part of a sold line's discount the server's own rulebook
    #: does not explain (step 6). `None` on a return leg.
    manual_disc_paise: int | None = None

    @property
    def is_return(self) -> bool:
        return bool(self.payload["direction"] == SaleLine.Direction.RETURN)

    @property
    def is_alteration(self) -> bool:
        return _is_alteration(self.payload)

    @property
    def value_paise(self) -> int:
        return int(self.payload["value_paise"])

    @property
    def qty(self) -> int:
        return int(self.payload["qty"])

    @property
    def unit_cost_paise(self) -> int:
        if self.original is not None:
            return self.original.unit_cost_paise
        if self.goods:
            # One number for a column that holds one. Where FIFO stayed inside a
            # single origin this is that origin's own cost; where it crossed two,
            # it is the average and `goods_cost_paise` is the exact money.
            pieces = sum(row.qty for row in self.goods)
            return sum(row.cost_paise for row in self.goods) // pieces if pieces else 0
        return self.piece.unit_cost_paise

    @property
    def goods_cost_paise(self) -> int | None:
        """The exact cost of this line's own pieces, or `None` at a legacy store.

        An average unit cost times a quantity is not what the goods cost; the
        allocations are. The cost event posts this, so a bill that crossed two
        origins posts neither a rounded-up nor a rounded-down paise.
        """
        return sum(row.cost_paise for row in self.goods) if self.goods else None

    @property
    def is_priced(self) -> bool:
        return self.unit_cost_paise > 0


# --- entry point -----------------------------------------------------------


def accept_sale(data: dict[str, Any], actor: Any) -> AcceptResult:
    """Take one bill, exactly once. See the module docstring for the shape."""
    # The replay response still reveals the bill's identifiers. Authorise the
    # requested store before looking up a previously accepted UUID, including
    # the concurrent replay branch below.
    requested_store = _resolve_store(data["store"], actor)
    uuid = data["idempotency_uuid"]
    existing = _find_by_uuid(uuid)
    if existing is not None:
        if existing.store_id != requested_store.pk:
            raise AcceptError("SCOPE_DENIED", "That bill is outside this counter.", 403)
        return _replay_or_refuse(existing, data)
    try:
        with transaction.atomic():
            return _accept_new(data, actor)
    except IntegrityError as exc:
        # Somebody got there first. If it was this same bill - a concurrent replay
        # from the till's queue - the honest answer is the answer they got, so we
        # read it back rather than reporting a clash. The re-read has to happen
        # out here: the transaction above is aborted and cannot be queried.
        existing = _find_by_uuid(uuid)
        if existing is not None:
            requested_store = _resolve_store(data["store"], actor)
            if existing.store_id != requested_store.pk:
                raise AcceptError("SCOPE_DENIED", "That bill is outside this counter.", 403) from None
            return _replay_or_refuse(existing, data)
        raise _translate_integrity(exc, data) from exc


def _find_by_uuid(uuid: Any) -> Sale | None:
    return Sale.objects.filter(idempotency_uuid=uuid).select_related("store").first()


def payload_fingerprint(data: dict[str, Any]) -> str:
    """A stable fingerprint of the bill as it arrived (PRD §10.5).

    Over the *validated* payload rather than the raw body, so a till that spells a
    timestamp two equivalent ways, or that sends a key the serializer drops, is
    still recognised as sending the same bill. `default=str` because the validated
    shape carries `Decimal`, `UUID` and `datetime`, all of which have one honest
    text form; `sort_keys` because a dictionary's order is not part of what a bill
    says.

    Derived keys the serializer adds for the pipeline's own convenience are left
    out: `all_lines` is `lines` plus `exchange.lines`, so including it would make
    the fingerprint depend on a shape the till never sent.
    """
    body = {key: value for key, value in data.items() if key != "all_lines"}
    # A till sends `tax_setting_version` since ticket 03; version 1 is what a bill
    # without it means, so the two spellings are one bill. Left out when it is 1,
    # so a bill accepted by a server that predates the field replays cleanly.
    if body.get("tax_setting_version") == LEGACY_VERSION:
        del body["tax_setting_version"]
    # A till sends `salesperson` since ticket 07; a bill from before names only
    # the old `salesman`, and its lines carry no salesperson. Left out when empty,
    # so a bill accepted by a server that predates the field replays cleanly.
    body = _without_empty_salesperson(body)
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _without_empty_salesperson(body: dict[str, Any]) -> dict[str, Any]:
    # Ticket 08 the same way: `shares` exists only on a split line, so a line
    # that carries none (or an empty list) fingerprints as it always did.
    def strip(lines: Any) -> Any:
        if not isinstance(lines, list):
            return lines
        return [
            {
                k: v
                for k, v in line.items()
                if not (k == "salesperson" and v is None) and not (k == "shares" and not v)
            }
            if isinstance(line, dict)
            else line
            for line in lines
        ]

    out = dict(body)
    if "lines" in out:
        out["lines"] = strip(out["lines"])
    exchange = out.get("exchange")
    if isinstance(exchange, dict) and "lines" in exchange:
        out["exchange"] = {**exchange, "lines": strip(exchange["lines"])}
    return out


def _replay_or_refuse(existing: Sale, data: dict[str, Any]) -> AcceptResult:
    """The same bill again is a replay; a *different* one under that key is not.

    PRD §10.5 and R-POS-008: "same identity, different payload is refused". Until
    OPS-09 the pipeline replayed the first bill either way - safe, in that no
    second bill and no second stock movement followed, but the till was never told
    its payload had been disagreed with, so a genuine divergence looked exactly
    like a successful retry.

    What arrives is kept (`QuarantinedUpload`) before the refusal goes back, so the
    argument is reviewable from both sides. The till halts its queue naming the
    bill and nothing is deleted to clear it.

    A bill accepted before this field existed carries no fingerprint, and an empty
    one means "we cannot tell" rather than "it differs": refusing every legacy
    retry would halt queues over bills that had synced perfectly.
    """
    if not existing.payload_fingerprint:
        return _replay(existing)
    if existing.payload_fingerprint == payload_fingerprint(data):
        return _replay(existing)
    QuarantinedUpload.objects.create(
        idempotency_uuid=data["idempotency_uuid"],
        store=existing.store,
        first_sale=existing,
        payload=json.loads(json.dumps(data, default=str)),
    )
    raise AcceptError(
        "IDEMPOTENCY_CONFLICT",
        f"A different bill has already been accepted under this key as "
        f"{existing.doc_number}. Both copies are kept for review; a person has to say "
        "which one is the sale.",
        409,
    )


def _replay(sale: Sale) -> AcceptResult:
    """The original answer, rebuilt from the bill itself.

    Rebuilt rather than remembered on purpose: a stored copy of the response is a
    second version of the truth that nothing keeps in step with the first. The
    bill and its flags *are* the answer, and reading them back cannot drift from
    what the row says.
    """
    return AcceptResult(sale=sale, created=False, flags=_flag_kinds(sale))


def _flag_kinds(sale: Sale) -> list[str]:
    return sorted({flag.kind for flag in sale.flags.all()})


def _translate_integrity(exc: IntegrityError, data: dict[str, Any]) -> AcceptError:
    """Name the constraint that fired, so the till knows whether to stop.

    Only the two that mean "this number is somebody else's" become a contract
    refusal; anything else is a defect and is re-raised as itself rather than
    dressed up as a business answer.
    """
    diagnostics = getattr(getattr(exc, "__cause__", None), "diag", None)
    # The constraint's own name, never the message text: an unrelated failure
    # that happens to mention a column would otherwise be dressed up as a
    # business answer, and the till would stop a queue over a server defect.
    name = getattr(diagnostics, "constraint_name", "") or ""
    if name in _BILL_NUMBER_CONSTRAINTS:
        return _bill_number_taken(data)
    raise exc


def _bill_number_taken(data: dict[str, Any]) -> AcceptError:
    return AcceptError(
        "BILL_NO_TAKEN",
        f"Bill {data['fy']}/{data['store']}/SAL/{data['till_seq']} already belongs to "
        "another sale. Two tills have been numbering the same series - a person has to "
        "sort this out before it can sync.",
        409,
    )


# --- the pipeline ----------------------------------------------------------


def _accept_new(data: dict[str, Any], actor: Any) -> AcceptResult:
    # Ask again, now that we are inside the transaction. The check in
    # `accept_sale` ran before it, and a concurrent copy of this same bill can
    # commit in between - at which point this is a replay that merely started
    # before the winner finished, and answering it as anything else would report
    # a bill's own twin as a second writer.
    replayed = _find_by_uuid(data["idempotency_uuid"])
    if replayed is not None:
        requested_store = _resolve_store(data["store"], actor)
        if replayed.store_id != requested_store.pk:
            raise AcceptError("SCOPE_DENIED", "That bill is outside this counter.", 403)
        return _replay(replayed)
    store = _resolve_store(data["store"], actor)  # step 1
    _check_alteration_lines(data, store)  # ticket 22
    # Which stock contract this store keeps is asked before the legacy fence, not
    # after it: a goods-v1 store's bill is not a legacy write that has to be
    # excused, it is a different posting entirely (OPS-07, PRD §8). The fence
    # stays exactly where it was for everything else, so a legacy write at a
    # goods-v1 site is still refused with `CONTRACT_DISABLED`.
    # Ticket 20: a bill collecting a customer reservation takes its pieces off
    # the hold first, inside this same transaction, so they are on the shelf the
    # plan below chooses from - and back under the hold if the bill is refused.
    pickup = release_for_bill(store, data, actor)
    # Ticket 21: a bill collecting a special order - checked and locked here, so
    # a refused bill leaves the order waiting.
    collection = check_for_bill(store, data, actor)
    # Ticket 19: the gift vouchers this bill spends - checked and locked here, so
    # a refused bill spends nothing and two bills cannot both spend one balance.
    voucher_uses = gift_vouchers.check_for_bill(store, data, actor)
    goods = _goods_plan(store, data)
    if goods is None:
        from stockledger.contracts import require_legacy_stock_writer

        require_legacy_stock_writer(store.pk, operation="Legacy sale acceptance")
    # Ticket 13: a bill whose returns were taken by the return tax rules is
    # checked for where its original came from before anything is read off it.
    return_tax = _return_tax(data)
    if return_tax and exchange_tax.held_by_gate(store):
        # The gate (CA sign-off) holds the rules off at a real store, and no till
        # there is ever told they are on (B1, B2): such a bill is not a till's.
        raise AcceptError(
            "FEATURE_OFF",
            "Exchange and return tax rules are waiting for CA sign-off and are not on at "
            f"{store.code}. This bill cannot have come from its till.",
            403,
        )
    if return_tax:
        refused = exchange_tax.refuse_elsewhere(store, data)
        if refused is not None:
            code, message = refused
            raise AcceptError(code, message, 400 if code == "VALIDATION" else 422)
    original_bill = _resolve_original_bill(data, store)  # step 8
    lines = _prepare_lines(data, store, original_bill, goods)  # steps 5, 8
    late_return = _mark_late_returns(lines, original_bill, data["billed_at"])
    _check_line_arithmetic(lines, return_tax=return_tax)  # step 3
    _check_totals(data, lines)  # step 3
    _check_cash_received(data)  # step 3, the drawer's half
    rulebook = _server_resolution(data, store, lines)  # steps 6 and 12 share it
    # Ticket 06: where the switch is on, only somebody the till could have held a
    # PIN for counts as a manager here (`manager_for_override`).
    pin_rules = pin_rules_on(store)
    override = manager_for_override(
        (data.get("override") or {}).get("user_id"), store, own_pin_rules=pin_rules
    )
    if late_return and override is None:
        raise AcceptError(
            "OVERRIDE_REQUIRED",
            "That bill is past the store's return window. A manager has to approve taking it back.",
            422,
        )
    # An unsolicited manager id is not evidence. The server derives whether an
    # exception exists; ordinary sales and in-window exchanges carry no override.
    if not late_return:
        override = None
    if pin_rules and override is not None and not can_sign(actor):
        raise AcceptError(
            "SCOPE_DENIED",
            "This login is not a person in the records, so the manager's approval on "
            "this bill could not be recorded.",
            403,
        )
    _check_discount_policy(lines, rulebook)  # step 6
    _guard_bill_number(data, store)  # step 4
    _check_till_number(data, store)  # step 4, the device's own series (R-POS-005)
    # Step 4, the new invoice series (ticket 04): checked before the bill is
    # written, because a posted bill cannot be changed. Never refused.
    invoice = resolve_invoice_number(data, store)
    on_job_cards = _open_job_card_returns(lines)  # ticket 22, read before writing

    sale = _write_sale(
        data, store, actor, original_bill, lines, override, invoice_number=invoice.number
    )  # step 9
    minted = sale.post()

    flags: list[str] = []
    flags += _flag_number_hole(sale, store, minted)  # step 4
    invoice_flag = record_invoice_number(sale, invoice)  # step 4, ticket 04
    if invoice_flag is not None:
        flags.append(_flag(sale, store, *invoice_flag))
    flags += _flag_missing_originals(sale, store, lines)  # step 8
    flags += _flag_job_card_returns(sale, store, on_job_cards)  # ticket 22
    flags += record_no_bill_caps(sale, store, actor)  # ticket 48
    flags += _flag_billed_while_paused(sale, store)  # step 4, change PRD §10.2
    flags += record_override(sale, store, actor, rules_on=pin_rules)  # ticket 06
    flags += _record_shares(sale, store, actor, lines)  # ticket 08
    record_funding(sale, store, actor, _goods_rows(lines), rulebook.credits)  # ticket 25
    record_margin_share(sale, store, actor, _goods_rows(lines))  # ticket 27
    _write_stock_legs(sale, store, lines, actor, goods)  # step 10, the stock half
    _record_deferred(store, lines)  # step 5
    _apply_tenders(  # step 7
        sale,
        data["tenders"],
        data.get("reservation"),
        data.get("special_order"),
        {use.voucher.number: use.voucher for use in voucher_uses},
    )
    record_pickup(sale, pickup, actor)  # ticket 20
    record_collection(sale, collection, actor)  # ticket 21
    gift_vouchers.record_use(sale, voucher_uses, actor)  # ticket 19
    _post_value(sale, lines, actor)  # step 10, the two value events
    flags += _apply_b2b(sale, store, data)  # step 11
    flags += _advisory_gst_check(sale, store, lines)  # step 12
    flags += _advisory_offer_check(sale, store, lines, rulebook)  # step 12
    flags += _advisory_after_discount_check(sale, store, lines, rulebook, data)  # ticket 11
    flags += _record_return_tax(sale, store, lines, original_bill, data, actor)  # ticket 13
    _tag_gifts(sale, store, actor, lines, rulebook)  # ticket 14
    transaction.on_commit(lambda: _upsert_customer(sale))  # step 6 (api-contract)
    # Ticket 18: after the customer row exists; never blocks the bill.
    transaction.on_commit(lambda: learn_from_bill(sale))
    return AcceptResult(sale=sale, created=True, flags=sorted(set(flags)))


def _tag_gifts(
    sale: Sale, store: Store, actor: Any, lines: list[_PreparedLine], rulebook: _Rulebook
) -> None:
    """Ticket 14: every sold piece the customer paid nothing for is a candidate
    gift; `tag_gifts` leaves out buy X get Y pieces and does nothing where off.

    Which rules priced the piece is the server's to say, never the till's: its
    own resolution, and the rule the till cites only where re-running that rule
    here finds it gives this line something (`credit_from_cited_rule`). A till
    naming a buy X get Y that does not apply cannot keep a gift from the tag.
    """
    by_line = rulebook.resolution.by_line()
    candidates = []
    for line in lines:
        if line.is_return or line.row is None or line.value_paise != 0:
            continue
        line_no = line.payload["line_no"]
        outcome = by_line.get(line_no)
        cited = line.payload.get("offer_id")
        applies = bool(cited) and (
            credit_from_cited_rule(
                cited,
                store.code,
                rulebook.day,
                list(rulebook.lines.values()),
                line_no,
                after_discount=rulebook.after_discount,
                tenant_id=store.tenant_id,
            )
            > 0
        )
        candidates.append(
            GiftCandidate(
                row=line.row,
                goods=line.goods,
                offer_ids=(outcome.offer_id if outcome else None, cited if applies else None),
            )
        )
    tag_gifts(sale, store, actor, candidates, rulebook.resolution.entitlements)


def _upsert_customer(sale: Sale) -> None:
    """Step 6 (api-contract) - fired only once the sale has actually committed.

    Never blocks: the bill is already printed and in the customer's hand, so a
    master-data hiccup here must not refuse it (Rule 5). No error code exists
    for this step by design - any failure is logged and swallowed.
    """
    try:
        upsert_customer(
            Customer,
            mobile=sale.customer_mobile,
            name=sale.customer_name,
            gstin=sale.buyer_gstin,
            made_at=sale.billed_at,
        )
    except Exception:
        logger.exception("customer upsert failed for sale %s", sale.doc_number)


def _resolve_store(code: str, actor: Any) -> Store:
    """Step 1 - the bill's store, and the caller's right to bill at it."""
    from sell.services.dataset import TillScopeError, resolve_till_store

    try:
        store = resolve_till_store(actor)
    except TillScopeError as exc:
        raise AcceptError("SCOPE_DENIED", str(exc), 403) from None
    if store.code.lower() != code.strip().lower():
        raise AcceptError(
            "SCOPE_DENIED",
            "A till bills for its own store only.",
            403,
        )
    return store


def _resolve_original_bill(data: dict[str, Any], store: Store) -> Sale | None:
    """Step 8 - the bill an exchange gives back against, if we still hold it.

    Pinned to the billing store, and the pin is the point. `till_seq` is a small
    counting number, so a payload naming another store's bill is a guess anybody
    can make - and a lookup that honoured it would let a cashier read back what a
    different shop's customer paid (the refund check quotes it), take that shop's
    cost of record into this shop's ledger, and spend the returnable quantity of a
    line they have never seen. v1 exchanges are same-store anyway (grill Q4), so
    the reference is read as evidence about *this* counter and nothing else.

    A cancelled bill is not returnable either: it has already been reversed, and
    giving back against it would refund what the books say was never sold.
    """
    exchange = data.get("exchange") or None
    if not exchange:
        return None
    ref = exchange["original"]
    return (
        Sale.objects.filter(
            store=store,
            fy=ref["fy"],
            till_seq=ref["till_seq"],
            docstatus=DocStatus.SUBMITTED,
        )
        .prefetch_related("lines", "lines__shares")
        .first()
    )


def _goods_plan(store: Store, data: dict[str, Any]) -> GoodsPlan | None:
    """Which goods pieces this bill takes, or `None` if this store is a legacy one.

    Settled before a single row is written, because a bill that cannot be supplied
    must refuse before it has a number: `INSUFFICIENT_ELIGIBLE_STOCK` names the
    barcode that is short, and nothing of the bill is left behind.
    """
    if not is_goods_site(store):
        return None
    try:
        # Ticket 22: an alteration charge is a service, with no piece to take.
        return plan_sale(store, [p for p in data["all_lines"] if not _is_alteration(p)])
    except GoodsSaleError as exc:
        raise AcceptError(exc.code, exc.message, exc.status) from exc


def _prepare_lines(
    data: dict[str, Any], store: Store, original_bill: Sale | None, goods: GoodsPlan | None
) -> list[_PreparedLine]:
    """Steps 5 and 8 - every line placed against a cohort or against an original."""
    sellers = SellerBook(store, data["all_lines"])
    prepared: list[_PreparedLine] = []
    for payload in data["all_lines"]:
        if payload["direction"] == SaleLine.Direction.RETURN:
            if payload.get("shares"):
                raise AcceptError(
                    "SPLIT_INVALID",
                    f"Line {payload['line_no']}: a piece coming back follows its own "
                    "sale's split; it cannot be split again.",
                    422,
                )
            line = _prepare_return_line(payload, store, original_bill, goods)
        elif _is_alteration(payload):
            line = _prepare_alteration_line(payload, sellers)
        else:
            line = _prepare_sale_line(payload, store, sellers, goods)
        line.dims, line.season = _line_description(line)
        if line.is_alteration:
            # Ticket 22: a service has no cost to post and waits for nothing.
            prepared.append(line)
            continue
        # A piece coming back unwinds the posting its own sale made, so its plan is
        # read off that line rather than derived again from masters that may have
        # moved since (Rule 3). Only a paper-era return, whose original bill we do
        # not hold, has nothing to read and falls back to a fresh resolution.
        line.cost = (
            plan_from_original(line.original)
            if line.original is not None
            else resolve_cost_plan(
                brand=line.dims.get("brand", ""),
                barcode=payload["barcode"].strip(),
                season=line.season,
                unit_cost_paise=line.unit_cost_paise,
            )
        )
        prepared.append(line)
    _check_return_quantities(prepared)
    _plan_return_shares(prepared)
    return prepared


def _plan_sale_shares(sellers: SellerBook, payload: dict[str, Any]) -> list[PlannedShare]:
    """Ticket 08: a sold line's two shares, checked as the till checks them (B14).

    Refused, never corrected: the till refuses the same shares before the bill
    is made, so only a broken or altered till can send them.
    """
    shares = payload.get("shares") or []
    if not shares:
        return []
    line_no = payload["line_no"]
    problem = split_problem(payload.get("salesperson"), shares)
    if problem is not None:
        raise AcceptError("SPLIT_INVALID", f"Line {line_no}: {problem}", 422)
    sellers_named = [sellers.seller_for({"salesperson": share["salesperson"]}) for share in shares]
    if any(seller is None or seller.staff is None for seller in sellers_named):
        raise AcceptError(
            "VALIDATION",
            f"Line {line_no}: a salesperson on this split is not on this store's staff list.",
            400,
        )
    percents = [int(share["percent"]) for share in shares]
    values = split_value(int(payload["value_paise"]), percents)
    return [
        PlannedShare(
            position=position,
            staff_id=seller.staff.pk,
            code=seller.code,
            name=seller.name,
            percent=percent,
            value_paise=value,
        )
        for position, (seller, percent, value) in enumerate(
            zip(sellers_named, percents, values, strict=True), start=1
        )
        if seller is not None and seller.staff is not None
    ]


def _plan_return_shares(lines: list[_PreparedLine]) -> None:
    """Ticket 08: a piece back against a split line gives back each person's part.

    In the same proportion as the sale, counted over everything already given
    back against that line (`split_shares.split_returned`), so a line returned
    in parts ends with each person's shares netting to nothing.
    """
    given_back: dict[int, int] = {}
    for line in lines:
        original = line.original
        if not line.is_return or original is None:
            continue
        sold = sorted(original.shares.all(), key=lambda share: share.position)
        if not sold:
            continue
        if original.id not in given_back:
            given_back[original.id] = returned_so_far(original)[1]
        before = given_back[original.id]
        values = split_returned(line.value_paise, before, [share.percent for share in sold])
        given_back[original.id] = before + line.value_paise
        line.shares = [
            PlannedShare(
                position=share.position,
                staff_id=share.salesperson_id,
                code=share.salesperson_code,
                name=share.salesperson_name,
                percent=share.percent,
                value_paise=value,
            )
            for share, value in zip(sold, values, strict=True)
        ]


def _mark_late_returns(
    lines: list[_PreparedLine], original_bill: Sale | None, exchanged_at: Any
) -> bool:
    """Mark a known exchange whose original bill is outside the policy window.

    Lateness is derived from the submitted original and today's policy, never
    from a till checkbox. Paper-era returns have no trustworthy sale date and
    keep their existing missing-original flag path.
    """
    if original_bill is None or not any(line.is_return for line in lines):
        return False
    # Judge when the offline exchange happened, not when its queue eventually
    # reaches head office. Crossing midnight while offline must not turn a valid,
    # already-printed bill into a refusal.
    days_old = (timezone.localdate(exchanged_at) - timezone.localdate(original_bill.billed_at)).days
    late = days_old > SellPolicy.current().return_window_days
    if late:
        for line in lines:
            if line.is_return:
                line.override_needed = True
    return late


def _named_seller(sellers: SellerBook, payload: dict[str, Any], line_no: Any) -> Seller:
    """Who sold the line: a staff record placed at this store, or an old row.

    A line that names nobody here is refused, as it always was: every sold piece
    carries the name of who sold it.
    """
    seller = sellers.seller_for(payload)
    if seller is None:
        raise AcceptError(
            "VALIDATION",
            f"Line {line_no}: no salesperson of this store was named. "
            "Every sold piece carries the name of who sold it.",
            400,
        )
    return seller


def _prepare_sale_line(
    payload: dict[str, Any],
    store: Store,
    sellers: SellerBook,
    goods: GoodsPlan | None,
) -> _PreparedLine:
    if goods is not None:
        return _prepare_goods_sale_line(payload, sellers, goods)
    piece = resolve_piece(store, payload["barcode"].strip(), payload["season"].strip())
    if not piece.is_known and not payload["manual_desc"].strip():
        raise AcceptError(
            "LINE_UNRESOLVED",
            f"Line {payload['line_no']}: barcode '{payload['barcode']}' is not in the books "
            "and the line carries no description, so there is nothing to sell.",
            422,
        )
    seller = _named_seller(sellers, payload, payload["line_no"])
    return _PreparedLine(
        payload=payload, piece=piece, seller=seller, shares=_plan_sale_shares(sellers, payload)
    )


def _is_alteration(payload: dict[str, Any]) -> bool:
    """Ticket 22: is this payload line a paid alteration's own line?"""
    return bool(payload.get("kind") == SaleLine.Kind.ALTERATION)


def _check_alteration_lines(data: dict[str, Any], store: Store) -> None:
    """Ticket 22: an alteration charge is one sold service at SAC 9988 and 5%.

    The till makes it that way and only that way, so any other shape is refused
    before the bill has a number. The switch is gated (OQ-49, CA sign-off): at a
    real store no till is ever told it is on (B1, B2), so a charge from one is not
    a till's. At a demo store a charge billed offline before the switch went off
    is kept - the bill is printed.
    """
    charges = [p for p in data["all_lines"] if _is_alteration(p)]
    if not charges:
        return
    if is_real_store(store) and not is_feature_on(store, ALTERATIONS):
        raise AcceptError(
            "FEATURE_OFF",
            f"Alterations are waiting for OQ-49 and the CA's sign-off and are not on at "
            f"{store.code}. This bill cannot have come from its till.",
            403,
        )
    for payload in charges:
        problem = _alteration_problem(payload)
        if problem is not None:
            raise AcceptError("VALIDATION", f"Line {payload['line_no']}: {problem}.", 400)


def _alteration_problem(payload: dict[str, Any]) -> str | None:
    """What is wrong with an alteration charge's shape, or ``None``."""
    if payload["direction"] != SaleLine.Direction.SALE:
        return "an alteration charge is not a piece to take back"
    if int(payload["qty"]) != 1:
        return "an alteration charge is one line of quantity 1"
    if int(payload["mrp_paise"]) <= 0:
        return "a paid alteration has a charge; a free one has no line"
    if int(payload["disc_paise"]) != 0 or payload.get("offer_id"):
        return "an alteration charge takes no discount and no offer"
    if Decimal(payload["gst_rate"]) != ALTERATION_GST_RATE:
        return f"an alteration charge is taxed at {ALTERATION_GST_RATE}% (SAC {ALTERATION_SAC})"
    if payload.get("shares"):
        return "an alteration charge is not split between salespeople"
    return None


def _prepare_alteration_line(payload: dict[str, Any], sellers: SellerBook) -> _PreparedLine:
    """Ticket 22: a paid alteration - a service, no piece, still sold by somebody."""
    return _PreparedLine(
        payload=payload,
        piece=ResolvedPiece(cohort=None, season="", unit_cost_paise=0, dims={}),
        seller=_named_seller(sellers, payload, payload["line_no"]),
    )


def _prepare_goods_sale_line(
    payload: dict[str, Any], sellers: SellerBook, goods: GoodsPlan
) -> _PreparedLine:
    """A sold line at a goods-v1 store: the pieces are already chosen, FIFO by origin.

    There is no "sold before inward" here and no deferred-for-price line. A piece
    that is not accepted, valued and on the shop floor was refused before the bill
    got this far, which is what R-INV-002 asks for.
    """
    line_no = int(payload["line_no"])
    seller = _named_seller(sellers, payload, line_no)
    return _PreparedLine(
        payload=payload,
        piece=ResolvedPiece(cohort=None, season="", unit_cost_paise=0, dims={}),
        seller=seller,
        shares=_plan_sale_shares(sellers, payload),
        goods=goods.sold.get(line_no) or [],
        goods_piece=goods.pieces.get(line_no),
    )


def _prepare_return_line(
    payload: dict[str, Any], store: Store, original_bill: Sale | None, goods: GoodsPlan | None
) -> _PreparedLine:
    original = None
    if original_bill is not None:
        wanted = payload["original_line"]
        original = next(
            (
                line
                for line in original_bill.lines.all()
                if line.direction == SaleLine.Direction.SALE
                and (
                    line.line_no == wanted if wanted else line.barcode == payload["barcode"].strip()
                )
            ),
            None,
        )
    if original is not None and original.is_alteration:
        raise AcceptError(
            "ALTERATION_NOT_RETURNABLE",
            f"Line {payload['line_no']}: line {original.line_no} of {original.sale.doc_number} "
            "is an alteration charge, not a piece to take back.",
            422,
        )
    if goods is not None:
        return _prepare_goods_return_line(payload, goods, original)
    piece = resolve_piece(store, payload["barcode"].strip(), payload["season"].strip())
    return _PreparedLine(
        payload=payload,
        piece=piece,
        original=original,
        # The paper era: a piece bought before the system existed still comes
        # back, and the counter still takes it (flagged, never refused).
        original_missing=original is None,
    )


def _prepare_goods_return_line(
    payload: dict[str, Any], goods: GoodsPlan, original: SaleLine | None
) -> _PreparedLine:
    """A piece coming back at a goods-v1 store, at the cost its own sale posted.

    There is no paper era here and no flagged-but-accepted unknown. A goods store
    only ever sold a piece by naming the lot portion and the origin it came out
    of, so a return with no such record has no cost anybody can name - and the one
    thing this must never do is invent one (PRD §8, Rule 5). It refuses instead,
    and a human takes it from there.
    """
    if original is None:
        raise AcceptError(
            "ORIGINAL_REQUIRED",
            f"Line {payload['line_no']}: this store takes a piece back against the bill "
            "that sold it, and that bill is not one of ours.",
            422,
        )
    try:
        allocations = returned_allocations(original, int(payload["qty"]))
    except GoodsSaleError as exc:
        raise AcceptError(
            exc.code, f"Line {payload['line_no']}: {exc.message}", exc.status
        ) from exc
    return _PreparedLine(
        payload=payload,
        piece=ResolvedPiece(cohort=None, season="", unit_cost_paise=0, dims={}),
        original=original,
        original_missing=False,
        goods=allocations,
    )


def _check_return_quantities(lines: list[_PreparedLine]) -> None:
    """A line can only be given back as many times as it was sold.

    `ALREADY_RETURNED` is the contract's own code for this on the plain-return
    endpoint; the exchange leg is the same act inside a bill and answers the same
    way, because the alternative is a refund path with no ceiling on it. The two
    read one ledger (`sell.services.refunds`), so neither can give back what the
    other already did.
    """
    wanted: dict[int, int] = {}
    for line in lines:
        if line.is_return and line.original is not None:
            wanted[line.original.id] = wanted.get(line.original.id, 0) + line.qty
    for line in lines:
        if not line.is_return or line.original is None:
            continue
        already = returned_so_far(line.original)[0]
        if already + wanted[line.original.id] > line.original.qty:
            raise AcceptError(
                "ALREADY_RETURNED",
                f"Line {line.payload['line_no']}: only "
                f"{line.original.qty - already} of that piece is still returnable.",
                422,
            )


def _flag_job_card_returns(sale: Sale, store: Store, found: list[dict[str, Any]]) -> list[str]:
    """Ticket 22: flag the pieces ``_open_job_card_returns`` found, if any."""
    if not found:
        return []
    return [_flag(sale, store, ContinuityFlag.Kind.RETURNED_ON_JOB_CARD, {"lines": found})]


def _open_job_card_returns(lines: list[_PreparedLine]) -> list[dict[str, Any]]:
    """Ticket 22: pieces coming back that the store holds on an open job card.

    Such a garment is with the store or the tailor in billed-retained custody
    (R-INV-014), so it should not be at the counter to give back. The till prints
    before it syncs and cannot know, so the bill is never refused for it: it is
    flagged, and staff settle the job card. What a return does to goods in
    custody waits on OQ-49.
    """
    from sell.services.alterations import open_job_qty

    wanted: dict[int, int] = {}
    for line in lines:
        if line.is_return and line.original is not None:
            wanted[line.original.id] = wanted.get(line.original.id, 0) + line.qty
    found = []
    for line in lines:
        original = line.original
        if not line.is_return or original is None:
            continue
        held = open_job_qty(original)
        if held and returned_so_far(original)[0] + wanted[original.id] > original.qty - held:
            found.append(
                {"line_no": int(line.payload["line_no"]), "original_line": original.line_no}
            )
    return found


def _check_line_arithmetic(lines: list[_PreparedLine], *, return_tax: bool = False) -> None:
    """Step 3 - does each line add up on its own terms?

    Internal consistency only: MRP less discount is the line, and the tax quoted
    is the tax inside it at the rate quoted. Whether that rate is the *right* rate
    is a different question with a different answer (step 12 flags it), because
    the printed bill is a fact and a slab disagreement is not the customer's
    problem.

    Ticket 13: on a bill taken by the return tax rules, a piece coming back may
    carry no tax at all - past its credit-note deadline the value is given with
    no tax reduction. Whether it really was past the deadline is the server's
    own judgement, flagged when it differs (``_record_return_tax``).
    """
    for line in lines:
        payload = line.payload
        value = line.value_paise
        if line.is_return:
            _check_return_refund(line)
            if return_tax:
                _check_return_rate(line)
        else:
            expected = payload["mrp_paise"] * line.qty - payload["disc_paise"]
            if expected != value:
                raise AcceptError(
                    "TENDER_MISMATCH",
                    f"Line {payload['line_no']}: {payload['mrp_paise']} x {line.qty} less "
                    f"{payload['disc_paise']} is {expected}, but the line says {value}.",
                    422,
                )
        rate = Decimal(payload["gst_rate"])
        if return_tax and line.is_return and payload["gst_paise"] == 0:
            continue
        expected_gst = value - base_from_inclusive(value, rate)
        if expected_gst != payload["gst_paise"]:
            raise AcceptError(
                "TENDER_MISMATCH",
                f"Line {payload['line_no']}: {value} paise at {rate}% carries "
                f"{expected_gst} paise of tax, but the line says {payload['gst_paise']}.",
                422,
            )


def _check_return_rate(line: _PreparedLine) -> None:
    """Ticket 13: a piece coming back is reversed at the rate on its own bill.

    The value is checked by ``_check_return_refund``; this is the rate half. A
    leg quoting any other rate is not a till's work - the till copies the rate
    off the original line - so it is refused like a wrong value, before the bill
    has a number. A paper-era original has nothing to check against.
    """
    if line.original is None:
        return
    quoted = Decimal(line.payload["gst_rate"])
    if quoted != Decimal(line.original.gst_rate):
        raise AcceptError(
            "TENDER_MISMATCH",
            f"Line {line.payload['line_no']}: line {line.original.line_no} of "
            f"{line.original.sale.doc_number} was taxed at {line.original.gst_rate}%, "
            f"but the piece coming back quotes {quoted}%.",
            422,
        )


def _check_return_refund(line: _PreparedLine) -> None:
    """What comes back is what was paid (D2), never today's price.

    When the original bill is ours we hold the number and check it. When it is not
    - a paper-era purchase - there is nothing to check it against, so the till's
    figure stands and the bill carries a flag saying why.
    """
    if line.original is None:
        return
    original = line.original
    entitled = entitled_refund(original, line.qty)
    if entitled != line.value_paise:
        raise AcceptError(
            "TENDER_MISMATCH",
            f"Line {line.payload['line_no']}: line {original.line_no} of "
            f"{original.sale.doc_number} was paid {entitled} paise for that quantity, "
            f"but the refund says {line.value_paise}.",
            422,
        )


def _check_totals(data: dict[str, Any], lines: list[_PreparedLine]) -> None:
    """Step 3 - do the lines, the totals and the money taken all agree?"""
    totals = data["totals"]
    sales = [line for line in lines if not line.is_return]
    returns = [line for line in lines if line.is_return]
    checks = [
        (
            "gross",
            sum(line.payload["mrp_paise"] * line.qty for line in sales),
            totals["gross_paise"],
        ),
        (
            "discount",
            sum(line.payload["disc_paise"] for line in sales),
            totals["discount_paise"],
        ),
        (
            "net",
            sum(line.value_paise for line in sales)
            - sum(line.value_paise for line in returns)
            + totals["round_paise"],
            totals["net_paise"],
        ),
        (
            "GST",
            sum(line.payload["gst_paise"] for line in sales)
            - sum(line.payload["gst_paise"] for line in returns),
            totals["gst_paise"],
        ),
    ]
    for label, computed, declared in checks:
        if computed != declared:
            raise AcceptError(
                "TENDER_MISMATCH",
                f"The bill's lines come to {computed} paise of {label}, but its total "
                f"says {declared}.",
                422,
            )
    if totals["net_paise"] < 0:
        raise AcceptError(
            "EXCHANGE_SHORT",
            "The pieces going out must be worth at least what is coming back.",
            422,
        )
    tendered = sum(t["amount_paise"] for t in data["tenders"])
    # Ticket 13 (B60): the bank offer's part of a piece coming back was never the
    # customer's money, so they pay it back into the bill; it is reversed against
    # the bank offer receivable, not given to them.
    expected = totals["net_paise"] + sum(_bank_part(line) for line in returns)
    # Exactly, in both directions. `round_paise` is bounded to ±50 by the write
    # serializer and is part of the net the tenders have to meet - it is not
    # slack between the two. Under- and over-tendering are the same refusal.
    if tendered != expected:
        raise AcceptError(
            "TENDER_MISMATCH",
            f"The bill is {expected} paise but the tenders come to {tendered}.",
            422,
        )


def _check_cash_received(data: dict[str, Any]) -> None:
    """Step 3's other half - what the customer physically handed over (PRD §9.1).

    Not a tender: it never joins the sum above, because what posts to CASH is
    what the bill *took*. It is a fact about the drawer, and the two ways of
    getting it wrong are both ways of losing money quietly.

    **Below the cash tender** means the customer did not hand over enough to
    cover the cash half, so the bill says money arrived that did not.

    **Present with no cash tender at all** means somebody typed a figure into
    the box on a bill paid entirely by card or UPI. Ignoring it - which is what
    a presentation-only field invites - would print a receipt with a change line
    on a sale whose drawer never opened. Nought counts as present: "nothing was
    received" is a statement, and a different one from leaving the box blank.
    """
    received = data.get("cash_received_paise")
    if received is None:
        return
    cash = sum(t["amount_paise"] for t in data["tenders"] if t["mode"] == SaleTender.Mode.CASH)
    if cash <= 0:
        raise AcceptError(
            "CASH_RECEIVED",
            "Cash received is recorded, but this bill takes no cash.",
            422,
        )
    if received < cash:
        raise AcceptError(
            "CASH_RECEIVED",
            f"Cash received is {received} paise but the bill takes {cash} paise in cash.",
            422,
        )


@dataclass
class _Rulebook:
    """What the rulebook says this bill should have cost - the server's own answer.

    Computed once and used twice: the cap (step 6) subtracts it to find what a
    cashier gave on their own, and the advisory check (step 12) compares it with
    what was actually charged. Both need the *same* number, or a bill could be
    refused for a discount the daily check would then call correct.

    The store, the day and the lines ride along because the cap needs to ask the
    rulebook a second, narrower question - "was the rule this line cites really
    running over this piece?" - and asking it needs all three.
    """

    store: Store
    day: date
    resolution: Resolution
    lines: dict[int, BillLine]
    #: Ticket 11: the bill says it was priced with GST after discount, so the
    #: rulebook is read that way for the cap and every check.
    after_discount: bool = False
    #: Lines whose discount was credited to the rule they cite rather than to
    #: today's rulebook. Every one is flagged at step 12, whatever the figures
    #: come to - see `_check_discount_policy`.
    drift: set[int] = field(default_factory=set)
    #: Ticket 25: what each sold line's discount may be credited to the rulebook,
    #: on the server's own reading. The till's offer record is never trusted for
    #: more than this when the discount is split between the brand and KDPS.
    credits: dict[int, int] = field(default_factory=dict)

    def saving_for(self, line_no: int) -> int:
        outcome = self.resolution.by_line().get(line_no)
        return outcome.discount_paise if outcome else 0

    def credit_for(self, line: _PreparedLine) -> tuple[int, bool]:
        """What this line's discount may be credited to the rulebook, and whether
        that credit came from the rule the line *cites* rather than from today's
        reading of the book.

        The second half is the important one: a credit the current rulebook does
        not produce is a disagreement, and a disagreement is always put in front
        of a human, whatever the amounts happen to be.
        """
        line_no = line.payload["line_no"]
        today = self.saving_for(line_no)
        cited = credit_from_cited_rule(
            line.payload.get("offer_id"),
            self.store.code,
            self.day,
            list(self.lines.values()),
            line_no,
            after_discount=self.after_discount,
            tenant_id=self.store.tenant_id,
        )
        return max(today, cited), cited > today


def _server_resolution(data: dict[str, Any], store: Store, lines: list[_PreparedLine]) -> _Rulebook:
    """Price this bill's sold lines against the rulebook, server-side.

    The `no_discount` flag is fetched here rather than taken off the payload: it
    is the one input to the rulebook that lives on the SKU master and not on the
    bill, and a till that mis-stated it could discount a piece the AMM sheet says
    is never discounted (D5 Q3).
    """
    day = _billed_on(data)
    # Ticket 22: no offer reaches an alteration charge.
    sold = [line for line in lines if not line.is_return and not line.is_alteration]
    barcodes = [line.payload["barcode"].strip() for line in sold]
    never_discounted = set(
        Sku.objects.filter(barcode__in=barcodes, no_discount=True).values_list("barcode", flat=True)
    )
    bill_lines = {
        line.payload["line_no"]: BillLine(
            line_no=line.payload["line_no"],
            barcode=line.payload["barcode"].strip(),
            season=line.season,
            qty=line.qty,
            mrp_paise=int(line.payload["mrp_paise"]),
            dims=line.dims,
            no_discount=line.payload["barcode"].strip() in never_discounted,
        )
        for line in sold
    }
    after_discount = bool(data.get("gst_after_discount"))
    resolution = (
        resolve_bill(
            store.code, day, list(bill_lines.values()),
            after_discount=after_discount, tenant_id=store.tenant_id,
        )
        if bill_lines
        else Resolution(lines=(), entitlements=(), after_discount=after_discount)
    )
    return _Rulebook(
        store=store,
        day=day,
        resolution=resolution,
        lines=bill_lines,
        after_discount=after_discount,
    )


def _billed_on(data: dict[str, Any]) -> date:
    """The day the counter printed this bill, in the store's own reckoning."""
    return timezone.localdate(data["billed_at"])


def _check_discount_policy(lines: list[_PreparedLine], rulebook: _Rulebook) -> None:
    """Step 6 - the manual discount dials are absolute.

    Whatever the rulebook is answerable for is the rulebook's; the remainder is a
    manual discount. It is capped at the policy percentage, with no manager
    override, and the policy separately decides whether it can stack on an offer
    discount at all.

    The rulebook's share is the server's own resolution, never the till's
    `offer_evidence.saved_paise`: that number arrives from the till, and the till
    is the party the cap exists to constrain. A cap the capped party can lift by
    describing its own discount as an offer is not a cap.

    With one door open, and only one. A line may also be credited with what the
    rule it *cites* works out - re-run server-side, so it is an amount rather
    than a claim (`recompute.credit_from_cited_rule` says why at length). Any
    line credited that way is recorded on `rulebook_drift`, and every one of them
    is flagged at step 12 whatever the figures come to: the door exists so a
    store's queue is not stopped by head office editing master data, not so a
    discount can pass unseen.
    """
    policy = SellPolicy.current()
    cap_percent = policy.manual_discount_cap_percent
    for line in lines:
        if line.is_return or line.is_alteration:
            continue
        credit, drifted = rulebook.credit_for(line)
        given = int(line.payload["disc_paise"])
        # Never more than was actually given, so a rulebook more generous than the
        # counter cannot manufacture headroom for a manual discount on top.
        manual = given - min(credit, given)
        line.manual_disc_paise = manual
        allowance = int(Decimal(line.payload["mrp_paise"] * line.qty) * cap_percent / 100)
        if manual > 0 and credit > 0 and not policy.manual_discount_on_offer_lines:
            raise AcceptError(
                "DISCOUNT_ON_OFFER_LINE",
                f"Line {line.payload['line_no']} already has a head-office offer, so manual "
                "discount is off for offer lines.",
                422,
            )
        if manual > allowance:
            raise AcceptError(
                "DISCOUNT_OVER_CAP",
                f"Line {line.payload['line_no']} discounts more than the head-office "
                f"limit ({cap_percent}% of MRP).",
                422,
            )
        rulebook.credits[line.payload["line_no"]] = credit
        if drifted:
            rulebook.drift.add(line.payload["line_no"])


def _guard_bill_number(data: dict[str, Any], store: Store) -> None:
    """Step 4 - is this number already somebody else's bill?

    The database says so too (`uq_sale_store_fy_seq`, and the kernel's unique
    `doc_number`), and that is what makes acceptance exactly-once under a race.
    This check exists so the ordinary case answers with the contract's own code
    instead of an integrity error, and it is the check that would fail first if
    a handover went wrong.
    """
    clash = (
        Sale.objects.filter(store=store, fy=data["fy"], till_seq=data["till_seq"])
        # A row carrying our own key is *this bill*, arriving twice. Without this
        # a replay that overlapped its original would be told its own number
        # belonged to somebody else, and the till would halt a queue over a bill
        # that had in fact synced perfectly.
        .exclude(idempotency_uuid=data["idempotency_uuid"])
        .exists()
    )
    if clash:
        raise _bill_number_taken(data)


def _check_till_number(data: dict[str, Any], store: Store) -> None:
    """Step 4's other half - does the display number belong to this store's counter?

    R-POS-005 asks for series ownership to be *enforced, not assumed*, and the
    only enforceable half on the server is this: a bill claiming
    `{StoreCode}-{CounterID}-{Seq}` has to name the counter this store actually
    registered, and the sequence it already carries.

    Three shapes, three answers, and the quiet one matters most:

      · a store with a registered till sends the number and it must match;
      · a store with no registered till sends nothing, exactly as it always has -
        legacy numbering is untouched by this ticket;
      · a store with no registered till sending one is refused, because a device
        minting a counter id nobody issued is the failure this check exists for.
    """
    from sell.services.till_authority import active_till, render_till_number

    claimed = (data.get("till_number") or "").strip()
    till = active_till(store)
    if till is None:
        if claimed:
            raise AcceptError(
                "TILL_SERIES",
                f"This bill says it came from counter {claimed}, and {store.code} has no "
                "registered counter. A manager registers the device before it bills.",
                422,
            )
        return
    if not claimed:
        # Not a refusal. A till that was registered after this bill was printed is
        # holding paper from before the counter id existed, and refusing it would
        # strand money over a change made at head office.
        return
    expected = render_till_number(till.series_prefix, int(data["till_seq"]))
    if claimed != expected:
        raise AcceptError(
            "TILL_SERIES",
            f"Bill {claimed} does not belong to {store.code}'s counter, which numbers "
            f"{expected}. Two devices have been numbering one series.",
            422,
        )


def _authorised_kind(lines: list[_PreparedLine]) -> str:
    """What the manager's tap on this bill was actually for.

    Derived from what the pipeline itself found, never taken from the payload's
    `override.kind`. Its sole remaining meaning is a late-return window override.
    """
    return "late_return" if any(line.override_needed for line in lines) else ""


def _write_sale(
    data: dict[str, Any],
    store: Store,
    actor: Any,
    original_bill: Sale | None,
    lines: list[_PreparedLine],
    override: Any,
    *,
    invoice_number: str | None = None,
) -> Sale:
    """Step 9 - the draft and its lines, before a number exists."""
    customer = data.get("customer") or {}
    totals = data["totals"]
    buyer_gstin = gstin_normalise(customer.get("gstin") or "")
    # What the till said about the manager's tap. Only the *time* is taken from
    # it - a clock this server does not have - and only when the pipeline both
    # recognised the person and independently found a late return.
    authorisation = (data.get("override") or {}) if override else {}
    sale = Sale.objects.create(
        idempotency_uuid=data["idempotency_uuid"],
        store=store,
        fy=data["fy"],
        till_seq=data["till_seq"],
        origin=data["origin"],
        billed_at=data["billed_at"],
        customer_name=(customer.get("name") or "").strip(),
        # Canonicalised here, not just in the master upsert: db-design links a
        # bill to its customer by mobile at query time, over this indexed
        # column, so a bill that snapshots '+91 98765-43210' as typed would
        # never join to master row '9876543210'. Both sides of that join are
        # written at this one boundary, so both get the same spelling.
        customer_mobile=normalise_mobile(customer.get("mobile") or ""),
        buyer_gstin=buyer_gstin,
        # Derived here rather than after the number is minted, because a posted
        # document is immutable in the database: the FSM trigger lets a submitted
        # row move to cancelled and change nothing else. Anything a bill is going
        # to say about itself has to be said before it is posted.
        b2b_tax_kind=_b2b_tax_kind(buyer_gstin, store),
        gross_paise=totals["gross_paise"],
        discount_paise=totals["discount_paise"],
        net_paise=totals["net_paise"],
        gst_paise=totals["gst_paise"],
        round_paise=totals["round_paise"],
        cash_received_paise=data.get("cash_received_paise"),
        exchange_of=original_bill,
        # Only a manager the pipeline actually recognised is written here. An
        # override naming somebody who is not a manager of this store has already
        # been discarded by `manager_for_override`, and recording the id anyway
        # would put a name on a bill that person never authorised.
        override_by=override,
        override_kind=_authorised_kind(lines) if override else "",
        override_at=authorisation.get("at"),
        created_by=actor,
        # The device's own display number, as it printed (R-POS-005). Stored, not
        # derived, because the counter that printed it can be replaced tomorrow
        # and this bill still reads the way the customer's copy does.
        till_number=(data.get("till_number") or "").strip(),
        # The tax settings version the till says it taxed this bill under
        # (ticket 03, R-POS-013). A till that predates versions sends none, and
        # it taxed by the slab table, which is version 1 (B5).
        tax_setting_version=data.get("tax_setting_version") or LEGACY_VERSION,
        # Ticket 11: priced with GST after discount, as the till says (B5: absent
        # is no, every bill before the ticket and at every store with it off).
        gst_after_discount=bool(data.get("gst_after_discount")),
        # Ticket 13: the returns were taken by the return tax rules, as the till
        # says (B5: absent is no).
        return_tax=_return_tax(data),
        # The number in the new invoice series, only when the server can vouch
        # for it (`resolve_invoice_number`); otherwise the bill is flagged.
        tax_invoice_number=invoice_number,
        payload_fingerprint=payload_fingerprint(data),
    )
    # Ticket 12: each line records the version and rule it was taxed under.
    rulebook = StoreTaxBooks(store).recorded(sale.tax_setting_version, sale.billed_at)
    for line in lines:
        line.row = _write_line(sale, line, override, _line_tax_record(sale, line, rulebook))
    _write_shares(lines)
    return sale


def _write_shares(lines: list[_PreparedLine]) -> None:
    """Ticket 08: each person's share, beside the line it belongs to."""
    SaleLineShare.objects.bulk_create(
        [
            SaleLineShare(
                line=line.row,
                position=share.position,
                salesperson_id=share.staff_id,
                salesperson_code=share.code,
                salesperson_name=share.name,
                percent=share.percent,
                value_paise=share.value_paise,
            )
            for line in lines
            if line.row is not None
            for share in line.shares
        ]
    )


def _record_shares(sale: Sale, store: Store, actor: Any, lines: list[_PreparedLine]) -> list[str]:
    """Ticket 08: audit the shares this bill wrote, and flag a split the store has off.

    A split sold line from a store whose switch is off is kept, never refused:
    the till prints before it syncs, so the bill was made while the switch was
    on (queued offline) or by an old till, and refusing it would lose it.
    """
    written = [line for line in lines if line.shares]
    if not written:
        return []
    audit_shares(
        sale,
        store,
        actor,
        [
            {
                "line_no": int(line.payload["line_no"]),
                "direction": line.payload["direction"],
                "shares": [
                    {
                        "position": share.position,
                        "staff_id": str(share.staff_id),
                        "percent": share.percent,
                        "value_paise": share.value_paise,
                    }
                    for share in line.shares
                ],
            }
            for line in written
        ],
    )
    split_sold = [line for line in written if not line.is_return]
    if split_sold and not split_on(store):
        return [
            _flag(
                sale,
                store,
                ContinuityFlag.Kind.SPLIT_WHILE_OFF,
                {"lines": [int(line.payload["line_no"]) for line in split_sold]},
            )
        ]
    return []


def _line_tax_record(
    sale: Sale, line: _PreparedLine, rulebook: TaxRulebook | None
) -> dict[str, Any]:
    """The version and rule a line was taxed under (ticket 12, R-POS-013).

    A sold line: the version its bill recorded, and the rule of it that covers
    the line's HSN - worked out here, from what the bill says, never guessed from
    what is in force now. A version this server does not hold is ``unknown``
    (the bill is already flagged ``tax_version_mismatch``). An exchange leg is
    reversed at its original bill's rate, so it keeps the original line's record.
    """
    if line.is_return and line.original is not None:
        original = line.original
        return {
            "tax_setting_version": original.tax_setting_version
            or original.sale.tax_setting_version,
            "tax_rule_kind": original.tax_rule_kind,
            "tax_rule_hsn_prefix": original.tax_rule_hsn_prefix,
        }
    if line.is_alteration:
        # Ticket 22: the alteration's own fixed rate, not a rule of the version.
        return {
            "tax_setting_version": sale.tax_setting_version,
            "tax_rule_kind": SaleLine.Kind.ALTERATION,
            "tax_rule_hsn_prefix": ALTERATION_SAC,
        }
    if rulebook is None:
        return {
            "tax_setting_version": sale.tax_setting_version,
            "tax_rule_kind": RULE_UNKNOWN,
            "tax_rule_hsn_prefix": "",
        }
    kind, prefix = rulebook.rule_record(line.dims.get("hsn", ""))
    return {
        "tax_setting_version": rulebook.version,
        "tax_rule_kind": kind,
        "tax_rule_hsn_prefix": prefix,
    }


def _write_line(
    sale: Sale, line: _PreparedLine, override: Any, tax_record: dict[str, Any]
) -> SaleLine:
    payload = line.payload
    alteration = line.is_alteration
    brand_id: int | None = None
    if not alteration:
        from masters.brand_identity import identity_id, with_brand_identity
        from stockledger.models import StockOnHand

        if line.original is not None:
            brand_id = identity_id(line.original, sale.store.tenant_id)
        elif line.goods_piece is not None:
            brand_id = line.goods_piece.brand_id
        else:
            brands = set(with_brand_identity(
                StockOnHand.objects.filter(store=sale.store, sku_code=payload["barcode"].strip()),
                sale.store.tenant_id,
            ).values_list("_access_brand_id", flat=True))
            if len(brands) == 1:
                brand_id = brands.pop()
        # A bill may have been printed while the till was offline. Preserve it
        # with unresolved identity rather than inventing a brand or rejecting
        # the customer's completed sale. Brand-scoped readers require a reviewed
        # identity and the reconciliation screen lists rows left unbound.
    return SaleLine.objects.create(
        **tax_record,
        sale=sale,
        brand_ref_id=brand_id,
        line_no=payload["line_no"],
        direction=payload["direction"],
        kind=SaleLine.Kind.ALTERATION if alteration else SaleLine.Kind.GOODS,
        barcode=ALTERATION_CODE if alteration else payload["barcode"].strip(),
        season=line.season,
        **line.dims,
        qty=line.qty,
        mrp_paise=payload["mrp_paise"],
        disc_paise=payload["disc_paise"],
        net_paise=line.value_paise,
        gst_rate=payload["gst_rate"],
        gst_paise=payload["gst_paise"],
        unit_cost_paise=line.unit_cost_paise,
        salesperson=line.seller.staff if line.seller else None,
        salesperson_match=line.seller.match if line.seller else None,
        salesperson_code=line.seller.code if line.seller else "",
        salesperson_name=line.seller.name if line.seller else "",
        offer=_offer_cited(payload, sale.store.tenant_id),
        offer_evidence=payload["offer_evidence"],
        manual_disc_paise=line.manual_disc_paise,
        manual_desc=ALTERATION_DESCRIPTION if alteration else payload["manual_desc"].strip(),
        sold_before_inward=not line.is_return and not alteration and not line.piece.is_known,
        # Ticket 22: a service has no cost to wait for, so it is never deferred.
        costing_status=(
            SaleLine.CostingStatus.POSTED
            if line.cost.postable or alteration
            else SaleLine.CostingStatus.DEFERRED
        ),
        # Frozen here and read back by any exchange against this line: which book
        # the cost came out of, and who a brand-owned piece is settled with. The
        # brand's terms are editable master data; what this bill posted is not.
        cost_book=line.cost.book,
        cost_vendor=line.cost.vendor,
        return_reason=payload["reason"].strip() if line.is_return else "",
        condition=(payload["condition"] or SaleLine.Condition.GOOD) if line.is_return else "",
        original_line=line.original,
        bank_offer_paise=_bank_part(line),
        override_by=override if line.override_needed else None,
        # The bill's own origin outcome (R-POS-009): which pieces this line took,
        # or gave back, and what each one cost. Written once and never recomputed.
        goods_allocations=[row.as_json() for row in line.goods],
    )


def _offer_cited(payload: dict[str, Any], tenant_id: Any) -> Offer | None:
    """The rule the till says won this line, if it names one that exists.

    Recorded, not trusted. What the bill was *worth* is settled by the server's
    own resolution (`_server_resolution`); this field only answers "which rule
    did the counter believe it was selling under", which is the question the
    daily check starts from and which nobody else can answer afterwards. An id
    naming no rule is dropped rather than refused - the bill is printed, and a
    bad reference is a finding for step 12, not a reason to stop a queue.
    """
    offer_id = payload.get("offer_id")
    if not offer_id:
        return None
    return Offer.objects.for_tenant(tenant_id).filter(pk=offer_id).first()


def _line_description(line: _PreparedLine) -> tuple[dict[str, str], str]:
    """How a line describes the piece it is about, snapshotted at billing (Rule 3).

    Three sources, in order of who knows best. A return leg is described by the
    line it gives back - the piece is that piece, whatever the shelf says today. A
    resolved scan is described by its cohort. A scan that resolved to nothing at
    all falls back to the SKU registry, which may still know the barcode even when
    no cohort prices it, and to blanks when even that draws a blank.
    """
    if line.original is not None:
        return line_dims(line.original), line.original.season
    if line.is_alteration:
        return {"item": "Alteration", "hsn": ALTERATION_SAC}, ""
    if line.goods_piece is not None:
        return {**line.goods_piece.dims, "hsn": line.goods_piece.hsn}, line.goods_piece.season
    dims = dict(line.piece.dims)
    season = dims.pop("season", "")
    if not dims:
        dims = _dims_from_registry(line.payload["barcode"].strip())
    return dims, season or line.payload["season"].strip()


def _dims_from_registry(barcode: str) -> dict[str, str]:
    """What the SKU master knows about a barcode with no cohort behind it."""
    return line_dims(Sku.objects.filter(barcode=barcode).first())


# --- what happens once the bill has a number -------------------------------


def _flag(sale: Sale, store: Store, kind: str, details: dict[str, Any]) -> str:
    ContinuityFlag.objects.create(kind=kind, store=store, sale=sale, details=details)
    return kind


def _flag_number_hole(sale: Sale, store: Store, minted: Any) -> list[str]:
    """Step 4 - bills the store minted before this one that have not arrived.

    A hole is normal for a few minutes and a problem by end of day, so it is
    reported rather than refused: refusing would stop a store selling because an
    *older* bill is stuck, which is exactly backwards.
    """
    accepted = getattr(minted, "accepted", None)
    if accepted is None or not accepted.hole_count:
        return []
    return [
        _flag(
            sale,
            store,
            ContinuityFlag.Kind.NUMBER_HOLE,
            {
                "from_seq": accepted.hole_from,
                "count": accepted.hole_count,
                "counter_advanced": accepted.counter_advanced,
            },
        )
    ]


def _flag_billed_while_paused(sale: Sale, store: Store) -> list[str]:
    """Step 4 - a bill numbered inside the counter's transfer pause (§10.2).

    Only an old or altered device can print one; the till refuses to. Kept, not
    refused, because it is money that has already changed hands - and flagged,
    because the stock it sold may have been promised to a transfer meanwhile.
    """
    from sell.services.till_authority import billed_while_paused

    pause = billed_while_paused(store.pk, sale.fy, sale.till_seq)
    if pause is None:
        return []
    return [
        _flag(
            sale,
            store,
            ContinuityFlag.Kind.BILLED_WHILE_PAUSED,
            {
                "pause_fy": pause.fy,
                "pause_next_seq": pause.next_seq,
                "paused_at": pause.paused_at.isoformat(),
            },
        )
    ]


def _flag_missing_originals(sale: Sale, store: Store, lines: list[_PreparedLine]) -> list[str]:
    missing = [
        line.payload["line_no"] for line in lines if line.is_return and line.original_missing
    ]
    if not missing:
        return []
    return [
        _flag(
            sale,
            store,
            ContinuityFlag.Kind.RETURN_ORIG_MISSING,
            {"lines": missing},
        )
    ]


def _write_stock_legs(
    sale: Sale, store: Store, lines: list[_PreparedLine], actor: Any, goods: GoodsPlan | None
) -> None:
    """Step 10 - the stock half of the bill.

    A sold piece leaves the shelf; a returned one comes back to it, unless it
    comes back damaged, in which case it goes straight into quarantine and never
    becomes sellable again without somebody looking at it (D3). Which of those a
    line is, and where it lands, is `sell.services.movements` - shared with the
    sweep so the two cannot answer it differently.

    A line the books cannot price writes nothing. That is not a gap: the piece was
    never inwarded, so there is no stock to take off a shelf it was never on, and
    the movement posts with the cost event when the paperwork lands (#186).

    None of that is true at a goods-v1 store, which has no on-hand projection to
    move and no quarantine bucket of its own. There the whole bill is one journal
    posting against the pieces it actually took - and nothing at all is written to
    the legacy ledger (PRD §8).
    """
    if goods is not None:
        if not any(goods.sold.values()) and not any(line.is_return for line in lines):
            return  # ticket 22: a bill of alteration charges alone moves no goods
        post_goods_sale(
            sale,
            store,
            actor=actor,
            plan=goods,
            returns=[
                ReturnLeg(
                    line_no=int(line.payload["line_no"]),
                    condition=str(line.payload["condition"] or SaleLine.Condition.GOOD),
                    allocations=list(line.goods),
                )
                for line in lines
                if line.is_return and line.goods
            ],
        )
        return
    for row in _priced_rows(lines):
        post_stock_move(sale, store, row, actor)


def _priced_rows(lines: list[_PreparedLine]) -> list[SaleLine]:
    """The `SaleLine` rows the books can price, in payload order.

    The stock half of a bill turns on *pricing* alone, not on the fuller question
    the cost event asks: a piece whose brand the masters cannot place is still a
    piece that left a shelf, and its movement posts now even though its value has
    to wait.
    """
    return [line.row for line in lines if line.row is not None and line.is_priced]


def _record_deferred(store: Store, lines: list[_PreparedLine]) -> None:
    """Step 5 - park every line whose cost the books cannot post yet.

    Three ways a line lands here, and the row says which (`DeferredCosting.Reason`).
    The commonest is a piece sold before its paperwork arrived, so nothing prices
    it; an exchange leg from the paper era, whose original bill we do not hold and
    whose barcode has no cohort, has no cost of record either. The other two are
    priced but unplaceable: the masters have never heard of the brand on the bill,
    or the brand is one whose stock is not ours and nobody can say which supplier
    is owed for it. Booking either of those into INVENTORY on the assumption that
    it is ours is exactly the defect this queue exists to avoid.

    This is the row the Dashboard counts and the daily check ages; the sweep posts
    the cost event when what is missing arrives (#186). Nothing posts at zero in
    the meantime (Rule 5).
    """
    for line in lines:
        if line.row is None or line.cost.postable or line.is_alteration:
            continue
        DeferredCosting.objects.create(
            sale_line=line.row,
            store=store,
            barcode=line.row.barcode,
            season=line.row.season,
            qty=line.row.qty,
            reason=line.cost.deferral,
        )


def _post_value(sale: Sale, lines: list[_PreparedLine], actor: Any) -> None:
    """Step 10 - the money event and the cost event (see `sell.services.postings`)."""
    post_sale_value(
        sale,
        [
            CostedLine(row=line.row, plan=line.cost, cost_override=line.goods_cost_paise)
            for line in lines
            if line.row is not None
        ],
        list(sale.tenders.all()),
        actor,
    )


def _apply_tenders(
    sale: Sale,
    plans: list[dict[str, Any]],
    reservation_id: Any = None,
    special_order_id: Any = None,
    vouchers: dict[str, GiftVoucher] | None = None,
) -> None:
    """Step 7 - write the cash, card, UPI, bank offer and advance tenders.

    An advance tender (ticket 20) is the advance of the reservation the bill
    collects, already checked by ``reservations.release_for_bill`` - or of the
    special order it collects (ticket 21), checked by
    ``special_orders.check_for_bill``. A bill names at most one of the two.

    A bank offer tender (ticket 11) names its offer only when head office knows
    it as a bank-layer offer; a number it does not know is not an offer, and
    the till/server check flags the difference rather than refusing the bill.
    """
    # Ticket 19: a gift voucher tender names its voucher, already checked and
    # locked by ``gift_vouchers.check_for_bill`` (``vouchers``, by number).
    vouchers = vouchers or {}
    bank_ids = {int(p["offer_id"]) for p in plans if p.get("offer_id")}
    known = set(
        Offer.objects.for_tenant(sale.store.tenant_id)
        .filter(pk__in=bank_ids, layer=Offer.Layer.BANK)
        .values_list("pk", flat=True)
    )
    for payload in plans:
        SaleTender.objects.create(
            sale=sale,
            mode=payload["mode"],
            amount_paise=payload["amount_paise"],
            upi_state=payload.get("upi_state") or "",
            upi_reference=payload.get("upi_reference") or "",
            offer_id=payload.get("offer_id") if payload.get("offer_id") in known else None,
            reservation_id=reservation_id if payload["mode"] == SaleTender.Mode.ADVANCE else None,
            special_order_id=special_order_id
            if payload["mode"] == SaleTender.Mode.ADVANCE
            else None,
            gift_voucher=vouchers[gift_vouchers.normalise_number(payload["gift_voucher"])]
            if payload["mode"] == SaleTender.Mode.GIFT_VOUCHER
            else None,
        )


def _b2b_tax_kind(buyer_gstin: str, store: Store) -> str:
    """Which tax split a bill carries, from the GSTIN's own state code.

    The buyer's state is the first two digits of their GSTIN. Same state as the
    store's registration means CGST + SGST; a different one means IGST. Bihar and
    Jharkhand are separate registrations, so this is an everyday branch here and
    not a corner case.

    The characters are taken as typed - a GSTIN that fails `gstin.describe` still
    gets a split out of its first two, and is flagged for a human rather than
    re-taxed. The till printed the customer's copy from these same two characters
    minutes ago (`till/gstin.ts`), and a server that quietly chose differently
    would put one tax on the paper and another in the books.
    """
    if not buyer_gstin:
        return Sale.B2bTaxKind.NONE
    if gstin_state_code(buyer_gstin) == store.gstin.state_code:
        return Sale.B2bTaxKind.CGST_SGST
    return Sale.B2bTaxKind.IGST


def _apply_b2b(sale: Sale, store: Store, data: dict[str, Any]) -> list[str]:
    """Step 11 - a GSTIN on the bill puts a 30-day clock on head office.

    Above the e-invoice threshold every GSTIN-bearing counter sale is legally a
    B2B invoice that must receive an IRN inside 30 days or the customer loses
    their input credit. The store cannot do that and should not be asked to: the
    deadline rides as data into a head-office queue (Rule 11).

    The split itself was derived before the bill was posted. Two things are
    checked here, and neither of them can stop a bill that is already paid for:

    * **Is the registration well formed at all** (#187). A cashier mistyping one
      character of fifteen has made a tax invoice head office must correct - it
      still lands, and the flag carries the reason so a clerk can ring the
      customer or fix a typo without re-deriving the check.
    * **Did the till print the same split**. Where it did not, the bill lands and
      the disagreement is flagged for the daily check.
    """
    if not sale.buyer_gstin:
        return []
    IrnQueueItem.objects.create(
        sale=sale, due_on=timezone.localdate(sale.billed_at) + timedelta(days=IRN_DUE_DAYS)
    )
    flags = []
    malformed = gstin_describe(sale.buyer_gstin)
    if malformed:
        flags.append(
            _flag(
                sale,
                store,
                ContinuityFlag.Kind.GSTIN_INVALID,
                {"gstin": sale.buyer_gstin, "reason": malformed},
            )
        )
    declared = data.get("b2b_tax_kind") or ""
    if declared and declared != sale.b2b_tax_kind:
        flags.append(
            _flag(
                sale,
                store,
                ContinuityFlag.Kind.GST_MISMATCH,
                {"printed_split": declared, "derived_split": sale.b2b_tax_kind},
            )
        )
    return flags


def _advisory_gst_check(sale: Sale, store: Store, lines: list[_PreparedLine]) -> list[str]:
    """Step 12 - does the tax charged match the tax version in force that day?

    Advisory by design. The bill is printed and the customer has gone; what this
    buys is the daily check knowing which bills to look at, not a refusal nobody
    could act on. A rupee a line is the threshold (B3).

    Only the sold lines are checked, and the arithmetic itself is
    `sell.services.recompute` - shared with the nightly re-run (#188), so a bill
    passed at the counter and a bill reported at midnight are answering the same
    question rather than two questions that happen to look alike.

    The version in force is the slab table (version 1) wherever the store's
    tax-settings switch is off - exactly the check this always was - and the
    newest saved version on or before the bill's day where it is on (ticket 03).
    Two more findings ride on it, both flags and never refusals:

    * `tax_version_mismatch` - the till taxed under a different version than the
      one in force (an offline counter that had not yet received a new one, or a
      version number this server does not know);
    * `tax_rule_missing` - a sold line whose HSN no rule of the version covers.

    The offer half of the same step is `_advisory_offer_check`, below.
    """
    books = StoreTaxBooks(store)
    in_force = books.at(sale.billed_at)
    recorded = books.recorded(sale.tax_setting_version, sale.billed_at)
    # The arithmetic is judged by the version the till used, when this server
    # knows it; whether that was the right version is the mismatch flag below.
    rulebook = recorded or in_force
    rows = _written_rows(lines)
    flags: list[str] = []
    offenders = gst_offenders(rows, rulebook)
    if offenders:
        flags.append(_flag(sale, store, ContinuityFlag.Kind.GST_MISMATCH, {"lines": offenders}))
    if sale.tax_setting_version != in_force.version:
        flags.append(
            _flag(
                sale,
                store,
                ContinuityFlag.Kind.TAX_VERSION_MISMATCH,
                {
                    "used": sale.tax_setting_version,
                    "in_force": in_force.version,
                    "known": recorded is not None,
                },
            )
        )
    missing = rule_missing_lines(rows, rulebook)
    if missing:
        flags.append(_flag(sale, store, ContinuityFlag.Kind.TAX_RULE_MISSING, {"lines": missing}))
    return flags


def _advisory_offer_check(
    sale: Sale, store: Store, lines: list[_PreparedLine], rulebook: _Rulebook
) -> list[str]:
    """Step 12, the offer half - did the counter charge what the rulebook says?

    The daily applied-vs-rulebook check (D5 Q10) is the thing this feeds, and it
    is advisory for the same reason its GST twin is: the bill is printed and the
    customer has gone. What it buys is a store's morning queue knowing which bills
    to look at.

    The comparison is the *whole* discount against the rulebook's, not the till's
    `offer_evidence` against the rulebook's, and that is deliberate. A line where
    the counter gave nothing and the rulebook says the customer was owed ₹600 is
    exactly the finding this check exists for, and comparing evidence with
    evidence would miss it - a till that applied no offer also writes no evidence,
    so the two would agree about nothing and report nothing.

    The comparison itself is `sell.services.recompute`, shared with the nightly
    re-run for the reason its GST twin is: one question, asked twice, not two.
    What only this side can supply is `drift` - the lines whose discount was
    credited to the rule they *cite* rather than to today's reading of the book,
    which is a fact about the accept decision and not about the bill.
    """
    rows = _written_rows(lines)
    offenders = offer_offenders(
        rows,
        {row.line_no: rulebook.saving_for(row.line_no) for row in rows},
        rulebook.drift,
    )
    if not offenders:
        return []
    return [_flag(sale, store, ContinuityFlag.Kind.OFFER_MISMATCH, {"lines": offenders})]


def _advisory_after_discount_check(
    sale: Sale,
    store: Store,
    lines: list[_PreparedLine],
    rulebook: _Rulebook,
    data: dict[str, Any],
) -> list[str]:
    """Ticket 11 - did the till and the server reach the same bill, to the paisa?

    Only a bill priced with GST after discount is worked out again; any bill
    whose pricing differs from the store's switch is flagged too. Advisory, like
    the rest of step 12: the bill stands (`sell.services.after_discount_check`).
    """
    try:
        with transaction.atomic():
            findings = after_discount_check.check(
                sale, store, _written_rows(lines), rulebook.resolution, data["tenders"]
            )
    except Exception as exc:  # noqa: BLE001 - a check must never cost a printed bill
        logger.exception("GST after discount check failed on %s", sale.doc_number)
        findings = [
            (
                ContinuityFlag.Kind.TILL_SERVER_MISMATCH,
                {"error": f"the server could not work the bill out again: {type(exc).__name__}"},
            )
        ]
    return [_flag(sale, store, kind, details) for kind, details in findings]


def _return_tax(data: dict[str, Any]) -> bool:
    """Ticket 13: does this bill say its returns were taken by the return tax rules?"""
    return bool(data.get("return_tax")) and bool(data.get("exchange"))


def _bank_part(line: _PreparedLine) -> int:
    """Ticket 13 (B60): a piece coming back's bank-offer part, as the till sent it."""
    return int(line.payload.get("bank_offer_paise") or 0) if line.is_return else 0


def _reduces_no_tax(line: _PreparedLine) -> bool:
    """A leg that carries no tax where its rate would put some in: past the deadline."""
    payload = line.payload
    if payload["gst_paise"] != 0:
        return False
    return split_inclusive(line.value_paise, Decimal(payload["gst_rate"])).gst_paise > 0


def _record_return_tax(
    sale: Sale,
    store: Store,
    lines: list[_PreparedLine],
    original_bill: Sale | None,
    data: dict[str, Any],
    actor: Any,
) -> list[str]:
    """Ticket 13 - the credit note an exchange issues, and what the server makes of it.

    A bill with pieces coming back is flagged when it says otherwise than the
    store's switch. A bill taken by the return tax rules gets its credit note
    (numbered in the CN series where ticket 04's format applies), and flags for
    what Accounts must see: a piece past its credit-note deadline, a bank offer's
    part reversed (B60), and anywhere the till (perhaps offline, holding an older
    tax version) judged the deadline or the bank's part differently from the
    server. Never a refusal: the bill is printed.
    """
    returns = [line for line in lines if line.is_return and line.row is not None]
    if not returns:
        return []
    flags: list[str] = []
    said = _return_tax(data)
    switch = exchange_tax.return_tax_on(store)
    if said != switch:
        flags.append(
            _flag(
                sale,
                store,
                ContinuityFlag.Kind.RETURN_TAX_MISMATCH,
                {"bill": said, "store_switch": switch},
            )
        )
    if not said:
        return flags
    late = any(_reduces_no_tax(line) for line in returns)
    deadline, server_late, findings = _judge_returns(sale, store, returns)
    flags += [_flag(sale, store, kind, details) for kind, details in findings]
    if sum(line.value_paise for line in returns) <= 0:
        # Nothing of value came back (a free piece): no credit note to issue.
        return flags
    note, problems = exchange_tax.issue_credit_note(
        sale,
        store,
        [line.row for line in returns if line.row is not None],
        original_sale=original_bill,
        deadline=deadline,
        late=late,
        actor=actor,
    )
    for kind, details in problems:
        flags.append(_flag(sale, store, kind, details))
    original = original_bill.doc_number if original_bill is not None else None
    if note.late or server_late:
        # Past the deadline by the bill (no tax reduced) or by the server's own
        # judgement (the till reduced tax it should not have): Accounts sees both.
        flags.append(
            _flag(
                sale,
                store,
                ContinuityFlag.Kind.LATE_CREDIT_NOTE,
                {
                    "original": original,
                    "deadline": deadline.isoformat() if deadline else None,
                    "value_paise": int(note.value_paise),
                    "tax_reduced": int(note.gst_paise) > 0,
                },
            )
        )
    if note.bank_offer_paise:
        flags.append(
            _flag(
                sale,
                store,
                ContinuityFlag.Kind.BANK_SHARE_RETURNED,
                {"original": original, "bank_offer_paise": int(note.bank_offer_paise)},
            )
        )
    return flags


def _judge_returns(
    sale: Sale, store: Store, returns: list[_PreparedLine]
) -> tuple[date | None, bool, list[tuple[str, dict[str, Any]]]]:
    """The server's own view of each piece coming back: its credit-note deadline,
    whether it is past it, and where the till judged the deadline or the bank's
    part differently."""
    options = exchange_tax.options_at(store, sale.billed_at)
    deadline = None
    server_late = False
    deadlines: list[dict[str, Any]] = []
    banks: list[dict[str, Any]] = []
    for line in returns:
        if line.original is None:
            continue  # a paper-era original: nothing to judge against (flagged already)
        judged = exchange_tax.judge(
            line.original,
            line.qty,
            returned=_returned_before(line, returns),
            exchange_at=sale.billed_at,
            options=options,
        )
        deadline = judged.deadline
        server_late = server_late or judged.leg.late
        till_late = _reduces_no_tax(line)
        if till_late != judged.leg.late:
            deadlines.append(
                {
                    "line_no": line.payload["line_no"],
                    "till_late": till_late,
                    "server_late": judged.leg.late,
                    "deadline": judged.deadline.isoformat(),
                }
            )
        if _bank_part(line) != judged.leg.bank_offer_paise:
            banks.append(
                {
                    "line_no": line.payload["line_no"],
                    "till": _bank_part(line),
                    "server": judged.leg.bank_offer_paise,
                }
            )
    findings: list[tuple[str, dict[str, Any]]] = []
    if deadlines:
        findings.append((ContinuityFlag.Kind.CN_DEADLINE_MISMATCH, {"lines": deadlines}))
    if banks:
        findings.append((ContinuityFlag.Kind.BANK_SHARE_MISMATCH, {"lines": banks}))
    return deadline, server_late, findings


def _returned_before(line: _PreparedLine, returns: list[_PreparedLine]) -> tuple[int, int, int]:
    """What had come back off this leg's original line before this bill:
    ``(qty, paise, bank paise)``.

    Read after the bill's own legs are written, so they are taken back out: the
    leg is judged against what stood when the counter priced it.
    """
    assert line.original is not None
    qty, paise = returned_so_far(line.original)
    bank = exchange_tax.returned_bank_so_far(line.original)
    mine = [
        other
        for other in returns
        if other.original is not None and other.original.pk == line.original.pk
    ]
    return (
        qty - sum(other.qty for other in mine),
        paise - sum(other.value_paise for other in mine),
        bank - sum(_bank_part(other) for other in mine),
    )


def _written_rows(lines: list[_PreparedLine]) -> list[SaleLine]:
    """The `SaleLine` rows this bill actually wrote, in payload order."""
    return [line.row for line in lines if line.row is not None]


def _goods_rows(lines: list[_PreparedLine]) -> list[SaleLine]:
    """The written rows that are pieces of goods - never an alteration charge
    (ticket 22), which has no brand to fund a discount or share a margin."""
    return [row for row in _written_rows(lines) if not row.is_alteration]
