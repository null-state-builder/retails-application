// The money on the screen (#181, D10 §4).
//
// Everything the Billing page computes lives here, as plain functions over plain
// data, for one reason: the numbers this file produces are checked again by the
// accept pipeline days later, on a bill that has already been printed and paid
// for. A disagreement of one paise is not a rendering bug - it is a queue that
// halts with `TENDER_MISMATCH` and a customer's receipt nobody can post.
//
// So the arithmetic is written against `sell/services/accept.py` step 3, line by
// line:
//
//   gross    = Σ mrp × qty                     (sale lines)
//   discount = Σ disc
//   net      = Σ line net + round
//   GST      = Σ line GST
//   line     : mrp × qty − disc == net, and GST == net − base(net, rate)
//   tenders  : Σ amounts == net
//
// The offer engine (#183) sits inside `priceCart`, and its output is kept in a
// field of its own rather than folded into `disc_paise`. That separation is the
// whole point: `disc_paise` is what a *cashier* keyed in, and it is the thing
// the cap measures. Adding a rulebook discount into it would trip the manual
// policy on every offer in the shop; keeping the rulebook's saving
// out of it would understate the bill. So the line carries both, adds them for
// the customer, and sends the sum as the line's discount with the evidence to
// say where it came from.
//
// One thing this file still deliberately does not do: invent a price. A piece
// the books have no MRP for arrives as `null` (contract, step 3) and stops the
// bill until a human types the price off the tag, because a nought would post no
// revenue and no tax against a garment that left the shop.

import { usesOf } from "./giftVoucher";
import type { GiftVoucherHeld, GiftVoucherUse } from "./giftVoucher";
import { normaliseGstin, taxKindFor } from "./gstin";
import {
  lateReturnAsk,
  refundTotals,
  whyExchangeCannotClose,
  whyReplacementIsShort,
} from "./exchange";
import type { Exchange } from "./exchange";
import { localDay } from "./invoiceNumbers";
import { bankShares, creditNoteTotals, isLate, optionsAt, returnLeg } from "./returnTax";
import type { CreditNoteTotals } from "./returnTax";
import { whySplitIsWrong } from "./split";
import type { LineSplit } from "./split";
import { resolveOffers } from "./offers";
import type { BankOffer, Entitlement, LineOutcome, OfferCart } from "./offers";
import { covers } from "./pin";
import type { Authorisation } from "./pin";
import {
  LEGACY_TAX_VERSION,
  rateHundredths,
  splitInclusive,
  taxLine,
  taxVersionFor,
} from "./pricing";
import {
  emptyPayment,
  revalidateAllocation,
  splitOf,
  toTenders,
  whyPaymentCannotClose,
} from "./tender";
import type { Payment, TenderSplit } from "./tender";
import type {
  BillDraft,
  BillLine,
  NoOffer,
  OfferEvidence,
  StackedCredit,
  TillGstSlab,
  TillCustomer,
  TillItem,
  TillOffer,
  TillSeason,
  TillTaxSettings,
  TillTaxVersion,
} from "./types";

/** One row of the line grid, as the cashier has it so far. */
export interface CartLine {
  /** Stable across re-prices and re-sorts, so React and the season swap have
   *  something to hold that is not the barcode - two lines can still share one,
   *  an off-the-tag line and the cohort it turns out to be, or two seasons of
   *  the same design, since a rescan of the same cohort bumps `qty` rather than
   *  opening a second line (#244, grill Q1). */
  key: string;
  /** Ticket 22: a paid alteration's own line (`alteration.ts`). Absent on every
   *  piece of goods - and on every line held or restored from before the ticket. */
  kind?: "alteration";
  /** Ticket 22: the alteration's fixed rate, as head office sent it. */
  fixed_rate?: string;
  barcode: string;
  season: string;
  design: string;
  brand: string;
  item: string;
  size: string;
  color: string;
  hsn: string;
  no_discount: boolean;
  /** The ticket price of one piece, GST inclusive. Nought means nobody has
   *  recorded one and the counter must ask. */
  mrp_paise: number;
  /** The books had no price for this piece, so a human is typing one off the
   *  tag. It stays true once the price is typed, because it is a fact about how
   *  the line arrived rather than about what it currently says - and the price
   *  box has to survive the first keystroke that makes the price non-nought. */
  needs_price: boolean;
  qty: number;
  /** Keyed in by the cashier, over the whole line. */
  disc_paise: number;
  /** The staff record who sold this line (ticket 07). */
  salesperson: string | null;
  /** Ticket 08: the line shared with a second salesperson by percentage.
   *  Absent or null on an ordinary line - and on every line held or restored
   *  from before the ticket, which is why it is optional. */
  split?: LineSplit | null;
  /** The other seasons this barcode is known in - the one-tap "which season?". */
  alternatives: TillItem[];
  /** What the counter's copy said was on the shelf when this was scanned. */
  stock: number;
  /** What the cashier typed for a piece the counter has never heard of (#186).
   *  Empty on every ordinary line: this is the description *instead of* a
   *  cohort, not a note about one. */
  manual_desc: string;
  /** The scan found nothing at all, so this line is being billed off the physical
   *  tag. Kept apart from `needs_price`, which is the milder case of a piece the
   *  books *do* know and simply never priced: this one has no cost of record
   *  either, and the server parks its cost event until the paperwork lands. */
  sold_before_inward: boolean;
}

