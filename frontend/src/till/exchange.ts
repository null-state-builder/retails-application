// A piece coming back inside the bill in front of you (#184, D2).
//
// Every return is now a leg on the Sale being built at the counter. Every figure
// therefore has to be worked out offline and then survive the server checking it
// to the paisa (`accept._check_return_refund`).
//
// Two rules carry the whole file, and both are the server's rather than this
// screen's:
//
// **What comes back is what was paid** (D2), never today's price. A piece bought
// under a 30% offer comes back at what the customer actually handed over for it,
// which is the line's net divided by its pieces - and the *last* piece of a line
// settles the remainder of what has not yet been given back, so three pieces at
// ₹9.99 refund 333 + 333 + 334 rather than leaving a paisa in the books for ever.
// That is why an original line has to carry `returned_paise` as well as
// `returned_qty`: without the paise a second partial return is a paisa out, and
// the bill is refused after the receipt has printed.
//
// **The tax inside the refund is the tax the bill charged**, at the rate on the
// original line - not today's slab. A rate change between the sale and the
// exchange has nothing to do with what is being given back, and re-deriving it
// would leave the reversal a few paise away from the posting it unwinds.
//
import { LATE_RETURN } from "./pin";
import type { Ask, Authorisation } from "./pin";
import { splitInclusive } from "./pricing";

/** The bill an exchange gives back against, as the counter found it. */
export interface ExchangeOriginal {
  fy: string;
  till_seq: number;
  /** For the screen and the receipt. The server resolves the bill from the pair
   *  above, so this is what a person reads, never what anything looks up by. */
  doc_number: string;
}

/** One line of the original bill, as it stands now. The shape of
 *  `SaleLineReadSerializer`, narrowed to what an exchange actually needs. */
export interface OriginalLine {
  line_no: number;
  barcode: string;
  season: string;
  design: string;
  color: string;
  size: string;
  brand: string;
  item: string;
  hsn: string;
  qty: number;
  net_paise: number;
  gst_rate: string;
  gst_paise: number;
  manual_desc: string;
  direction: string;
  returned_qty: number;
  returned_paise: number;
  /** Ticket 13 (B60): what earlier returns took off this line's share of the
   *  bill's bank offers. Absent from an older server or draft: nought. */
  returned_bank_paise?: number;
}

/** One piece being given back on the bill in front of the cashier. */
export interface ExchangeLeg {
  /** Stable across re-prices, like a cart line's. */
  key: string;
  original_line: number;
  barcode: string;
  season: string;
  brand: string;
  item: string;
  design: string;
  size: string;
  qty: number;
  refund_paise: number;
  gst_rate: string;
  gst_paise: number;
  reason: string;
  condition: "good" | "damaged";
  /** How the piece reads on the screen and on the receipt - the books' words for
   *  it, or the cashier's where the line was billed off a tag (#186). */
  description: string;
  /** Ticket 13 (B60): the part of `refund_paise` the bank offer paid on the
   *  original bill - not the customer's to be credited. Set only by the return
   *  tax rules (`cart.priceCart`); absent otherwise. */
  bank_offer_paise?: number;
  /** Ticket 13: past the credit-note deadline, so the leg reduces no tax. */
  late?: boolean;
}

/** The whole exchange riding on a bill: which original, and which of its lines. */
export interface Exchange {
  original: ExchangeOriginal;
  lines: ExchangeLeg[];
  /** Original bill time, kept on the local cart so a restored in-progress
   * exchange still knows whether Save & Print needs the late-window manager. */
  billed_at?: string;
  /** The against-bill card, snapshotted with the cart so a refresh can restore
   * return picking without another network request. Omitted on older drafts. */
  original_bill?: {
    lines: OriginalLine[];
    billed_at: string;
    customer_name: string;
    customer_mobile: string;
    /** Ticket 05: whether the original was a B2B bill. Absent on older drafts. */
    buyer_gstin?: string;
    net_paise: number;
    local: boolean;
    /** Ticket 13: what the bank offers paid of the original, spread over its
     *  lines for B60, and the GSTIN it was issued under ("" or absent: this
     *  store's). Absent on older drafts. */
    bank_offer_paise?: number;
    store_gstin?: string;
  };
  /** The manager's late-window approval, when this exchange needed one. Optional
   *  so an exchange parked by an older till still restores safely. */
  authorisation?: Authorisation | null;
}

/** What the cashier has marked against one original line. Kept separate from
 * the money leg so changing a reason or condition never reimplements refund
 * arithmetic in the screen. */
export interface PickedReturn {
  qty: number;
  reason: string;
  condition: "good" | "damaged";
}

export type PickedReturns = Record<number, PickedReturn>;

/** Picker decisions carried by an exchange restored from the autosaved cart. */
export function pickedFromExchange(exchange: Exchange | null): PickedReturns {
  return Object.fromEntries(
    (exchange?.lines ?? []).map((line) => [
      line.original_line,
      { qty: line.qty, reason: line.reason, condition: line.condition },
    ]),
  );
}

