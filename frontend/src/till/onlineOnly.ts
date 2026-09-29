// What an offline till must not do (store operations ticket 05).
//
// ST-POS-5, ST-CMP-4 (D8) and section 28 of the store operations PRD. Some work
// needs head office at the moment it happens, and a counter with no connection
// cannot pretend otherwise:
//
//   · **A credit note as payment.** Its balance is shared across the chain and
//     only head office holds it live (overall PRD R-POS-003). Known or unknown,
//     an offline till takes none.
//   · **A B2B bill** (the buyer's GSTIN is on it). Where e-invoicing applies, an
//     invoice without an IRN is not valid (CGST Rules 48(5)), so an offline till
//     cannot issue one. This is the one thing allowed to block a bill (D8).
//   · **A credit note against a B2B bill** - at this counter, an exchange whose
//     original bill carried the buyer's GSTIN.
//
// Each is refused **before the bill is made**, with one message saying what
// cannot be done, why, and what to do instead. Everything else - an ordinary
// B2C bill, an exchange against a B2C bill - bills offline exactly as before.
//
// The rule is a store feature switch (`online-only-refusals`), on for every
// store unless Admin switches it off (baseline B4). Off, nothing here refuses and
// the till behaves as it did before this ticket. A till that has never heard
// the switch treats it as on, which is the default everywhere.
//
// Note on credit notes: the counter has had no credit-note payment since #284
// (the tender modes are cash, card and UPI, and the server refuses any other).
// The check below still names the mode, so a payment path added later cannot
// reach an offline bill without meeting it.

import { GIFT_VOUCHER_OFFLINE_REFUSAL } from "./giftVoucher";
import { normaliseGstin } from "./gstin";

/** The feature key, as the server's registry spells it. */
export const ONLINE_ONLY_FEATURE = "online-only-refusals";

/** What to do instead, for a B2B bill - word for word from the PRD (section 28). */
export const B2B_WHAT_TO_DO =
  "Bill this as a normal sale, or wait for the connection to issue a GST invoice";

export const B2B_OFFLINE_REFUSAL =
  "Offline, a bill with the buyer's GSTIN cannot be made: a GST invoice must be " +
  `registered online. ${B2B_WHAT_TO_DO}.`;

export const B2B_CREDIT_NOTE_OFFLINE_REFUSAL =
  "Offline, an exchange against a bill with the buyer's GSTIN cannot be made: its " +
  `credit note must be registered online. ${B2B_WHAT_TO_DO}.`;

export const CREDIT_NOTE_OFFLINE_REFUSAL =
  "Offline, a credit note cannot be taken as payment: its balance is checked with " +
  "head office. Take another payment, or wait for the connection.";

/** Ticket 20 (section 28): collecting a reservation, and using its advance, is
 *  checked with head office at the moment it happens. */
export const RESERVATION_OFFLINE_REFUSAL =
  "Offline, a reservation cannot be collected: its pieces and advance are held at " +
  "head office. Wait for the connection, or take the reservation off this bill.";

/** Ticket 21: collecting a special order, and using its advance, is checked
 *  with head office at the moment it happens, the same way. */
export const SPECIAL_ORDER_OFFLINE_REFUSAL =
  "Offline, a special order cannot be collected: its advance is held at head office. " +
  "Wait for the connection, or take the special order off this bill.";

export interface OnlineOnlyInput {
  /** Whether the counter has a connection right now. */
  online: boolean;
  /** This store's switch, as the counter last heard it (true if never heard). */
  refusalsOn: boolean;
  /** The buyer's GSTIN on this bill, as it would be sent ("" for B2C). */
  buyerGstin: string;
  /** The buyer's GSTIN on the bill an exchange is taken against ("" for none). */
  originalBuyerGstin: string;
  /** How the bill is being paid, mode by mode. */
  tenderModes: readonly string[];
  /** Ticket 20: this bill collects a customer reservation. */
  reservation?: boolean;
  /** Ticket 21: this bill collects a special order. */
  specialOrder?: boolean;
  /** Ticket 19: a gift voucher pays towards this bill. */
  giftVoucher?: boolean;
}

/** Why this bill cannot be made offline, in the counter's words - or "". */
export function whyOfflineRefuses(input: OnlineOnlyInput): string {
  if (input.online) return "";
  // Special orders are online only too (ticket 21), whatever the switch says.
  if (input.specialOrder) return SPECIAL_ORDER_OFFLINE_REFUSAL;
  // Reservations are online only whatever ticket 05's switch says (ticket 20):
  // there is no offline way to hold, release or spend them.
  if (input.reservation || input.tenderModes.some((mode) => mode === "advance")) {
    return RESERVATION_OFFLINE_REFUSAL;
  }
  // Gift vouchers are online only too (ticket 19), whatever the switch says:
  // head office holds every voucher's balance.
  if (input.giftVoucher || input.tenderModes.some((mode) => mode === "gift_voucher")) {
    return GIFT_VOUCHER_OFFLINE_REFUSAL;
  }
  if (!input.refusalsOn) return "";
  if (input.tenderModes.some((mode) => mode === "credit_note")) {
    return CREDIT_NOTE_OFFLINE_REFUSAL;
  }
  if (normaliseGstin(input.originalBuyerGstin)) return B2B_CREDIT_NOTE_OFFLINE_REFUSAL;
  if (normaliseGstin(input.buyerGstin)) return B2B_OFFLINE_REFUSAL;
  return "";
}

/** The switch as the dataset carries it. An older server that says nothing
 *  leaves whatever the counter already holds; a counter that has never heard
 *  holds the default, which is on (B4). */
export function refusalsOnFrom(held: unknown): boolean {
  return typeof held === "boolean" ? held : true;
}