/** Ticket 20: the customer reservation this bill collects, as head office
 *  answered for it. Its advance pays towards the bill as an `advance` tender. */
export interface ReservationPickup {
  id: string;
  ref: string;
  advance_paise: number;
}

export interface Cart {
  lines: CartLine[];
  /** How the customer is paying - see `tender.ts`. */
  payment: Payment;
  /** The manager who authorised whatever on this bill needed authorising, as
   *  established by their PIN at this counter (#182). */
  authorisation: Authorisation | null;
  /** A piece being given back on this same bill (#184, grill Q7). Null on an
   *  ordinary sale, which is almost all of them.
   *
   *  It is not a `CartLine` with a minus on it, and that is deliberate: an
   *  exchange leg is priced at what the customer *paid* on another bill, carries
   *  no offer, no salesperson and no discount, and reaches no rulebook. Folding it
   *  into the line list would put a row through `priceLine` that every branch of
   *  that function would have to be taught to skip. */
  exchange: Exchange | null;
  /** Ticket 20: the reservation this bill collects. Absent or null on every
   *  other bill - and on every cart held or restored from before the ticket. */
  reservation?: ReservationPickup | null;
  /** Ticket 21: the special order this bill collects, the same shape. A bill
   *  carries a reservation or a special order, never both. */
  special_order?: ReservationPickup | null;
  /** Ticket 19: gift vouchers head office answered for, in the order they were
   *  taken. Absent on every cart held or restored from before the ticket. */
  gift_vouchers?: GiftVoucherHeld[];
}

/** A bill with nothing on it yet. */
export function emptyCart(): Cart {
  return { lines: [], payment: emptyPayment(), authorisation: null, exchange: null };
}

/** Apply the bill-level actor to the lines that deliberately have nobody yet.
 * A line-level choice is a fact the cashier made and always wins. */
export function inheritBillSalesperson(cart: Cart, salesperson: string | null): Cart {
  if (salesperson === null) return cart;
  return {
    ...cart,
    lines: cart.lines.map((line) => (line.salesperson === null ? { ...line, salesperson } : line)),
  };
}

export interface PricedLine extends CartLine {
  line_no: number;
  gross_paise: number;
  /** What the rulebook took off this line, kept apart from `disc_paise` so the
   *  cap measures the cashier and only the cashier. */
  offer_paise: number;
  /** The brand-layer rule that won - the sale line's FK. */
  offer_id: number | null;
  /** One entry per rule that reduced this line, each with its own share: what
   *  the chips on the screen say, and the only honest way to say it. */
  offer_credits: StackedCredit[];
  offer_evidence: OfferEvidence | NoOffer;
  net_paise: number;
  gst_rate: string;
  gst_paise: number;
  /** No rule of the bill's tax version covers this HSN (ticket 03): the line
   *  took the version's "no rule" rate and the server will flag the bill. */
  tax_rule_missing: boolean;
  /** The most of this line a cashier may knock off on their own (B2). */
  cap_paise: number;
  cap_percent: string;
  over_cap: boolean;
  /** A rulebook discount locks manual entry unless HO's stacking dial is on. */
  manual_discount_allowed: boolean;
}

export interface PricedBill {
  lines: PricedLine[];
  /** The tax settings version every line was taxed under (ticket 03). */
  tax_setting_version: number;
  /** Ticket 11: priced with GST after discount (the store's switch, as held).
   *  False: every figure below is exactly what it was before the ticket. */
  gst_after_discount: boolean;
  /** Ticket 11: bank instant discounts taken as payments, not price cuts. They
   *  leave every line's taxable value alone; each is a "bank offer" tender. */
  bank_offers: BankOffer[];
  bank_offer_paise: number;
  /** What the customer pays with money: the bill less its bank offers and any
   *  reservation advance. The payment panel allocates this, never `net_paise`. */
  payable_paise: number;
  /** Ticket 20: the reservation this bill collects, and the part of its advance
   *  the bill uses (never more than the bill). */
  reservation: ReservationPickup | null;
  /** Ticket 21: the special order this bill collects; its advance is
   *  `advance_paise` the same way. */
  special_order: ReservationPickup | null;
  advance_paise: number;
  /** Ticket 19: the gift vouchers on this bill and what each pays (after any
   *  advance, never more than is still owed), and what they pay together. */
  gift_vouchers: GiftVoucherUse[];
  gift_voucher_paise: number;
  pieces: number;
  gross_paise: number;
  discount_paise: number;
  /** The exchange on this bill, if there is one - carried through so the screen
   *  and `toDraft` read one object rather than two. */
  exchange: Exchange | null;
  /** What the pieces coming back are worth, and the tax inside that. Both are
   *  subtracted from the bill: the customer pays the non-negative difference. */
  refund_paise: number;
  refund_gst_paise: number;
  /** Ticket 13: the pieces coming back were taken by the exchange and return tax
   *  rules (the store's switch, as held). False: exactly as before the ticket. */
  return_tax: boolean;
  /** Ticket 13 (B60): the bank offer's part of what is coming back. The customer
   *  never paid it, so it is added to what they pay, not credited to them. */
  refund_bank_paise: number;
  /** Ticket 13: the credit note this exchange issues beside the new invoice. */
  credit_note: CreditNoteTotals | null;
  /** Gifts this bill has earned - a trolley at a token price, and the like. The
   *  counter scans them as ordinary lines; this is the prompt to do so. */
  entitlements: Entitlement[];
  /** What the lines come to before the bill is rounded. */
  subtotal_paise: number;
  round_paise: number;
  net_paise: number;
  gst_paise: number;
  /** The figure staff quote to the customer. */
  saved_paise: number;
  /** How the money is being put up, as the panel's boxes now stand - `cart.payment`
   *  after the repricing re-read it against this bill's own amount. */
  payment: Payment;
  /** How the money is being put up. */
  split: TenderSplit;
  /** Who authorised the exceptions on this bill, if anybody has. */
  authorisation: Authorisation | null;
}

