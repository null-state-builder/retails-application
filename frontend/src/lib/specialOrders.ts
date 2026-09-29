/** Special orders (store operations ticket 21, ST-ORD-2): the words the Special
 *  orders screen and the till's collection show. The server decides every
 *  status and amount; these only say them. */

import type { ApiRead, ApiSchemas } from "./api";
import { formatINR, rupeesToPaise } from "./format";
import type { Tender } from "./reservations";

export type SpecialOrder = ApiRead<ApiSchemas["SpecialOrder"]>;

export const SPECIAL_ORDERS_API = "/sell/special-orders";

/** Online only (§28): said the same way on the screen and at the till. */
export const SPECIAL_ORDER_OFFLINE =
  "Special orders need the internet. Nothing can be taken, moved on, cancelled or refunded until it is back; what you typed stays here.";

const STATUS: Record<string, { label: string; tone: string }> = {
  asked: { label: "Asked", tone: "amber" },
  ordered: { label: "Ordered", tone: "blue" },
  arrived: { label: "Arrived", tone: "blue" },
  told: { label: "Customer told", tone: "blue" },
  collected: { label: "Collected", tone: "green" },
  cancelled: { label: "Cancelled", tone: "navy" },
};

/** The steps in order, for the progress line on a card. */
export const STEPS = ["asked", "ordered", "arrived", "told", "collected"] as const;

/** The status as a chip: words and a tone, never colour alone. */
export function statusChip(status: string): { label: string; tone: string } {
  return STATUS[status] ?? { label: status, tone: "navy" };
}

/** Is the order still being worked (not collected, not cancelled)? */
export function isOpen(o: Pick<SpecialOrder, "status">): boolean {
  return o.status !== "collected" && o.status !== "cancelled";
}

/** Can it be collected on a bill now? The piece is at the store and the
 *  customer was told - no step is skipped (a walk-in is told "in person"). */
export function collectable(o: Pick<SpecialOrder, "status">): boolean {
  return o.status === "told";
}

/** What was asked for, in one line. */
export function itemText(
  o: Pick<SpecialOrder, "brand_name" | "style_code" | "size" | "colour">,
): string {
  return [o.brand_name, o.style_code, o.colour, o.size].filter(Boolean).join(" · ");
}

/** How it was ordered, in words. */
export function routeText(
  o: Pick<SpecialOrder, "route" | "transfer_source" | "booking_number" | "ordered_barcode">,
): string {
  if (o.route === "transfer") return `Transfer request from ${o.transfer_source ?? "another site"}`;
  if (o.route === "booking") return `Booking ${o.booking_number ?? ""}`.trim();
  return "Not ordered yet";
}

const TOLD: Record<string, string> = {
  call: "by phone call",
  message: "by message",
  in_person: "in person",
};

export function toldText(how: string): string {
  return TOLD[how] ?? how;
}

/** What became of the advance, in words. */
export function advanceText(
  o: Pick<SpecialOrder, "advance_outcome" | "advance_balance_paise" | "voucher">,
): string {
  const paid = o.voucher ? formatINR(o.voucher.amount_paise) : "";
  switch (o.advance_outcome) {
    case "none":
      return "No advance";
    case "held":
      return `${paid} held`;
    case "used":
      return `${paid} used on the bill`;
    case "refunded":
      return `${paid} refunded`;
    case "forfeited":
      return `${paid} forfeited`;
    case "refund_due":
      return `${formatINR(o.advance_balance_paise)} to refund`;
    default:
      return o.advance_outcome;
  }
}

export interface SpecialOrderDraft {
  customer_name: string;
  customer_mobile: string;
  brand: string;
  style_code: string;
  size: string;
  colour: string;
  note: string;
  advance: string;
  mode: Tender;
  reference: string;
}

export function emptyDraft(): SpecialOrderDraft {
  return {
    customer_name: "",
    customer_mobile: "",
    brand: "",
    style_code: "",
    size: "",
    colour: "",
    note: "",
    advance: "",
    mode: "cash",
    reference: "",
  };
}

/** What is wrong with the draft before it is sent, or "" when it can go. The
 *  server checks everything again; this only saves a round trip. */
export function draftProblem(draft: SpecialOrderDraft): string {
  if (!draft.customer_name.trim()) return "Type the customer's name.";
  if (draft.customer_mobile.replace(/\D/g, "").slice(-10).length !== 10)
    return "Type the customer's 10-digit mobile number.";
  if (!draft.brand) return "Choose the brand.";
  if (!draft.style_code.trim() || !draft.size.trim() || !draft.colour.trim())
    return "Type the style, size and colour.";
  if (draft.advance.trim() && !rupeesToPaise(draft.advance))
    return "Type the advance in rupees, or leave it blank.";
  return "";
}

/** The request body for `POST /sell/special-orders`. */
export function specialOrderRequest(id: string, store: string, draft: SpecialOrderDraft) {
  const amount = draft.advance.trim() ? rupeesToPaise(draft.advance) : null;
  return {
    id,
    store,
    customer_name: draft.customer_name.trim(),
    customer_mobile: draft.customer_mobile.trim(),
    brand: Number(draft.brand),
    style_code: draft.style_code.trim(),
    size: draft.size.trim(),
    colour: draft.colour.trim(),
    note: draft.note.trim(),
    advance: amount
      ? { amount_paise: amount, mode: draft.mode, reference: draft.reference.trim() }
      : null,
  };
}