/** The part of a found bill the return picker needs. `FoundBill` satisfies this
 * shape, but keeping the picker independent avoids making exchange arithmetic
 * depend on the transport that found the bill. */
export interface ReturnableBill {
  original: ExchangeOriginal;
  lines: OriginalLine[];
}

let legKeys = 0;

/** How much of a line is still returnable. */
export function returnableQty(line: OriginalLine): number {
  return Math.max(0, line.qty - line.returned_qty);
}

/** The five things a refund is worked out from - the same five
 *  `sell.services.refunds.refund_share` takes, and the shape of a row in
 *  `sell/vectors/refunds.json`. */
export interface RefundInputs {
  paid_paise: number;
  line_qty: number;
  returning: number;
  returned_qty: number;
  returned_paise: number;
}

/**
 * What `returning` pieces of a line are worth back, in whole paise (D2).
 *
 * The mirror of `sell.services.refunds.refund_share`, and it has to agree with
 * it exactly: the server recomputes this figure and refuses the bill outright
 * where the two differ, on a receipt already in a customer's hand. Neither is
 * the authority - `sell/vectors/refunds.json` is, and both suites read it, which
 * is the "two engines, one set of golden files" shape the offer rulebook and the
 * GSTIN checker already hold to.
 *
 * Two rules, and the second is the one that is easy to miss.
 *
 * A share is rounded **half-up**, in integers. `Math.round` happens to round
 * half-up for positive numbers, but it does it on a float - and the point of the
 * integer form is that it cannot be a paisa adrift on some bill nobody thought
 * about, which is what `pricing.ts` says about every other figure here.
 *
 * The **last** piece of a line settles the remainder of what has not been given
 * back, so the parts always sum to what the customer actually paid.
 */
export function refundShare(inputs: RefundInputs): number {
  const { paid_paise, line_qty, returning, returned_qty, returned_paise } = inputs;
  if (returned_qty + returning >= line_qty) return paid_paise - returned_paise;
  return Math.floor((2 * paid_paise * returning + line_qty) / (2 * line_qty));
}

/** `refundShare` for one line of an old bill, told what has already come back. */
export function refundFor(line: OriginalLine, qty: number): number {
  return refundShare({
    paid_paise: line.net_paise,
    line_qty: line.qty,
    returning: qty,
    returned_qty: line.returned_qty,
    returned_paise: line.returned_paise,
  });
}

/** A line of the original bill as a leg on the bill being rung up now. */
export function legFor(line: OriginalLine, qty: number, key = newLegKey()): ExchangeLeg {
  const refund = refundFor(line, qty);
  return {
    key,
    original_line: line.line_no,
    barcode: line.barcode,
    season: line.season,
    brand: line.brand,
    item: line.item,
    design: line.design,
    size: line.size,
    qty,
    refund_paise: refund,
    gst_rate: line.gst_rate,
    // The rate the bill charged, out of the refund - see the module note. The
    // server checks exactly this identity (`_check_line_arithmetic`).
    gst_paise: splitInclusive(refund, line.gst_rate).gst_paise,
    reason: "",
    condition: "good",
    description: describeOriginal(line),
  };
}

/** The marked quantities as return legs on the Sale being built now. */
export function legsFrom(found: ReturnableBill | null, picked: PickedReturns): ExchangeLeg[] {
  if (!found) return [];
  return found.lines
    .filter((line) => (picked[line.line_no]?.qty ?? 0) > 0)
    .map((line) => {
      const choice = picked[line.line_no];
      return {
        ...legFor(line, choice.qty, `x${line.line_no}`),
        reason: choice.reason,
        condition: choice.condition,
      };
    });
}

/** Mark one scanned original piece, capped at what the customer can still
 * return after all earlier returns and exchanges. */
export function markReturnedPiece(
  found: ReturnableBill,
  picked: PickedReturns,
  scanned: string,
): { picked: PickedReturns; error: string } {
  const barcode = scanned.trim();
  const line = found.lines.find(
    (candidate) =>
      candidate.barcode === barcode &&
      (picked[candidate.line_no]?.qty ?? 0) < returnableQty(candidate),
  );
  if (!line) {
    const belonged = found.lines.some((candidate) => candidate.barcode === barcode);
    return {
      picked,
      error: belonged
        ? "Every returnable piece with that barcode is already marked."
        : "That barcode is not on the bill loaded here.",
    };
  }
  const current = picked[line.line_no] ?? { qty: 0, reason: "", condition: "good" as const };
  return {
    picked: { ...picked, [line.line_no]: { ...current, qty: current.qty + 1 } },
    error: "",
  };
}

/** Mark every piece that remains returnable, preserving decisions already made
 * on a line. */
export function takeEverythingBack(
  found: ReturnableBill,
  picked: PickedReturns,
): PickedReturns {
  return Object.fromEntries(
    found.lines.flatMap((line) => {
      const qty = returnableQty(line);
      if (!qty) return [];
      const current = picked[line.line_no] ?? { qty: 0, reason: "", condition: "good" as const };
      return [[line.line_no, { ...current, qty }]];
    }),
  );
}