export interface PricingWorld {
  seasons: TillSeason[];
  slabs: TillGstSlab[];
  /** The tax settings the counter holds (ticket 03). Absent or switched off:
   *  version 1, the slabs, exactly as before. */
  tax?: TillTaxSettings | null;
  offers: TillOffer[];
  /** This store's registration (ticket 13: whose credit-note deadline applies). */
  store?: { gstin: string } | null;
}

export interface PricingOptions {
  /** `SellPolicy.manual_discount_cap_percent`, as it came down in the dataset. */
  capPercent?: string;
  allowManualDiscountOnOfferLines?: boolean;
}

/** A scanned piece as a fresh line: one of it, no discount, no salesperson yet. */
export function addPiece(
  piece: TillItem,
  found: { stock: number; alternatives: TillItem[] },
  key = newKey(),
): CartLine {
  return {
    key,
    barcode: piece.barcode,
    season: piece.season,
    design: piece.design,
    brand: piece.brand,
    item: piece.item,
    size: piece.size,
    color: piece.color,
    hsn: piece.hsn,
    no_discount: piece.no_discount,
    // `null` means nobody has recorded a price. It becomes a nought here and a
    // refusal to close in `whyItCannotClose` - never a free garment.
    mrp_paise: piece.mrp_paise ?? 0,
    needs_price: piece.mrp_paise == null,
    qty: 1,
    disc_paise: 0,
    salesperson: null,
    alternatives: found.alternatives,
    stock: found.stock,
    manual_desc: "",
    sold_before_inward: false,
  };
}

/**
 * A piece the counter has never heard of, billed off the tag in its hand (#186).
 *
 * The whole of grill Q5 in one function: a scan that finds nothing is not a
 * refused customer. The garment is real, the price is printed on it, and the
 * books will catch up when the PT lands - so the line goes on the bill with a
 * description somebody types and a price somebody reads, and the server parks its
 * *cost* event rather than inventing one (Rule 5) or stopping the sale (Rule 8).
 *
 * Everything the books would normally supply is blank on purpose. No brand means
 * no offer can reach it, which is right: a rulebook rule is written about a brand,
 * an item or a design, and this line is none of them yet. No HSN means the tax
 * falls to the general apparel slab, which is what an apparel shop's unknown
 * garment is. And the barcode is kept exactly as scanned even though nothing
 * matches it - it is how the sweep finds this line when the cohort is priced.
 */
export function addManualPiece(barcode: string, key = newKey()): CartLine {
  return {
    key,
    barcode: barcode.trim(),
    season: "",
    design: "",
    brand: "",
    item: "",
    size: "",
    color: "",
    hsn: "",
    no_discount: false,
    mrp_paise: 0,
    // The price box is the point of this line, and it stays open: there is no
    // recorded MRP to fall back on and never will be until the paperwork lands.
    needs_price: true,
    qty: 1,
    disc_paise: 0,
    salesperson: null,
    alternatives: [],
    stock: 0,
    manual_desc: "",
    sold_before_inward: true,
  };
}

let keys = 0;

/** The one source of cart-line keys - a restore rekey (`draft.ts`) mints from
 *  this too, rather than a generator of its own, so a line restored from a
 *  crash and the next fresh scan can never be issued the same key. */
export function newKey(): string {
  return `l${(keys += 1)}`;
}

/**
 * A scanned piece, onto the bill (#244, grill Q1).
 *
 * Scanning the same tag twice bumps the line already carrying it rather than
 * opening a duplicate - "always, no toggle" was the ruling, so there is no
 * setting to turn this off. The match is the cohort, `(barcode, season)`, and
 * an off-the-tag line never matches: it has no cohort behind it, so the second
 * scan of a barcode `addManualPiece` billed is a different piece, not the same
 * one again.
 *
 * The matched line keeps its salesperson, discount and rate exactly as they stood
 * - a rescan is not an edit, and re-stamping today's default salesperson onto a
 * line somebody already credited would undo their pick. A piece needing a
 * different salesperson on its second unit is edited on the line, same as any
 * other line-level change.
 */
export function scanPiece(
  cart: Cart,
  piece: TillItem,
  found: { stock: number; alternatives: TillItem[] },
  salesperson: string | null,
): Cart {
  const match = cart.lines.find(
    (line) =>
      line.barcode === piece.barcode &&
      line.season === piece.season &&
      !line.sold_before_inward &&
      line.kind !== "alteration",
  );
  if (match) {
    return {
      ...cart,
      lines: cart.lines.map((line) => (line === match ? { ...line, qty: line.qty + 1 } : line)),
    };
  }
  return { ...cart, lines: [...cart.lines, { ...addPiece(piece, found), salesperson }] };
}

