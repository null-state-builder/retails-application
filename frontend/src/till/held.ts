// Parking a bill and picking it up again (#185, grill Q13).
//
// A hold is the one thing at this counter that is not a fact. Nothing is sold,
// nothing leaves the shelf, no number is spent - it is a cart, and a cart is what
// somebody is *thinking about* buying. So the whole module is a store of carts
// with two rules on it:
//
//   · **A hold is till-local.** It has to survive a dead line, because the person
//     who parked it is standing in front of the counter. The server copy is a
//     count on a Dashboard and nothing more (`sync.pushHeld`).
//   · **A retrieved hold is repriced.** Grill Q13 is explicit: a kept bill carries
//     to the next day at *that* day's offers. So the payload stores what the cart
//     was, not what it cost, and everything money-shaped is worked out again from
//     the counter's world at the moment it comes back.
//
// The second rule is why `restore` re-resolves every line against today's items
// rather than trusting what was parked. The one thing it keeps is a price a human
// typed off a tag: nobody but that person knows it, and re-deriving it would
// silently make a garment free.

import type { Cart, CartLine } from "./cart";
import type { HeldBill, TillDb } from "./db";
import { resolveScan } from "./lookup";
import type { ScanWorld } from "./lookup";
import { emptyPayment } from "./tender";
import type { Payment } from "./tender";
import type { TillCustomer, TillItem } from "./types";

/** One parked line: the cart's row without anything the counter can work out
 *  again. `alternatives` and `stock` are read off today's world on retrieval, so
 *  parking them would send a stale shelf up to the server and back. */
export type HeldLine = Omit<CartLine, "alternatives" | "stock">;

/** What a hold actually holds. Deliberately the cart and not the bill: there is
 *  no total here that anything reprices from, only the two figures the hold list
 *  reads so a person can tell one parked customer from another. */
export interface HeldPayload {
  lines: HeldLine[];
  customer: TillCustomer;
  /** Whatever the payment panel had on it, if the panel had got that far. */
  payment: Payment;
  /** What it came to when it was parked - shown in the list, never billed from.
   *  A kept bill reprices on retrieval and this is what it is compared against
   *  so the counter can be told the price moved. */
  net_paise: number;
  pieces: number;
}

/** The local calendar day, which is the only day a shop's close happens on.
 *
 *  Not `toISOString().slice(0, 10)`: that is UTC, and in IST it rolls over at
 *  half past five in the morning - so a bill parked at 8pm on Tuesday would be
 *  put to the store as "held since before today" while the same shift was still
 *  running. */
export function localDay(at: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}

/** The cart, as it goes into a hold. */
export function heldPayload(
  cart: Cart,
  customer: TillCustomer,
  totals: { net_paise: number; pieces: number },
): HeldPayload {
  return {
    lines: cart.lines.map(({ alternatives: _a, stock: _s, ...line }) => line),
    // The GSTIN rides along with the name and the mobile (#187): a bill parked
    // half-billed and picked up ten minutes later is the same customer's, and a
    // hold that dropped it would turn their tax invoice back into a retail bill
    // without anybody at the counter noticing.
    customer: { name: customer.name, mobile: customer.mobile, gstin: customer.gstin },
    payment: cart.payment,
    net_paise: totals.net_paise,
    pieces: totals.pieces,
  };
}

export interface RestoredHold {
  cart: Cart;
  customer: TillCustomer;
  /** How many lines name a piece the counter no longer stocks - the price on
   *  those is the one that was parked, and the screen says so rather than
   *  pretending it is today's. */
  staleLines: number;
}

/**
 * A parked cart, as today's cart.
 *
 * Every line is looked up again: the ticket price, the shelf count and the other
 * seasons this barcode is known in all come from the world as it is now, which is
 * what "reprices at today's rules" means at the line level. Tax and the discount
 * cap follow on their own, because `priceCart` re-derives both from the day it is
 * given.
 *
 * A hand-typed price is the exception and keeps what the human typed. A piece the
 * counter has since stopped carrying keeps everything it was parked with - the
 * garment is real and is in somebody's hands, so the honest answer is the old
 * price and a word about it, not a line that vanishes.
 */
