// How the bill was paid (#182, D10 §4, grill Q4; store and warehouse
// operations PRD §9.1).
//
// The payment panel has three modes - cash, card and UPI - and a
// bill may use any of them together. Everything about the arithmetic here is
// decided by one fact at the other end: `accept.py` step 3 refuses a bill whose
// tenders do not come to `totals.net_paise` **exactly**, and a refused bill is a
// receipt already in a customer's hand and a queue that has stopped. So a split
// that does not add up is stopped at the counter, where a person can still fix
// it, and never allowed to become a printed bill.
//
// Three shapes here are decisions rather than convenience.
//
// **Nothing is paid until somebody says it is.** A bill starts with no tender
// allocated at all. Cash used to be "whatever is left of the bill", which made
// the ordinary all-cash sale a zero-keystroke sale - and also meant a bill could
// close as a cash sale that no person had ever said was one. A cashier who put
// half of it on card and walked away left the drawer answering for the other
// half. So the whole due amount is unallocated until an **All cash**, **All UPI**
// or **All card** tap, or an amount typed into a row, says where the money went.
// An all-cash sale is one tap rather than none, and the drawer is never volunteered.
//
// **Cash received is not the cash tender.** The tender is what the bill took;
// cash received is what the customer physically handed over, and the difference
// is change out of the drawer. Blank means *exact* - they handed over the cash
// tender and there is nothing to give back. An explicit nought is a different
// statement ("nothing was received") and blocks a bill whose cash row is taking
// money. Change is measured against the cash tender alone: a ₹1,000 note on a
// bill half paid by card is change on the cash half only.
//
// **An allocation is about the amount it was made against.** Every allocation
// carries the due it answered (`due_paise`). Scan another piece, change an offer,
// take a line off - and the allocation no longer answers the bill in front of
// the customer. It is cleared back to unallocated (the mode the cashier chose is
// kept, so re-allocating is one tap) rather than left on screen looking settled.

import type { UpiCharged } from "./payment";
import type { BillTender } from "./types";

export type TenderMode = "cash" | "card" | "upi";

/** The four amount boxes on the payment panel, as the rules name them. */
export type PaymentField = "cash" | "card" | "upi" | "cash_received";

/** How each mode reads to a person - on the customer's copy and on any screen
 *  that lists what a bill was paid with.
 *
 *  Here rather than in `receipt.ts`, where it started, because a second reader
 *  arrived: browser QA of #184 found the reprint screen showing a raw
 *  `credit_note` where the paper says "Credit note". The words belong with the
 *  modes, and one map means the paper and the screen cannot disagree about what
 *  a customer paid with. */
export const TENDER_WORDS: Record<string, string> = {
  cash: "Cash",
  card: "Card",
  upi: "UPI",
  credit_note: "Credit note",
  // Ticket 11: a bank instant discount taken as a payment, not a price cut.
  bank_offer: "Bank offer",
  // Ticket 20: a reservation's advance, paid earlier against a receipt voucher.
  advance: "Advance paid earlier",
  // Ticket 19: a gift voucher, sold earlier as its own document.
  gift_voucher: "Gift voucher",
};

/** How one tender row reads on paper and on screen: its mode's words, and the
 *  voucher's number where a gift voucher paid (ticket 19). */
export function tenderWords(tender: { mode: string; gift_voucher?: string }): string {
  const words = TENDER_WORDS[tender.mode] ?? tender.mode;
  return tender.gift_voucher ? `${words} ${tender.gift_voucher}` : words;
}

export interface Payment {
  /** What the cash row takes. `null` is an empty box - nothing allocated to
   *  cash - and so is `0`; the two differ only in what the box shows. */
  cash_paise: number | null;
  card_paise: number;
  upi_paise: number;
  /** Cash the customer physically handed over. Presentation only: what posts to
   *  CASH is what the bill took, and the difference is change out of the drawer.
   *
   *  `null` is blank, and blank means **exact** - the customer handed over the
   *  cash tender and there is no change. An explicit `0` is a different
   *  statement and is not blank. */
  cash_received_paise: number | null;
  /** The mode an "All …" tap put the whole bill on, kept when an allocation is
   *  cleared so the cashier's choice survives a change to the cart. Null while
   *  the rows are a split somebody typed. */
  mode: TenderMode | null;
  /** The bill this allocation was made against, or null before anything was
   *  allocated. See the header: an allocation is about its own due amount. */
  due_paise: number | null;
  /** Boxes holding something that is not an amount - a stray letter, a minus
   *  sign, a figure past the box's own ceiling. A box in this list keeps no
   *  earlier value in force; the bill is blocked until it is fixed. */
  invalid: PaymentField[];
  /** What the bank answered, if the QR charge card got an answer at all (#248).
   *  `null`/absent - which is every bill on the mock adapter - means the cashier
   *  is vouching for the UPI row themselves, and it goes up stamped `manual`. */
  upi_charge?: UpiCharged | null;
}