/**
 * A quantity as the counter may type it: whole pieces, never fewer than one.
 *
 * `<input type="number" step="1">` does not stop anybody typing "1.5" - `step`
 * is a validation hint, not a filter, and `e.target.value` hands the string over
 * regardless. A fraction here is not a display nuisance: `mrp × 1.5` puts a
 * float on the write path (ADR-0004), and the server's `IntegerField(min_value=1)`
 * then refuses the bill - after it has printed, with the whole queue behind it.
 */
export function qtyFrom(typed: string | number): number {
  const whole = Math.trunc(Number(typed));
  return Number.isFinite(whole) && whole >= 1 ? whole : 1;
}

/**
 * The most of a line a cashier may discount on their own.
 *
 * `int(Decimal(mrp × qty) × cap / 100)` is what `_check_discount_policy` works
 * out, and `int()` truncates. In hundredths of a percent so nothing here touches
 * a float: a cap that arrived as 7.499999 would put the counter and the server
 * on opposite sides of a refusal.
 */
export function capFor(mrpPaise: number, qty: number, capPercent: string): number {
  return Math.floor((mrpPaise * qty * rateHundredths(capPercent)) / 10_000);
}

/** The only manual value the counter writes from its discount cell. */
export function clampManualDiscount(paise: number, capPaise: number): number {
  return Math.min(Math.max(paise, 0), capPaise);
}

/**
 * The exchange with each leg worked out by the return tax rules (ticket 13).
 *
 * Read off the original bill the exchange carries (`original_bill`), so the
 * figures are that bill's evidence, never today's rule. An exchange restored
 * from a draft older than the ticket carries no such snapshot and keeps its
 * legs as they were priced.
 */
export function withReturnTax(exchange: Exchange, world: PricingWorld, day: string): Exchange {
  const bill = exchange.original_bill;
  if (!bill) return exchange;
  const options = optionsAt(world.tax, day);
  const gstin = bill.store_gstin || world.store?.gstin || "";
  const late = isLate(localDay(new Date(bill.billed_at)), day, gstin, options);
  const shares = bankShares(bill.lines, bill.bank_offer_paise ?? 0);
  return {
    ...exchange,
    lines: exchange.lines.map((leg) => {
      const line = bill.lines.find((row) => row.line_no === leg.original_line);
      if (!line) return leg;
      const worked = returnLeg(
        { ...line, bank_offer_paise: shares[line.line_no] ?? 0 },
        leg.qty,
        late,
      );
      return {
        ...leg,
        refund_paise: worked.refund_paise,
        gst_paise: worked.gst_paise,
        bank_offer_paise: worked.bank_offer_paise,
        late,
      };
    }),
  };
}

/**
 * Price the whole cart: the rulebook, then every line, then the bill.
 *
 * The rulebook runs over the **whole cart at once** and cannot be folded into
 * `priceLine`. A "spend ₹11,000 and take ₹1,000 off" measures across a brand's
 * lines and shares its reward back over them, and a "buy 2 get 1" gives away the
 * cheapest of three pieces that may be on three different rows - neither
 * question can be answered one line at a time.
 */
