"""The two balanced value events a bill posts (#178, api-contract §Postings).

A bill moves money twice, and the two movements are different kinds of fact, so
they post as two balanced vouchers under the one document number rather than as
one long list of legs:

* **Event A - the money side.** What the customer paid, what we earned, what the
  state is owed. It posts for *every* bill, including one selling a piece whose
  paperwork has not arrived: the customer's money is a fact whatever the books
  know about the piece.
* **Event B - the cost side.** What the piece cost us, and which book it came out
  of. That depends on the brand's commercial model, and where the books cannot
  say, the line waits rather than posting a guess (Rule 5).

Keeping them apart is not tidiness. A bill can have a real money side and no
costable line at all, and the alternative - one voucher - would either refuse the
money or invent the cost.

Three rules run through everything here.

**`post_entries` or nothing.** Balanced-or-fail, integer paise, one writer, no
parallel books. The store-scoped till may post these accounts and no others; the
carve-out and its reasoning live in `core.floors`.

**The bill's own numbers, never recomputed.** The line stores the tax the customer
was actually charged, so the revenue base is `net - gst` by subtraction. Base plus
tax is then exactly what was paid, on every line, and no rounding can drift
between the printed bill and the ledger. Whether that tax was the *right* tax is a
different question, asked and flagged separately (accept step 12): the books
record what happened, not what should have.

**Never at zero (Rule 5).** A leg worth nothing is not written, and a line the
books cannot price or cannot place is not posted at all - it waits in
`DeferredCosting`, where the Dashboard counts it and the daily check ages it.

Still CA-gated (CONTEXT.md F9): the once-only GST recognition on SOR/consignment
and the Tally-side treatment of that vendor accrual. Event B raises the payable
because the liability is real on the day the piece sells; what the statutory books
do with its tax is the CA's ruling and is not encoded here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.gl import GLAccount
from core.posting import Leg, cr, dr, post_entries
from finledger.posting import post_sale_collection, post_sale_sor_liability
from masters.ownership import brand_is_owned
from sell.models import DeferredCosting, Return, Sale, SaleLine, SaleTender
from sell.services.movements import SellDocument, SellLine
from stockledger.models import StockLedgerEntry
from vendors.models import Vendor

if TYPE_CHECKING:
    from masters.models import Brand

#: `core.posting.dr` or `core.posting.cr` - which side of a pair a leg goes on.
LegWriter = Callable[..., Leg]


@dataclass(frozen=True)
class TenderPosting:
    """Where one way of paying goes, in both books.

    One table rather than two maps, so the credit note's carve-out is stated once:
    it has a value account (the liability it extinguishes) and deliberately no cash
    account, because no money moved. Two maps would leave a mode that had drifted
    out of one of them silently untendered or silently uncollected.
    """

    #: The value account. Card and UPI are *clearing* accounts, not CASH: the
    #: customer has paid but the money has not reached the bank, and only keeping
    #: them apart lets a store's drawer be counted against CASH alone.
    value_account: str
    #: The cash-ledger account the collection is a receipt into, for the store's
    #: cash summary - or empty where nothing was collected.
    cash_account: str = ""


TENDERS: dict[str, TenderPosting] = {
    SaleTender.Mode.CASH: TenderPosting(GLAccount.CASH, "CASH"),
    SaleTender.Mode.CARD: TenderPosting(GLAccount.CARD_CLEARING, "CARD"),
    SaleTender.Mode.UPI: TenderPosting(GLAccount.UPI_CLEARING, "UPI"),
    SaleTender.Mode.CREDIT_NOTE: TenderPosting(GLAccount.CREDIT_NOTE_LIABILITY),
    # Ticket 11: owed by the bank, and no money moved at the counter, so no
    # cash-ledger receipt - the drawer is never counted against it.
    SaleTender.Mode.BANK_OFFER: TenderPosting(GLAccount.BANK_OFFER_RECEIVABLE),
    # Ticket 20: a reservation's advance, taken into the drawer (and the cash
    # ledger) when its receipt voucher was issued. Used now, it gives up the
    # customer advance it was held as; no money moves at the counter.
    SaleTender.Mode.ADVANCE: TenderPosting(GLAccount.CUSTOMER_ADVANCE),
    # Ticket 19: a gift voucher, paid for into the drawer (and the cash ledger)
    # when it was sold. Spent now, it gives up the voucher liability; no money
    # moves at the counter.
    SaleTender.Mode.GIFT_VOUCHER: TenderPosting(GLAccount.GIFT_VOUCHER_LIABILITY),
}


@dataclass(frozen=True)
class CostPlan:
    """How one bill line's cost posts - or why it cannot yet.

    Exactly one of the two is ever set, which is why `book` is the field and
    `postable` reads off it rather than off a separate flag: a plan that claimed
    both to be brand-owned and to be waiting would be a state nothing could act
    on. `book` is also what gets written to the line, so an exchange reading it
    back later sees exactly what this bill posted.

    `deferral` carries the `DeferredCosting.Reason` naming what the books are
    waiting for, which is what lets the sweep (#186) tell a piece waiting for its
    *price* from one waiting for its *brand* - the first has not moved stock yet,
    the second has.
    """

    #: A `SaleLine.CostBook` value, or empty when nothing can post.
    book: str = ""
    vendor: Vendor | None = None
    deferral: str = ""

    @property
    def postable(self) -> bool:
        return bool(self.book)

    @property
    def owned(self) -> bool:
        return self.book == SaleLine.CostBook.OWN


@dataclass(frozen=True)
class CostedLine:
    """One written line and the cost plan resolved for it."""

    row: SellLine
    plan: CostPlan
    #: What this line's own pieces actually cost, where a per-unit rate cannot say
    #: it: at a goods-v1 store one line can consume two receipt layers at two
    #: costs, and `unit_cost_paise × qty` would then post a rounded average rather
    #: than the money. The allocations on the line carry the truth and this is
    #: their exact sum. `None` at a legacy store, where the cohort's one rate is
    #: the whole answer (OPS-07).
    cost_override: int | None = None

    @property
    def is_return(self) -> bool:
        return self.row.is_return

    @property
    def cost_paise(self) -> int:
        return self.row.cost_paise if self.cost_override is None else self.cost_override


# --- which book the piece came out of --------------------------------------


def plan_from_original(original: SaleLine) -> CostPlan:
    """The plan the *original* sale posted under, read back off its line.

    An exchange unwinds a posting that already happened, so it has to unwind the
    one that actually happened - not one re-derived from today's masters. A brand
    flipped from SOR to Outright between the sale and the exchange would otherwise
    reverse a brand-owned piece into our own inventory and leave the brand still
    owed for it; a brand that has since gained a second supplier would credit the
    wrong one. In both cases the trial balance stays at zero and the subledger tie
    still passes, so nothing downstream would ever notice - which is why the model
    and the vendor are columns on the line (Rule 3) and this reads them.

    A line the books never placed has no cost event to reverse, and says which of
    the two silences it was so the return waits for the same thing the sale does.
    """
    if int(original.unit_cost_paise or 0) <= 0:
        return CostPlan(deferral=DeferredCosting.Reason.UNPRICED)
    if not original.cost_book:
        return CostPlan(deferral=DeferredCosting.Reason.MODEL_UNKNOWN)
    if original.cost_book == SaleLine.CostBook.OWN:
        return CostPlan(book=SaleLine.CostBook.OWN)
    if original.cost_vendor is None:
        return CostPlan(deferral=DeferredCosting.Reason.VENDOR_UNKNOWN)
    return CostPlan(book=SaleLine.CostBook.BRAND, vendor=original.cost_vendor)


def resolve_cost_plan(*, brand: str, barcode: str, season: str, unit_cost_paise: int) -> CostPlan:
    """Where this piece's cost posts from, or what is missing.

    Asked of a piece being *sold*. A piece coming back reads the plan off the line
    it gives back instead (`plan_from_original`); the only returns that reach here
    are paper-era ones whose original bill we do not hold, where a re-derivation is
    all there is.

    Three things must be known before a cost event can be written, and each of
    them refuses rather than guesses:

    1. **What it cost.** No price of record means the piece was sold before its
       paperwork arrived (grill Q5).
    2. **Whose it was.** `brand_is_owned` answers `None` when the masters have
       never heard of the brand on the bill, and `None` is not "ours": booking a
       brand's stock into INVENTORY would understate what we owe them and
       overstate what we hold, which is the model-blind-liability defect of 30
       June wearing a different hat.
    3. **Who is owed**, for brand-owned stock only. A payable with no
       counterparty is not machine-computed, it is a guess - and the posting floor
       refuses it anyway (`core.floors`), so naming the supplier here is the
       difference between a deferred line and a 500.
    """
    if unit_cost_paise <= 0:
        return CostPlan(deferral=DeferredCosting.Reason.UNPRICED)
    owned = brand_is_owned(brand)
    if owned is None:
        return CostPlan(deferral=DeferredCosting.Reason.MODEL_UNKNOWN)
    if owned:
        return CostPlan(book=SaleLine.CostBook.OWN)
    vendor = supplier_of(barcode=barcode, season=season, brand=brand)
    if vendor is None:
        return CostPlan(deferral=DeferredCosting.Reason.VENDOR_UNKNOWN)
    return CostPlan(book=SaleLine.CostBook.BRAND, vendor=vendor)


def resolve_goods_cost_plan(*, brand_id: int | None, unit_cost_paise: int,
                            brands: Mapping[int, Brand] | None = None) -> CostPlan:
    """Goods ownership is established by its stable tenant-owned brand.

    Opening sources do not yet pin a supplier for brand-owned merchandise.
    Such a source cannot acquire a payable recipient from a display label or
    from a legacy inward; it stays unresolved until its owner links the source.
    """
    from core.tenancy import require_tenant_id
    from masters.models import Brand

    if unit_cost_paise <= 0:
        return CostPlan(deferral=DeferredCosting.Reason.UNPRICED)
    if brand_id is None:
        return CostPlan(deferral=DeferredCosting.Reason.MODEL_UNKNOWN)
    tenant_id = require_tenant_id()
    brand = (brands.get(brand_id) if brands is not None
             else Brand.objects.filter(tenant_id=tenant_id, pk=brand_id).first())
    if brand is None or brand.pk != brand_id or brand.tenant_id != tenant_id:
        return CostPlan(deferral=DeferredCosting.Reason.MODEL_UNKNOWN)
    if brand.ownership == Brand.Ownership.OWNED:
        return CostPlan(book=SaleLine.CostBook.OWN)
    return CostPlan(deferral=DeferredCosting.Reason.VENDOR_UNKNOWN)


def supplier_of(*, barcode: str, season: str, brand: str) -> Vendor | None:
    """The vendor a brand-owned piece is owed to, or `None` if nobody can say.

    Asked of the piece before the brand, because the piece knows better: the
    inward that brought this cohort in was booked against one vendor, and that
    booking is the commercial arrangement the settlement will be made under. Two
    vendors can carry one brand, so the brand alone cannot answer.

    The lookup is deliberately not store-scoped. A cohort is a (barcode, season)
    pair for the whole chain, and by the time it sells the piece may have been
    transferred twice - the store holding it has no inward of its own to read, but
    the cohort's inwards are still in the ledger.

    All of the cohort's booked inwards are read, not the latest one, because the
    pieces are indistinguishable once they share a cohort: two vendors' inwards on
    one cohort means nobody can say whose piece just sold, and the latest booking
    is not an answer, it is a guess with a counterparty on it. A payable to the
    wrong vendor keeps the trial balance at zero and the subledger tie green, so
    nothing downstream would ever catch it - the line defers to a human instead,
    exactly as it does when the *brand's* suppliers are the ambiguity.

    Falling back to the brand's supplier when there is only one is the paper-era
    case: a piece whose PT predates the system, or a booking-less direct receipt.
    Where the brand has two suppliers and the piece has no inward, nothing here
    can choose between them either.
    """
    vendor_ids = set(
        StockLedgerEntry.objects.filter(
            sku_code=barcode,
            kind=StockLedgerEntry.Kind.PT_INWARD,
            booking__isnull=False,
            **({"season": season} if season else {}),
        ).values_list("booking__vendor_id", flat=True)
    )
    if len(vendor_ids) > 1:
        return None
    if vendor_ids:
        return Vendor.objects.get(pk=vendor_ids.pop())
    suppliers = list(Vendor.objects.filter(brands__name=brand, is_active=True).distinct()[:2])
    return suppliers[0] if len(suppliers) == 1 else None


# --- the two events --------------------------------------------------------


def post_sale_value(
    sale: Sale, costed: list[CostedLine], tenders: list[SaleTender], actor: Any
) -> None:
    """Post both value events for a bill the server has just accepted.

    Called inside the accept transaction, after the bill has its number and its
    tenders: event A needs to know how the money arrived, and `post_entries`
    needs a minted `doc_number` to hang the legs on.
    """
    _post_money_event(sale, costed, tenders, actor)
    post_cost_event(sale, costed, actor)
    _write_collections(sale, tenders, actor)


def post_return_value(ret: Return, costed: list[CostedLine], actor: Any) -> None:
    """Both value events for a plain return (#184, api-contract §Postings).

    The same two events a bill posts, each one running backwards, and one leg
    fewer than a bill has.

    **Event A' - the money side.** Revenue and the tax inside it go back at what
    the customer actually paid (D2), and the whole of it becomes a credit-note
    liability: the store now owes the customer that much, spendable here until it
    expires. The identity that balances it is short precisely because of what is
    missing::

        Σ refunds - Σ revenue given back - Σ tax given back = 0

    **There is no cash leg on this document, and there is no code path that could
    write one** (grill Q7). Nothing here reads a tender, because a `Return` has no
    tenders to read: returns are the best-documented theft channel at any POS, so
    the money stays inside the system until it is spent on a real bill. That is a
    property of the shape rather than of a check somebody could delete.

    No rounding leg either, and for a reason worth stating: a refund is a share of
    a figure the customer already paid to the paisa, so there is nothing to round
    to a rupee. A rounding line here would be the books inventing a few paise of
    income out of giving money back.

    **Event B' - the cost side**, reversed per the *original* piece's own model
    (`plan_from_original`, frozen onto the return line at draft time). A line the
    books never priced posts nothing at all: there is no cost event to unwind,
    and the caller flags it instead of guessing (Rule 5).
    """
    legs: list[Leg] = []
    refunded = 0
    for line in costed:
        refunded += line.row.value_paise
        base = line.row.value_paise - int(line.row.gst_paise or 0)
        legs += _revenue_legs(dr, line.row, base)
    if refunded:
        legs.append(
            cr(
                GLAccount.CREDIT_NOTE_LIABILITY,
                refunded,
                memo=f"Credit note issued against {ret.doc_number}",
            )
        )
    if legs:
        post_entries(ret, legs, posted_by=actor)
    post_cost_event(ret, costed, actor)


def _post_money_event(
    sale: Sale, costed: list[CostedLine], tenders: list[SaleTender], actor: Any
) -> None:
    """Event A - the money the bill took and the revenue it earned.

    The whole voucher balances by an identity worth stating, because it is what
    makes a bill with an exchange in it post as one event rather than two:

        Σ tenders + Σ refunds - Σ sales - credit note issued + rounding
            - bank offer part of pieces returned = 0

    A tender is money in. A refund leg gives revenue and its tax back. A bill that
    nets negative takes no money at all and hands the difference over as a credit
    note instead (grill Q7), which is the liability that closes the ring.
    """
    legs = [
        dr(
            TENDERS[tender.mode].value_account,
            int(tender.amount_paise or 0),
            memo=f"{tender.get_mode_display()} · {sale.doc_number}",
        )
        for tender in tenders
    ]
    for line in costed:
        # A refund gives back revenue and the tax inside it, at what the customer
        # actually paid on the original line (D2) - never at today's price or
        # today's rate.
        side = dr if line.is_return else cr
        base = line.row.value_paise - int(line.row.gst_paise or 0)
        legs += _revenue_legs(side, line.row, base)
    net = int(sale.net_paise or 0)
    if net < 0:
        legs.append(
            cr(
                GLAccount.CREDIT_NOTE_LIABILITY,
                -net,
                memo=f"Credit note issued against {sale.doc_number}",
            )
        )
    # Ticket 13 (B60): a piece coming back off a bill the bank partly paid. The
    # customer is credited only what they paid; the bank's part goes back
    # against what the bank owes us, since it will not pay for a returned piece.
    # The customer's tenders carry that part, so the voucher still balances.
    bank_back = sum(
        int(getattr(line.row, "bank_offer_paise", 0) or 0) for line in costed if line.is_return
    )
    if bank_back:
        legs.append(
            cr(
                GLAccount.BANK_OFFER_RECEIVABLE,
                bank_back,
                memo=f"Bank offer part of pieces returned · {sale.doc_number}",
            )
        )
    legs += _rounding_legs(sale)
    if legs:
        post_entries(sale, legs, posted_by=actor)


def _dims(row: SellLine) -> dict[str, str]:
    """What every leg about this line is filed under (Rule 3, snapshotted).

    Brand and season, because they are what every margin question is asked by -
    which is also why the revenue, tax and cost legs are written per line rather
    than as one summed pair: a leg that added two brands together could not answer
    any of them.
    """
    return {"brand": row.brand, "season": row.season}


def _sides(line: CostedLine) -> tuple[LegWriter, LegWriter]:
    """Which way round a pair goes: `out` is the piece leaving, `back` its counter
    entry. An exchange leg is the same pair the other way about."""
    return (cr, dr) if line.is_return else (dr, cr)


def _revenue_legs(side: LegWriter, row: SellLine, base_paise: int) -> list[Leg]:
    """The revenue and tax legs for one line.

    Ticket 22: a paid alteration is a service, so its value goes to alteration
    revenue rather than the sale of goods; its tax is output GST like any line's.
    """
    legs = []
    if base_paise:
        account = (
            GLAccount.ALTERATION_REVENUE
            if getattr(row, "kind", "") == SaleLine.Kind.ALTERATION
            else GLAccount.SALES_REVENUE
        )
        legs.append(side(account, base_paise, **_dims(row)))
    if row.gst_paise:
        legs.append(side(GLAccount.OUTPUT_GST, int(row.gst_paise or 0), **_dims(row)))
    return legs


def _rounding_legs(sale: Sale) -> list[Leg]:
    """The rupee-rounding line, on whichever side it belongs.

    Rounded up, the customer paid a few paise more than the lines came to and the
    difference is ours; rounded down, it is a cost of doing business. Both are the
    same account and the sign carries which.
    """
    rounding = int(sale.round_paise or 0)
    if not rounding:
        return []
    side = cr if rounding > 0 else dr
    return [side(GLAccount.ROUND_OFF, abs(rounding), memo="Bill rounding")]


def post_cost_event(
    doc: SellDocument, costed: list[CostedLine], actor: Any, *, against_voucher: str = ""
) -> bool:
    """Event B - what the pieces cost, out of the book that held them.

    Owned stock (Outright / Correction) comes out of INVENTORY at the cost frozen
    on the line. Brand-owned stock (SOR / Consignment) was never in INVENTORY at
    all: it sat behind the SOR memo pair, so selling it removes the memo *and*
    raises what we now owe the brand - at the settlement rate, which is the cost of
    record for the piece and never the discounted price the customer paid.

    An exchange leg reverses whichever of those the *original* piece was, at the
    original's own frozen cost, reading both off the original line's own columns
    (`plan_from_original`): the piece is that piece, whatever the brand's terms
    have become since.

    Called twice for some bills, which is why it is public. A line sold before its
    paperwork has no cost to post on the day, and the sweep (#186) calls this again
    when the PT prices it - a second balanced voucher under the same bill, written
    on the day the price arrived. That voucher carries `against_voucher`, and it is
    the only thing that tells the two apart: a COGS leg dated three days after the
    bill it belongs to is otherwise indistinguishable from one posted with it, and
    the difference is exactly what a books-health check has to be able to see.

    Answers whether anything was written, so a caller acting on the posting (the
    sweep marks its queue row) does not have to re-derive it.
    """
    postable = [line for line in costed if line.plan.postable and line.cost_paise]
    if not postable:
        return False
    legs: list[Leg] = []
    for line in postable:
        legs += (
            _own_cost_legs(line, against_voucher)
            if line.plan.owned
            else _sor_cost_legs(line, against_voucher)
        )
    post_entries(doc, legs, posted_by=actor)
    _accrue_sor_liability(doc, postable, actor)
    return True


def _own_cost_legs(line: CostedLine, against_voucher: str = "") -> list[Leg]:
    """KDPS-owned: the value leaves our own inventory and becomes cost of sale."""
    dims = {**_dims(line.row), "against_voucher": against_voucher}
    out, back = _sides(line)
    return [
        out(GLAccount.COGS, line.cost_paise, **dims),
        back(GLAccount.INVENTORY, line.cost_paise, **dims),
    ]


def _sor_cost_legs(line: CostedLine, against_voucher: str = "") -> list[Leg]:
    """Brand-owned: the liability accrues now, and the memo stock comes off.

    Four legs, two pairs. The first pair is the money that matters - cost of sale
    against a real payable to a named vendor. The second is the memo pair the
    inward raised so brand-owned pieces could be counted without being valued on
    our balance sheet; the piece has gone, so the memo goes with it.
    """
    dims = {**_dims(line.row), "against_voucher": against_voucher}
    out, back = _sides(line)
    # A postable brand-owned plan names its vendor by construction; the posting
    # floor refuses the leg without one anyway (`core.floors`).
    assert line.plan.vendor is not None
    return [
        out(GLAccount.COGS, line.cost_paise, **dims),
        back(
            GLAccount.VENDOR_PAYABLE,
            line.cost_paise,
            party_type="vendor",
            party_code=line.plan.vendor.code,
            **dims,
        ),
        # The inward raised the memo as Dr SOR_STOCK / Cr SOR_CONTRA, so removing
        # it is that pair the other way about.
        out(GLAccount.SOR_CONTRA, line.cost_paise, **dims),
        back(GLAccount.SOR_STOCK, line.cost_paise, **dims),
    ]


def _accrue_sor_liability(doc: SellDocument, postable: list[CostedLine], actor: Any) -> None:
    """Mirror each vendor's accrual into the vendor subledger.

    One row per vendor per bill, netted: a bill that sells one of a brand's pieces
    and takes another back owes the difference, and two rows that cancel each other
    would be noise on the vendor's account rather than detail.
    """
    per_vendor: dict[Vendor, int] = defaultdict(int)
    for line in postable:
        if line.plan.vendor is not None:
            per_vendor[line.plan.vendor] += -line.cost_paise if line.is_return else line.cost_paise
    for vendor, amount in per_vendor.items():
        post_sale_sor_liability(
            vendor,
            amount,
            f"SOR/consignment accrual on {doc.doc_number}",
            actor,
            reference=doc.doc_number or "",
        )


def _write_collections(sale: Sale, tenders: list[SaleTender], actor: Any) -> None:
    """The cash-ledger receipt rows behind the bill's tenders."""
    for tender in tenders:
        account = TENDERS[tender.mode].cash_account
        if not account:  # a credit note is not money moving
            continue
        post_sale_collection(
            account=account,
            amount_paise=int(tender.amount_paise or 0),
            doc_number=sale.doc_number or "",
            description=f"Counter sale {sale.doc_number}",
            mode=tender.mode,
            user=actor,
        )