/** The bill's payment, resolved: what each mode takes and what is still unpaid. */
export interface TenderSplit {
  cash_paise: number;
  card_paise: number;
  upi_paise: number;
  /** What the three modes have been given between them. */
  allocated_paise: number;
  /** The bill less what is allocated. Positive = still to allocate; negative =
   *  allocated past the bill. Nought is the only figure that may close. */
  unallocated_paise: number;
  /** Everything the customer has put up. The same figure as `allocated_paise`,
   *  under the name the receipt and the day close have always used. */
  total_paise: number;
  /** The bill this split was resolved against (#246).
   *
   *  Carried rather than asked for again, because the caller cannot be trusted
   *  to reproduce it: `priceCart` resolves the split against `Math.max(net, 0)`,
   *  since a bill that owes the customer takes no tender at all, and a prefill
   *  computed from a raw `net_paise` on that bill would offer a figure the
   *  close-validation then refused. One field here is one fewer thing every
   *  caller has to remember. */
  net_paise: number;
  /** Bill less tendered. Positive = unpaid, negative = over-tendered. The same
   *  figure as `unallocated_paise`, under its older name. */
  balance_paise: number;
  /** Nobody has allocated anything at all yet, on a bill that wants paying. */
  nothing_allocated: boolean;
  /** What the customer handed over in notes, or null for blank - which means
   *  exactly the cash tender. */
  cash_received_paise: number | null;
  /** Cash back out of the drawer, against the cash *tender*, never the bill:
   *  a ₹1,000 note handed over on a bill half paid by card is change on the
   *  cash half only. */
  change_paise: number;
  /** A cash-received figure typed on a bill whose cash row takes nothing. Not a
   *  figure to ignore: somebody said money came in, and the bill says it did
   *  not, and one of the two is wrong. */
  cash_received_stranded: boolean;
  /** The customer handed over less than the cash row is taking. */
  cash_received_short: boolean;
  /** An explicit nought was typed against a cash row that is taking money. */
  cash_received_zero: boolean;
  /** Boxes holding something that is not an amount, carried through so the one
   *  close-validation can speak for them. */
  invalid: PaymentField[];
  /** The acquirer's reference for the UPI row, when the bank confirmed it
   *  through the charge card (#248) - and `null` whenever the cashier is the
   *  only one vouching for it, which is every bill until real hardware lands.
   *  Never blank when it is set; see `confirmedUpiOf`. */
  upi_confirmed: string | null;
}

export function emptyPayment(): Payment {
  return {
    cash_paise: null,
    card_paise: 0,
    upi_paise: 0,
    cash_received_paise: null,
    mode: null,
    due_paise: null,
    invalid: [],
    upi_charge: null,
  };
}

/** What the three rows have been given between them. */
export function allocatedOf(payment: Payment): number {
  return (payment.cash_paise ?? 0) + payment.card_paise + payment.upi_paise;
}

/**
 * Resolve a payment against the bill it is paying.
 */
export function splitOf(payment: Payment, netPaise: number): TenderSplit {
  const cash = payment.cash_paise ?? 0;
  const allocated = cash + payment.card_paise + payment.upi_paise;
  const received = payment.cash_received_paise;
  return {
    cash_paise: cash,
    card_paise: payment.card_paise,
    upi_paise: payment.upi_paise,
    allocated_paise: allocated,
    unallocated_paise: netPaise - allocated,
    total_paise: allocated,
    net_paise: netPaise,
    balance_paise: netPaise - allocated,
    nothing_allocated: allocated === 0 && netPaise > 0,
    cash_received_paise: received,
    // Blank is exact, so there is nothing to hand back - and a bill taking no
    // cash gives no change whatever was typed, because change comes out of the
    // cash the drawer took and that is nought.
    change_paise: received === null || cash <= 0 ? 0 : Math.max(0, received - cash),
    cash_received_stranded: received !== null && cash <= 0,
    cash_received_short: received !== null && cash > 0 && received < cash,
    cash_received_zero: received === 0 && cash > 0,
    invalid: payment.invalid,
    upi_confirmed: confirmedUpiOf(payment),
  };
}