export function priceCart(
  cart: Cart,
  world: PricingWorld,
  day: string,
  options: PricingOptions = {},
): PricedBill {
  const capPercent = options.capPercent ?? "0.00";
  const allowManualDiscountOnOfferLines = options.allowManualDiscountOnOfferLines ?? false;
  // Ticket 11: GST after discount, only where this store's switch is on. Off,
  // the rulebook, the rounding and the tenders are exactly as before (B5).
  const afterDiscount = world.tax?.after_discount === true;
  const rulebook = resolveOffers(offerCart(cart, day), world.offers ?? [], { afterDiscount });
  const byLine = new Map(rulebook.lines.map((outcome) => [outcome.line_no, outcome]));
  // One tax version for the whole bill: the one in force on its day (ticket 03).
  const taxVersion = taxVersionFor(world.tax, day);
  const lines = cart.lines.map((line, index) =>
    priceLine(
      line,
      index + 1,
      world,
      taxVersion,
      day,
      capPercent,
      allowManualDiscountOnOfferLines,
      byLine.get(index + 1),
    ),
  );

  const gross = lines.reduce((n, l) => n + l.gross_paise, 0);
  const discount = lines.reduce((n, l) => n + l.disc_paise + l.offer_paise, 0);
  // What is given back comes off before the rounding, so the rupee-rounding line
  // is about the figure the customer actually settles - which is what the server
  // re-derives (`_check_totals`).
  // Ticket 13: where the switch is on, each piece coming back is worked out by
  // the return tax rules - its own bill's rate and value, no tax after the
  // credit-note deadline, the bank's part set aside (B60).
  const returnTax = world.tax?.return_tax === true && cart.exchange !== null;
  const exchange =
    returnTax && cart.exchange ? withReturnTax(cart.exchange, world, day) : cart.exchange;
  const refunds = refundTotals(exchange?.lines ?? []);
  const subtotal = lines.reduce((n, l) => n + l.net_paise, 0) - refunds.refund_paise;
  const round = roundingOf(
    subtotal,
    afterDiscount ? (taxVersion?.options?.round_total_paise ?? 100) : 100,
  );
  const net = subtotal + round;
  const bankOffers = afterDiscount ? (rulebook.bank_offers ?? []) : [];
  const bankPaise = bankOffers.reduce((n, offer) => n + offer.amount_paise, 0);
  // The bank pays its part; the customer is asked for the rest - and pays back
  // the bank's part of any piece coming back, which was never theirs (B60).
  const refundBank = returnTax ? refunds.bank_offer_paise : 0;
  // Ticket 20: a reservation's advance pays first, up to what is owed; any
  // advance left over is refunded from the Reservations screen.
  // Ticket 21: a special order's advance, the same way.
  const reservation = cart.reservation ?? null;
  const specialOrder = reservation ? null : (cart.special_order ?? null);
  const holder = reservation ?? specialOrder;
  const owed = net - bankPaise + refundBank;
  const advance = holder ? Math.max(0, Math.min(holder.advance_paise, owed)) : 0;
  // Ticket 19: gift vouchers pay next, each up to what it holds; what they do not
  // cover the customer pays. The tax is the goods', whatever pays for them.
  const vouchers = usesOf(cart.gift_vouchers ?? [], owed - advance);
  const voucherPaise = vouchers.reduce((n, voucher) => n + voucher.pays_paise, 0);
  const payable = owed - advance - voucherPaise;
  // Negative bills are refused by `whyItCannotClose`; resolving the raw figure
  // here keeps the payment arithmetic honest until that guard explains why.
  //
  // Every repricing re-reads the allocation against the amount it was made for
  // (PRD §9.1). A cart that has moved since the cashier said "All cash" carries
  // an allocation about a different bill, and this is where it goes back to
  // unallocated - so the panel a cashier is looking at is always about the
  // figure the customer is being asked for.
  const payment = revalidateAllocation(cart.payment, payable);
  const split = splitOf(payment, payable);

  return {
    lines,
    tax_setting_version: taxVersion?.version ?? LEGACY_TAX_VERSION,
    gst_after_discount: afterDiscount,
    bank_offers: bankOffers,
    bank_offer_paise: bankPaise,
    payable_paise: payable,
    reservation,
    special_order: specialOrder,
    advance_paise: advance,
    gift_vouchers: vouchers,
    gift_voucher_paise: voucherPaise,
    // Ticket 22: an alteration charge is a service, not a piece.
    pieces: lines.reduce((n, l) => n + (l.kind === "alteration" ? 0 : l.qty), 0),
    gross_paise: gross,
    discount_paise: discount,
    exchange,
    refund_paise: refunds.refund_paise,
    refund_gst_paise: refunds.gst_paise,
    return_tax: returnTax,
    refund_bank_paise: refundBank,
    credit_note: returnTax && exchange ? creditNoteTotals(exchange.lines) : null,
    entitlements: rulebook.entitlements,
    subtotal_paise: subtotal,
    round_paise: round,
    net_paise: net,
    // Rounding is not a discount and moves no tax: the GST total is the lines'
    // less whatever the returned pieces gave back, which is what the accept
    // pipeline re-derives (`_check_totals`).
    gst_paise: lines.reduce((n, l) => n + l.gst_paise, 0) - refunds.gst_paise,
    // What staff quote across the counter is everything off the ticket price -
    // the rulebook's part and the cashier's - because that is the number the
    // customer is comparing with the tags in their hand.
    saved_paise: discount,
    /** The payment after the repricing re-read it - what the panel draws and
     *  what the next keystroke patches. Not always `cart.payment`: an
     *  allocation about a bill that has since changed is cleared here. */
    payment,
    split,
    authorisation: cart.authorisation,
  };
}

/** The cart in the rulebook's own terms; line numbers are positions, as ever.
 *  Ticket 22: an alteration charge is left out - no offer reaches it, and it
 *  counts towards no spend threshold (the server leaves it out the same way). */
function offerCart(cart: Cart, day: string): OfferCart {
  return {
    day,
    lines: cart.lines.flatMap((line, index) =>
      line.kind === "alteration"
        ? []
        : [
            {
              line_no: index + 1,
              brand: line.brand,
              item: line.item,
              design: line.design,
              size: line.size,
              color: line.color,
              barcode: line.barcode,
              season: line.season,
              qty: line.qty,
              mrp_paise: line.mrp_paise,
              no_discount: line.no_discount,
            },
          ],
    ),
  };
}

/**
 * Every rule that reduced this line, each with the part *it* took off.
 *
 * The evidence records the winning brand rule by name and the total saving in
 * one field, with the add-ons that stacked on top listed separately. That is the
 * right shape to store - it is what the daily applied-versus-rulebook check
 * reads - and the wrong shape to show a cashier: browser QA found one chip
 * reading "Louis Philippe flat 30% · ₹1,172.17" where ₹1,172.17 was the 30%
 * *and* a storewide 5%, so the counter would have quoted the brand's offer as
 * half again as generous as it is.
 *
 * So the brand rule's own share is what is left after the add-ons, which is
 * arithmetic that cannot be ambiguous: the parts are recorded, and the whole is
 * recorded, so the remainder is the first part.
 */