export function restoreHold(hold: HeldBill, world: ScanWorld): RestoredHold {
  const payload = hold.payload;
  let staleLines = 0;
  const lines: CartLine[] = (payload.lines ?? []).map((line) => {
    const found = resolveScan(line.barcode, world);
    const today = pieceInSeason(found.candidates, line.season);
    // A sold-before-inward line is not a piece the shop stopped carrying - it is
    // one the books never knew, and saying "check the lines the counter no longer
    // stocks" about it would send somebody looking for a change that never
    // happened (#186).
    if (!today && !line.sold_before_inward) staleLines += 1;
    return {
      ...line,
      // Holds outlive deploys: a cart parked by a build that had no manual line
      // in it comes back without these two, and `undefined` would reach the
      // close check as a description nobody typed. The type says they are always
      // there; the IndexedDB row on a counter that synced last week does not.
      manual_desc: line.manual_desc ?? "",
      sold_before_inward: line.sold_before_inward ?? false,
      ...(today ? refreshed(today, line) : {}),
      alternatives: found.candidates,
      stock: found.stock,
    };
  });
  return {
    cart: {
      lines,
      payment: payload.payment ?? emptyPayment(),
      authorisation: null,
      // A hold carries no exchange, deliberately. A hold is a *cart* - what
      // somebody is thinking about buying - and a piece the customer has handed
      // back is not that: they are standing there with it, and parking the bill
      // would leave a refund owed to somebody who has walked out. If they want to
      // pause, the exchange comes off the bill and is picked again.
      exchange: null,
    },
    customer: {
      name: payload.customer?.name ?? "",
      mobile: payload.customer?.mobile ?? "",
      gstin: payload.customer?.gstin ?? "",
    },
    staleLines,
  };
}

/** The same season if the counter still has it, else whatever it would resolve to
 *  now - a hold parked against a season that has since closed is still that
 *  piece, and the scan ladder's answer is the right one to fall back to. */
function pieceInSeason(candidates: TillItem[], season: string): TillItem | null {
  return candidates.find((row) => row.season === season) ?? candidates[0] ?? null;
}

/** What today's copy of the piece overwrites on a parked line. */
function refreshed(today: TillItem, line: HeldLine): Partial<CartLine> {
  return {
    season: today.season,
    design: today.design,
    brand: today.brand,
    item: today.item,
    size: today.size,
    color: today.color,
    hsn: today.hsn,
    no_discount: today.no_discount,
    // A price a person typed off the tag is theirs and survives; anything else is
    // the ticket price as the books have it today.
    mrp_paise: line.needs_price ? line.mrp_paise : (today.mrp_paise ?? 0),
    needs_price: line.needs_price || today.mrp_paise == null,
    // The paperwork landed while the bill was parked, so this is an ordinary line
    // now: the cohort prices it, the cost event posts with the bill, and nothing
    // goes into the costing queue at all.
    sold_before_inward: false,
  };
}

// --- the store of them -------------------------------------------------------

/** Every hold at this counter, oldest first - the order they were parked in is
 *  the order a person remembers them in. */
export async function listHolds(db: TillDb): Promise<HeldBill[]> {
  const rows = await db.held.toArray();
  return rows.sort((a, b) => a.held_at.localeCompare(b.held_at));
}

/** Park a cart. One write, and no other table is touched - a hold moves no
 *  stock, no money and no number, and that is enforced by there being nothing
 *  here that could. */
export async function parkHold(
  db: TillDb,
  hold: { held_uuid: string; label: string; payload: HeldPayload; held_at?: string },
): Promise<HeldBill> {
  const row: HeldBill = {
    held_uuid: hold.held_uuid,
    label: hold.label,
    held_at: hold.held_at ?? new Date().toISOString(),
    expires_policy: "today",
    payload: hold.payload,
  };
  await db.held.put(row);
  return row;
}

/** Take a hold off the list. The only way a hold leaves, whether it was resumed
 *  into a bill or the store let it go at day close - and both of those are a
 *  person's decision, never a timer's. */
export async function dropHold(db: TillDb, heldUuid: string): Promise<void> {
  await db.held.delete(heldUuid);
}

/** The store's answer at day close: this one carries to tomorrow.
 *
 *  Stamped with the day it was answered, so tomorrow's close asks again. A hold
 *  kept once and never mentioned again would be a cart nobody sees. */
export async function keepHold(db: TillDb, heldUuid: string, day = localDay()): Promise<void> {
  const row = await db.held.get(heldUuid);
  if (!row) return;
  await db.held.put({ ...row, expires_policy: "kept", reviewed_on: day });
}

/**
 * The holds the store has to answer for before the day closes.
 *
 * Parked before today, and not already answered for today. Nothing here deletes
 * anything and nothing calls a timer: the whole of grill Q13's "nothing expires
 * silently" is that this function only ever produces a *list to show somebody*.
 */
export function holdsToReview(holds: HeldBill[], day = localDay()): HeldBill[] {
  return holds.filter(
    (hold) => localDay(new Date(hold.held_at)) < day && (hold.reviewed_on ?? "") < day,
  );
}

/** How a parked cart names itself in the list, when nobody labelled it. */
export function describeHold(hold: HeldBill): string {
  const pieces = hold.payload?.pieces ?? 0;
  return `${pieces} ${pieces === 1 ? "piece" : "pieces"}`;
}

/** The five columns `PUT /api/sell/held-bills` takes, and not the sixth: the
 *  day-close answer is the counter's own bookkeeping (see `HeldBill`). */
export function mirrorRow(hold: HeldBill): Record<string, unknown> {
  const { reviewed_on: _reviewed, ...row } = hold;
  return row;
}