/**
 * Put the whole bill on one mode - the **All cash**, **All UPI** and **All card**
 * taps (PRD §9.1).
 *
 * The three rows are replaced rather than added to: "all" is a statement about
 * the whole bill, and a tap that left a stray ₹200 on card would be a split
 * nobody asked for. Everything that was about the money now being somewhere
 * else goes with it - a bank stamp pinned to a UPI figure that no longer
 * stands, and a cash-received note on a bill that has stopped taking cash.
 */
export function allocateAll(payment: Payment, duePaise: number, mode: TenderMode): Payment {
  const due = Math.max(0, duePaise);
  return {
    ...payment,
    cash_paise: mode === "cash" ? due : null,
    card_paise: mode === "card" ? due : 0,
    upi_paise: mode === "upi" ? due : 0,
    upi_charge: mode === "upi" ? payment.upi_charge : null,
    cash_received_paise: mode === "cash" ? payment.cash_received_paise : null,
    mode,
    due_paise: due,
    invalid: [],
  };
}

/** The payment with every row emptied, and the cashier's chosen mode kept. */
export function clearAllocation(payment: Payment, duePaise: number): Payment {
  return {
    ...payment,
    cash_paise: null,
    card_paise: 0,
    upi_paise: 0,
    cash_received_paise: null,
    upi_charge: null,
    due_paise: Math.max(0, duePaise),
    invalid: [],
  };
}

/**
 * The allocation, re-read against what the bill now says (PRD §9.1).
 *
 * Called on every repricing, because every repricing can move the due amount:
 * a piece scanned, a line taken off, a season swapped, an offer that started at
 * noon. An allocation that no longer comes to the bill is not a figure to leave
 * on screen looking settled - the panel goes back to unallocated and the cashier
 * says again where the money went. The mode they chose is kept, so the ordinary
 * "scan one more thing" is one tap rather than a fresh decision.
 *
 * An untouched panel is not an allocation and is never "cleared": it simply
 * learns which bill it is standing in front of.
 */
export function revalidateAllocation(payment: Payment, duePaise: number): Payment {
  const due = Math.max(0, duePaise);
  if (payment.due_paise === due) return payment;
  const allocated = allocatedOf(payment);
  const untouched =
    allocated === 0 && payment.cash_received_paise === null && payment.invalid.length === 0;
  if (untouched || allocated === due) return { ...payment, due_paise: due };
  return clearAllocation(payment, due);
}

/**
 * Whether the UPI row on this payment is still one the bank confirmed (#248).
 *
 * A stamp is only good for the figure it was given about. The cashier charges
 * ₹1,499 through the QR, the bank confirms *that*, and then the customer changes
 * their mind and half of it goes on card - the reference now answers about a sum
 * nobody is being asked to pay, and a bill carrying it would tell head office a
 * bank confirmed a figure it has never seen. So the stamp is pinned to its
 * amount and falls away the moment the two disagree, which drops the row back to
 * `manual`: the cashier vouching for it, which is the truth.
 *
 * A blank reference is refused here for the same reason it is refused by the
 * server (`upi_state=confirmed` with no `upi_reference` is a `VALIDATION`): a
 * bill refused at the wire is a receipt already in a customer's hand and a queue
 * that has stopped.
 */
export function confirmedUpiOf(payment: Payment): string | null {
  const charge = payment.upi_charge;
  if (!charge || payment.upi_paise <= 0) return null;
  if (charge.amount_paise !== payment.upi_paise) return null;
  return charge.reference.trim() || null;
}

/** How a tender was proved, in the words the screen and the paper both use.
 *  A card row and a UPI row nobody's bank answered for are the cashier's own
 *  word, and they say so; only a charge the bank confirmed is "confirmed". */