export function creditsOn(evidence: OfferEvidence | NoOffer): StackedCredit[] {
  if (!("saved_paise" in evidence)) return [];
  const stacked = evidence.stack ?? [];
  const brandShare = evidence.saved_paise - stacked.reduce((n, s) => n + s.saved_paise, 0);
  const credits: StackedCredit[] = [];
  if (evidence.offer_id !== null && brandShare > 0) {
    credits.push({
      offer_id: evidence.offer_id,
      offer_name: evidence.offer_name,
      layer: evidence.layer,
      saved_paise: brandShare,
    });
  }
  return [...credits, ...stacked];
}

function priceLine(
  line: CartLine,
  line_no: number,
  world: PricingWorld,
  taxVersion: TillTaxVersion | null,
  day: string,
  capPercent: string,
  allowManualDiscountOnOfferLines: boolean,
  offer: LineOutcome | undefined,
): PricedLine {
  const gross = line.mrp_paise * line.qty;
  const offerPaise = offer?.discount_paise ?? 0;
  const net = gross - offerPaise - line.disc_paise;
  // A line that cannot be priced still has to render: the screen shows why and
  // refuses to close, rather than throwing on the way to telling anybody.
  // Ticket 22: an alteration charge is taxed at its own fixed rate, never by a
  // slab or a rule of the version.
  const alteration = line.kind === "alteration";
  const tax =
    net > 0 && line.qty > 0
      ? alteration
        ? { split: splitInclusive(net, line.fixed_rate ?? "0.00"), ruleMissing: false }
        : taxLine(taxVersion, world.slabs, line.hsn, net, line.qty, day, world.tax?.hsn_digits)
      : { split: { rate: "0.00", base_paise: 0, gst_paise: 0 }, ruleMissing: false };
  const split = tax.split;
  const cap = capFor(line.mrp_paise, line.qty, capPercent);
  const evidence = offer?.evidence ?? {};
  return {
    ...line,
    line_no,
    gross_paise: gross,
    offer_paise: offerPaise,
    offer_id: offer?.offer_id ?? null,
    offer_credits: creditsOn(evidence),
    offer_evidence: evidence,
    net_paise: net,
    gst_rate: split.rate,
    gst_paise: split.gst_paise,
    tax_rule_missing: tax.ruleMissing,
    cap_paise: cap,
    cap_percent: capPercent,
    // The cap measures the cashier, so it measures `disc_paise` alone. An offer
    // is head office's decision and was never the counter's to authorise.
    over_cap: line.disc_paise > cap,
    manual_discount_allowed: !alteration && (offerPaise === 0 || allowManualDiscountOnOfferLines),
  };
}

/**
 * What the bill moves to reach a whole rupee.
 *
 * Integer arithmetic and half-up, matching the shop: 50 paise goes up. The
 * result is always within the ±50 the accept pipeline's serializer allows,
 * which matters because that bound is what stops a rounding line being used to
 * take ₹500 less than the lines say.
 *
 * `unitPaise` is ticket 11's tax-version setting (100: the nearest rupee, the
 * baseline; 1: no rounding). The server's twin is `sell.pricing.round_off`.
 */
export function roundingOf(subtotalPaise: number, unitPaise = 100): number {
  if (unitPaise <= 1) return 0;
  const remainder = ((subtotalPaise % unitPaise) + unitPaise) % unitPaise;
  if (remainder === 0) return 0;
  return 2 * remainder >= unitPaise ? unitPaise - remainder : -remainder;
}

/** Change out of the drawer. Never negative: cash short of the cash tender is a
 *  bill that does not close, not a negative change. */
export function changeFor(receivedPaise: number, cashPaise: number): number {
  return Math.max(0, receivedPaise - cashPaise);
}

/**
 * Why Save & Print is not available, in a sentence for the counter - or "".
 *
 * Every one of these is a bill the server would refuse. Stopping here is not the
 * screen being fussy: a refused bill halts the whole queue behind it, on a
 * receipt the customer is already holding, so the honest place to catch it is
 * before the number is spent.
 *
 * Note what is *not* here. A count that says nought is not a reason (grill Q6) -
 * the piece is in the customer's hand, so the count was wrong, and the stocktake
 * settles it. Being offline is not a reason either; that is the designed state.
 */