/** How a returned piece reads: the books' words, or the cashier's off the tag. */
export function describeOriginal(line: OriginalLine): string {
  const words = [line.brand, line.item, line.design, line.size].filter(Boolean).join(" · ");
  return words || line.manual_desc.trim() || line.barcode;
}

/** The one source of exchange-leg keys - see `cart.ts`'s `newKey` for why a
 *  restore rekey mints from this rather than a generator of its own. */
export function newLegKey(): string {
  return `x${(legKeys += 1)}`;
}

/** What the whole exchange gives back, the tax inside it, and (ticket 13, B60)
 *  the bank offer's part of it that the customer never paid. */
export function refundTotals(legs: ExchangeLeg[]): {
  refund_paise: number;
  gst_paise: number;
  bank_offer_paise: number;
} {
  return {
    refund_paise: legs.reduce((n, leg) => n + leg.refund_paise, 0),
    gst_paise: legs.reduce((n, leg) => n + leg.gst_paise, 0),
    bank_offer_paise: legs.reduce((n, leg) => n + (leg.bank_offer_paise ?? 0), 0),
  };
}

/** The exact late-window decision shown to a manager for this exchange. */
export function lateReturnAsk(exchange: Exchange): Ask {
  return {
    kind: LATE_RETURN,
    ref: exchange.original.doc_number,
    paise: refundTotals(exchange.lines).refund_paise,
    label: exchange.original.doc_number,
  };
}

/** Whether the original bill is beyond the cached local-calendar-day window. */
export function isPastReturnWindow(
  billedAt: string,
  windowDays: number,
  now: Date = new Date(),
): boolean {
  const billed = new Date(billedAt);
  if (Number.isNaN(billed.getTime())) return true;
  const localDay = (date: Date): number =>
    Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86_400_000;
  return localDay(now) - localDay(billed) > Math.max(0, windowDays);
}

/** Whether this bill can close with these legs on it - or why not.
 *
 *  Only the counter's own refusals; what the *bill* still needs is
 *  `cart.whyItCannotClose`, which calls this. */
export function whyExchangeCannotClose(exchange: Exchange | null): string {
  if (!exchange) return "";
  if (!exchange.lines.length) {
    return "No piece is being given back. Take the exchange off the bill, or pick a line.";
  }
  const unpriced = exchange.lines.find((leg) => leg.refund_paise <= 0);
  if (unpriced) {
    return `${unpriced.description} was billed at nothing, so there is nothing to give back for it.`;
  }
  return "";
}

// ── No refund, no store credit (PRD §10.4) ──────────────────────────────────
//
// **This increment has no refund tender and no store credit anywhere in the
// exchange flow, online or offline.** The customer-return-policy PRD §3.2 names
// three possible outcomes for a lower-value exchange - refund the difference,
// issue store credit, or disallow it - and the store and warehouse operations
// PRD §10.4 picks the third for this increment and leaves the other two as
// separate work.
//
// What that meant in practice, and what was *not* deleted:
//
//   · `BillTender.mode` is `cash | card | upi` and has never carried a refund
//     mode, so there is nothing to disable there.
//   · The counter's credit-note cache went with #273 (`db.ts` version 5) and the
//     entry and redemption paths at the till went with it. The server's
//     `CreditNote` model, `sell.services.refunds` and the historical `Return`
//     reader are untouched: they are what a store's existing history is made of,
//     and reading it back is not issuing a new one.
//   · `refundShare`/`refundFor` keep their names. They compute **what a returned
//     piece is worth against the bill it came off**, which is what the
//     replacement has to cover; no money leaves the drawer on their account.
//
// So the rule below is the whole of the enforcement at the counter, and
// `cart.whyItCannotClose` is where it is felt.

/** What the pieces coming back are worth, as the bill they came off priced them. */
export function eligibleReturnValue(exchange: Exchange | null): number {
  return exchange ? refundTotals(exchange.lines).refund_paise : 0;
}

/** Why this exchange may not close for value reasons, naming the shortfall.
 *
 *  §10.4: the replacement value must equal or exceed the eligible original paid
 *  value, and the customer pays any difference. A lower-value exchange is
 *  refused - there is no refund and no store credit to settle it with - and the
 *  refusal names the figure, because "pick something worth more" without saying
 *  how much more is a cashier guessing at a counter with a customer waiting. */
export function whyReplacementIsShort(
  exchange: Exchange | null,
  replacementPaise: number,
): string {
  const owed = eligibleReturnValue(exchange);
  if (!exchange || !exchange.lines.length || replacementPaise >= owed) return "";
  return `Pick replacement worth at least ${rupees(owed)}`;
}

/** Whole rupees where the paise are nought, two decimals otherwise. The counter
 *  reads this out loud to a customer, so ₹1,499 beats ₹1,499.00. */
export function rupees(paise: number): string {
  const whole = paise / 100;
  return `₹${whole.toLocaleString("en-IN", {
    minimumFractionDigits: paise % 100 ? 2 : 0,
    maximumFractionDigits: 2,
  })}`;
}