export function tenderProofWords(upiState?: string | null): string {
  return upiState === "confirmed" ? "confirmed" : "recorded manually";
}

/**
 * What an empty tender box takes when the cashier taps into it (#246, grill Q4).
 *
 * The panel is built on one rule - what is still owed is always on screen, and
 * tapping a row fills it - so this is that figure: the bill less every row
 * somebody has actually filled in. Typing over what it fills is what makes a
 * split a split; there is no separate mode, and no other arithmetic.
 *
 * Deliberately **not** keyed by mode, unlike `prefillFor(split, mode)` as
 * design.md sketched it. The prefill only ever fires on a box standing empty,
 * an empty box has put up nothing, and so every mode is owed the same figure -
 * a `mode` parameter would advertise a difference that does not exist.
 *
 * Nothing here is money moving: the figure is *offered* into a box the cashier
 * can still overtype, and `whyPaymentCannotClose` judges the result exactly as
 * it did before.
 */
export function prefillFor(split: TenderSplit): number {
  return Math.max(0, split.unallocated_paise);
}

/** The explicit-tender patch behind the UPI and Card “rest” controls. */
export function restTenderPatch(
  split: TenderSplit,
  mode: "upi" | "card",
): Pick<Payment, "upi_paise"> | Pick<Payment, "card_paise"> {
  // `rest` finishes an already-started row; replacing its entered figure with
  // just the remainder would silently discard the first half of a split.
  const paise = (mode === "upi" ? split.upi_paise : split.card_paise) + prefillFor(split);
  return mode === "upi" ? { upi_paise: paise } : { card_paise: paise };
}

/** A confirmed UPI charge is fixed: the rest must go to another tender. */
export function canFillTenderRest(split: TenderSplit, mode: "upi" | "card"): boolean {
  return prefillFor(split) > 0 && (mode !== "upi" || split.upi_confirmed === null);
}

/** ₹100 and ₹500, in paise - the two notes an Indian counter is handed. */
const CHIP_STEPS = [10000, 50000];

/**
 * The quick-cash chips under the cash row: exact, then the next ₹100 and the
 * next ₹500 (grill Q4).
 *
 * `duePaise` is the **cash tender**, not `TenderSplit.balance_paise`. What the
 * customer is handing money against is what the cash row is taking, so a bill
 * whose cash row takes nothing offers no chips at all.
 *
 * Exact keeps its paise: it exists to close the change line to nought, and a
 * chip rounded to the rupee would leave a stray fifty paise behind. The round
 * figures are deduped, so a bill that is already ₹5,000 offers one chip rather
 * than the same one three times.
 */
export function cashChips(duePaise: number): number[] {
  if (duePaise <= 0) return [];
  const chips = [duePaise];
  for (const step of CHIP_STEPS) {
    const rounded = Math.ceil(duePaise / step) * step;
    if (!chips.includes(rounded)) chips.push(rounded);
  }
  return chips;
}

/** The six things the payment card's one balance line can be saying (#246,
 *  PRD §9.1 - `unallocated` is the state a fresh bill now opens in). */
export type BalanceTone = "unallocated" | "short" | "over" | "stranded" | "change" | "settled";

/** The one balance line, resolved: which of the six, in what words, on what
 *  figure. The figure is always positive - the words carry the direction. */
export interface BalanceStanding {
  tone: BalanceTone;
  says: string;
  paise: number;
}

/**
 * Where the money stands, as the one line under the tenders says it (#246).
 *
 * A rule rather than a rendering detail, and here rather than in the panel,
 * because getting it wrong is not cosmetic: the green line is an *instruction*
 * to open the drawer and hand notes back, and the only thing standing between a
 * cashier and doing that is which branch this picks.
 *
 * `unallocated` is where every bill now starts: the whole amount is on screen
 * with nothing said about where it is coming from. It is kept apart from
 * `short`, which is a split somebody has begun and not finished - the two need
 * different things done to them.
 *
 * The one that is easy to miss is `stranded`. `change_paise` is measured against
 * the **cash tender**, so a bill whose cash row has fallen to nought - the
 * cashier tapped a chip, then put the whole amount on card - still holds a
 * cash-received figure. Green there would tell a cashier to pay out of a drawer
 * that took nothing. It is a figure to clear, not change to give, so it is red
 * and says so.
 *
 * `over` is red for the same reason and one more: `whyPaymentCannotClose` is
 * about to refuse the bill, and a green line would say the sale is fine.
 */