export function whyItCannotClose(bill: PricedBill): string {
  if (!bill.lines.length && !bill.exchange) {
    return "Nothing on this bill yet. Scan a piece to start.";
  }
  // The counter's own refusals about the pieces coming back, before anything
  // about the ones going out: a leg worth nothing back is not something a
  // cashier can fix by scanning harder (#184).
  const exchange = whyExchangeCannotClose(bill.exchange);
  if (exchange) return exchange;

  const unpriced = bill.lines.find((line) => line.mrp_paise <= 0);
  if (unpriced) {
    return `Line ${unpriced.line_no} has no price. Type the price from the tag before saving.`;
  }
  // `LINE_UNRESOLVED` (422) on the server, and a terminal one: a barcode the
  // books cannot place and no words to say what it was is a bill the queue would
  // halt on, taking every bill behind it - after the customer had paid and walked
  // out with the receipt. The counter has to say what it sold (#186).
  const undescribed = bill.lines.find(
    (line) => line.sold_before_inward && !line.manual_desc.trim(),
  );
  if (undescribed) {
    return (
      `Line ${undescribed.line_no} is not in the system. ` +
      "Type what the garment is before saving."
    );
  }
  const negative = bill.lines.find((line) => line.net_paise < 0);
  if (negative) {
    return `Line ${negative.line_no} is discounted by more than it costs.`;
  }
  const overCap = bill.lines.find((line) => line.over_cap);
  if (overCap) {
    return (
      `Line ${overCap.line_no} is above Head Office's ${overCap.cap_percent}% ` +
      "manual discount limit."
    );
  }
  const manualOnOffer = bill.lines.find(
    (line) => !line.manual_discount_allowed && line.disc_paise > 0,
  );
  if (manualOnOffer) {
    return (
      `Line ${manualOnOffer.line_no} already has an offer; Head Office has turned ` +
      "manual discount stacking off."
    );
  }
  // §10.4: the replacement must cover the eligible original paid value, and there
  // is no refund tender and no store credit in this increment to settle a
  // shortfall with. Named with the figure rather than left as a principle - see
  // `exchange.whyReplacementIsShort`.
  const short = whyReplacementIsShort(
    bill.exchange,
    bill.lines.reduce((n, line) => n + line.net_paise, 0),
  );
  if (short) return short;
  if (bill.net_paise < 0) {
    return "The pieces going out must be worth at least what is coming back.";
  }
  if (bill.payable_paise < 0) {
    return "The bank offer comes to more than this bill. Take the pieces coming back on a bill of their own.";
  }
  // Found in browser QA of #185, and it is #181's rule rather than that ticket's:
  // `_resolve_line` refuses a line with no salesperson on it, and the counter was
  // letting one through. The bill printed, the customer paid, and the queue then
  // halted on a `VALIDATION` that no amount of retrying could clear - taking
  // every bill behind it with it, because the queue does not reorder itself to
  // get past a bill the server refuses. A per-line salesperson is locked in D10, so
  // this is a question the screen has to ask before the number is spent.
  // A line restored from before ticket 07 may still hold the retired list's
  // numeric id: it names nobody on the staff list, so it is asked again.
  const unattributed = bill.lines.find((line) => typeof line.salesperson !== "string");
  if (unattributed) {
    return `Line ${unattributed.line_no} has no salesperson. Pick who sold the piece.`;
  }
  // Ticket 08: a split the server would refuse is refused here, before the
  // number is spent - the same reason as the salesperson check above.
  for (const line of bill.lines) {
    if (!line.split) continue;
    const wrong = whySplitIsWrong(line.salesperson, line.split);
    if (wrong) return `Line ${line.line_no}: ${wrong}`;
  }
  return whyPaymentCannotClose(bill.split);
}

export interface BillIdentity {
  billedAt: string;
  /** Partial rather than a whole `TillCustomer`: the screen hands over whatever
   *  the strip has, and `toDraft` is what fills the three fields in. */
  customer?: Partial<TillCustomer>;
  /** The shop's own state (`storeStateCodeOf(world.store)`), or null when the
   *  counter has no identity yet.
   *
   *  Null is not "some other state" and must not be read as one - see `toDraft`,
   *  which refuses to raise a tax invoice it cannot put a tax on. */
  storeStateCode?: string | null;
}

/**
 * The bill as the till layer takes it: everything but its number.
 *
 * The tenders are what each mode **took**, not what the customer held out. What
 * goes into the drawer and what they are owed back are the counter's business;
 * what posts to CASH is what the sale was worth, and a bill that tendered ₹2,000
 * for a ₹1,499 sale would refuse to balance and would be ₹501 out if it did.
 *
 * The manager's authorisation rides along as the contract's `override`, naming
 * who and when. Only an authorisation that matches this exchange's original bill
 * and value is sent; a stale tap is not evidence for a different return.
 */