export function balanceStandingOf(split: TenderSplit): BalanceStanding {
  if (split.nothing_allocated) {
    return { tone: "unallocated", says: "To allocate", paise: split.unallocated_paise };
  }
  if (split.unallocated_paise > 0) {
    return { tone: "short", says: "Still to allocate", paise: split.unallocated_paise };
  }
  if (split.unallocated_paise < 0) {
    return { tone: "over", says: "Over by", paise: -split.unallocated_paise };
  }
  if (split.cash_received_stranded) {
    return {
      tone: "stranded",
      says: "Cash received, but this bill takes none",
      paise: split.cash_received_paise ?? 0,
    };
  }
  if (split.change_paise > 0) {
    return { tone: "change", says: "Change to give", paise: split.change_paise };
  }
  return { tone: "settled", says: "Nothing left to pay", paise: 0 };
}

/**
 * Why this bill cannot be paid for, in a sentence for the counter - or "".
 *
 * Every one of these is a bill the server would refuse - most with
 * `TENDER_MISMATCH`, the cash-received ones with `CASH_RECEIVED`. Catching them
 * here is the difference between a cashier fixing a figure and a store person
 * unpicking a printed bill days later.
 *
 * The order is the order a person can act in: a box holding nonsense first,
 * because nothing below it can be believed while one is on screen; then where
 * the money is coming from; then what the customer physically handed over.
 */
export function whyPaymentCannotClose(split: TenderSplit): string {
  if (split.invalid.length) {
    return "One of the amounts is not a number. Correct it before saving.";
  }
  if (split.unallocated_paise > 0) {
    return "Allocate the payment. Say how the rest of this bill is being paid.";
  }
  if (split.unallocated_paise < 0) {
    return "Tenders do not equal the amount due. They come to more than the bill.";
  }
  if (split.cash_received_stranded) {
    return "Cash received is recorded, but this bill takes no cash. Clear it or allocate cash.";
  }
  if (split.cash_received_zero) {
    return "Cash received is zero. Type what the customer handed over, or clear the box.";
  }
  if (split.cash_received_short) {
    return "Cash received is less than the cash this bill is taking.";
  }
  return "";
}

/** The tenders as the bill carries them: every mode that took money, once.
 *
 *  A row of nought is not a tender and is left out - the server's own serializer
 *  drops them, and a `SaleTender` may not be written at zero (`ck_saletender_
 *  amount_positive`). */
export function toTenders(split: TenderSplit): BillTender[] {
  const rows: BillTender[] = [
    { mode: "cash", amount_paise: split.cash_paise },
    { mode: "card", amount_paise: split.card_paise },
    {
      mode: "upi",
      amount_paise: split.upi_paise,
      // The bank's word when the charge card got one, and nothing at all
      // otherwise - `stampManualUpi` below fills in the cashier's.
      ...(split.upi_confirmed
        ? { upi_state: "confirmed" as const, upi_reference: split.upi_confirmed }
        : {}),
    },
  ];
  return stampManualUpi(rows.filter((row) => row.amount_paise > 0));
}

/**
 * Stamp `manual` on every UPI row missing a stamp, and leave everything else
 * alone - a row `toTenders` already stamped `confirmed` off a charge the bank
 * answered (#248), and every non-UPI row.
 *
 * Two callers, both at the wire boundary (#241): `toTenders` stamps a bill as
 * it is built, and `transport.billBody` runs this over a bill already sitting
 * in the queue from before this build, which would otherwise halt on the
 * server's new refusal (Rule 5, flag never block) - a queued bill is a printed
 * bill, and a halted queue stays halted until a human clears it.
 */
export function stampManualUpi(tenders: BillTender[]): BillTender[] {
  return tenders.map((tender) =>
    tender.mode === "upi" && !tender.upi_state
      ? // The reference goes with the stamp, always: `upi_reference` without
        // `confirmed` is its own refusal at the server, and this function exists
        // precisely to keep bills it does not control off that cliff.
        { ...tender, upi_state: "manual", upi_reference: undefined }
      : tender,
  );
}