export function toDraft(bill: PricedBill, identity: BillIdentity): BillDraft {
  const lines: BillLine[] = bill.lines.map((line) => ({
    line_no: line.line_no,
    direction: "sale",
    // Ticket 22: only on an alteration charge, so every other line replays as before.
    ...(line.kind === "alteration" ? { kind: "alteration" as const } : {}),
    barcode: line.barcode,
    season: line.season,
    qty: line.qty,
    mrp_paise: line.mrp_paise,
    // The wire carries one discount per line, because that is what the bill
    // says and what the customer paid. Which part of it the rulebook is
    // answerable for is a separate question, and the server answers it by
    // re-resolving the cart itself - the evidence below is what the daily check
    // audits, never what the cap believes.
    disc_paise: line.offer_paise + line.disc_paise,
    net_paise: line.net_paise,
    gst_rate: line.gst_rate,
    gst_paise: line.gst_paise,
    salesperson: line.salesperson,
    // Ticket 08: only a split line carries shares, so every other line goes on
    // the wire exactly as it did and replays with the same fingerprint.
    ...(line.split && line.salesperson && line.split.with
      ? {
          shares: [
            { salesperson: line.salesperson, percent: line.split.first_percent },
            { salesperson: line.split.with, percent: line.split.second_percent },
          ],
        }
      : {}),
    offer_id: line.offer_id,
    offer_evidence: line.offer_evidence,
    // What the cashier typed for a piece nothing could place. Sent on every line
    // for one field's worth of noise and one fewer branch: the server reads it
    // only when the barcode resolves to nothing, which is exactly when it is set.
    manual_desc: line.manual_desc,
  }));
  // Normalised here rather than at the field, so what prints on the paper, what
  // the split was derived from, and what the server stores are one string.
  //
  // **A counter that does not know its own state does not raise a tax invoice.**
  // The split is derived from the buyer's state against the shop's, so with no
  // shop state there is no honest answer - and the two dishonest ones are both
  // worse than dropping the registration: printing IGST because the comparison
  // failed would put one tax on the customer's copy and another in the books,
  // and printing CGST + SGST would do the same the other way round. Without it
  // the bill is an ordinary retail sale, which is recoverable; a tax invoice
  // charging the wrong tax is not. The screen keeps the field disabled in this
  // state, so this is the backstop for a hold parked before the counter lost its
  // identity rather than something a cashier can walk into.
  const storeState = identity.storeStateCode || null;
  const gstin = storeState ? normaliseGstin(identity.customer?.gstin ?? "") : "";
  // The returned legs number on from the sold lines, because the server keys
  // every line of a bill by `line_no` and refuses a bill that uses one twice.
  const exchange = bill.exchange
    ? {
        original: { fy: bill.exchange.original.fy, till_seq: bill.exchange.original.till_seq },
        lines: bill.exchange.lines.map((leg, index) => ({
          line_no: lines.length + index + 1,
          barcode: leg.barcode,
          season: leg.season,
          qty: leg.qty,
          // What the customer paid on the original bill, and the tax inside it at
          // the rate that bill charged (D2). The server recomputes both and
          // refuses a bill where either is a paisa out.
          refund_paise: leg.refund_paise,
          gst_rate: leg.gst_rate,
          gst_paise: leg.gst_paise,
          reason: leg.reason,
          condition: leg.condition,
          original_line: leg.original_line,
          // Ticket 13 (B60): only when there is one, so every other bill goes on
          // the wire - and fingerprints on replay - exactly as before.
          ...(bill.return_tax && (leg.bank_offer_paise ?? 0) > 0
            ? { bank_offer_paise: leg.bank_offer_paise }
            : {}),
        })),
      }
    : undefined;
  return {
    billed_at: identity.billedAt,
    customer: {
      name: identity.customer?.name ?? "",
      mobile: identity.customer?.mobile ?? "",
      gstin,
    },
    // What the *customer's copy* says the tax split is. The server derives its
    // own from the same GSTIN and flags a disagreement rather than silently
    // preferring one (contract step 11) - which only works if the till says what
    // it printed, so this rides on every bill including the B2C ones, where it
    // is "none".
    b2b_tax_kind: taxKindFor(gstin, storeState ?? ""),
    lines,
    ...(exchange ? { exchange } : {}),
    // The tax settings version these lines were taxed under (ticket 03). The
    // server stores it and flags a bill taxed under a version not in force.
    tax_setting_version: bill.tax_setting_version,
    // Ticket 11: only when true, so a bill from a store with the switch off goes
    // on the wire - and fingerprints on replay - exactly as before.
    ...(bill.gst_after_discount ? { gst_after_discount: true } : {}),
    // Ticket 13: only when true, for the same reason.
    ...(bill.return_tax ? { return_tax: true } : {}),
    // Ticket 20: only on a pickup bill, for the same reason.
    ...(bill.reservation ? { reservation: bill.reservation.id } : {}),
    // Ticket 21: only on a special order's collection bill.
    ...(bill.special_order ? { special_order: bill.special_order.id } : {}),
    // Each bank offer is a payment the bank makes (ticket 11), first, then what
    // the customer paid with.
    tenders: [
      // Ticket 20: the reservation's advance, first - it was paid before.
      ...(bill.advance_paise > 0
        ? [{ mode: "advance" as const, amount_paise: bill.advance_paise }]
        : []),
      // Ticket 19: each gift voucher that pays, naming it.
      ...bill.gift_vouchers
        .filter((voucher) => voucher.pays_paise > 0)
        .map((voucher) => ({
          mode: "gift_voucher" as const,
          amount_paise: voucher.pays_paise,
          gift_voucher: voucher.number,
          gift_voucher_code: voucher.code,
        })),
      ...bill.bank_offers
        .filter((offer) => offer.amount_paise > 0)
        .map((offer) => ({
          mode: "bank_offer" as const,
          amount_paise: offer.amount_paise,
          offer_id: offer.offer_id,
        })),
      ...toTenders(bill.split),
    ],
    totals: {
      gross_paise: bill.gross_paise,
      discount_paise: bill.discount_paise,
      net_paise: bill.net_paise,
      gst_paise: bill.gst_paise,
      round_paise: bill.round_paise,
    },
    ...(bill.exchange &&
    bill.authorisation &&
    covers(bill.authorisation, [lateReturnAsk(bill.exchange)])
      ? {
          override: {
            user_id: bill.authorisation.user_id,
            kind: "late_return" as const,
            at: bill.authorisation.at,
          },
        }
      : {}),
  };
}
