/** Customer reservations (store operations ticket 20, ST-ORD-1): the words the
 *  Reservations screen and the till's pickup show. The server decides every
 *  status, date and amount; these only say them. */

import type { ApiRead, ApiSchemas } from "./api";
import { formatINR, rupeesToPaise } from "./format";

export type Reservation = ApiRead<ApiSchemas["Reservation"]>;
export type ReservationItem = ApiRead<ApiSchemas["ReservationItem"]>;

export const RESERVATIONS_API = "/sell/reservations";

/** Online only (§28): said the same way on the screen and at the till. */
export const RESERVATION_OFFLINE =
  "Reservations need the internet. Nothing can be reserved, collected, cancelled or refunded until it is back; what you typed stays here.";

export type Tender = "cash" | "card" | "upi";
export const TENDERS: { value: Tender; label: string }[] = [
  { value: "cash", label: "Cash" },
  { value: "card", label: "Card" },
  { value: "upi", label: "UPI" },
];

const STATUS: Record<string, { label: string; tone: string }> = {
  active: { label: "Reserved", tone: "blue" },
  collected: { label: "Collected", tone: "green" },
  cancelled: { label: "Cancelled", tone: "navy" },
  expired: { label: "Expired", tone: "amber" },
};

/** The status as a chip: words and a tone, never colour alone. */
export function statusChip(status: string): { label: string; tone: string } {
  return STATUS[status] ?? { label: status, tone: "navy" };
}

/** What became of the advance, in words. */
export function advanceText(
  r: Pick<Reservation, "advance_outcome" | "advance_balance_paise" | "voucher">,
): string {
  const paid = r.voucher ? formatINR(r.voucher.amount_paise) : "";
  switch (r.advance_outcome) {
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
      return `${formatINR(r.advance_balance_paise)} to refund`;
    default:
      return r.advance_outcome;
  }
}

/** How long is left, for an open reservation. */
export function daysLeftText(daysLeft: number): string {
  if (daysLeft < 0) return "Past its date";
  if (daysLeft === 0) return "Last day today";
  if (daysLeft === 1) return "1 day left";
  return `${daysLeft} days left`;
}

/** A date as the customer reads it on the voucher: 5 Oct 2026. */
export function dayText(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const month =
    m === undefined
      ? undefined
      : ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m - 1];
  if (!month || !Number.isInteger(y) || !Number.isInteger(d)) return iso;
  return `${d} ${month} ${y}`;
}

export function itemLabel(item: ReservationItem): string {
  return [item.brand, item.item, item.size, item.color].filter(Boolean).join(" · ") || item.barcode;
}

/** The pieces being reserved, as typed or scanned: one line per barcode. */
export interface DraftLine {
  barcode: string;
  qty: number;
}

/** A scan onto the list: the same tag again adds one to its line. */
export function addScan(lines: DraftLine[], raw: string): DraftLine[] {
  const barcode = raw.trim();
  if (!barcode) return lines;
  const found = lines.find((l) => l.barcode === barcode);
  if (found) return lines.map((l) => (l === found ? { ...l, qty: l.qty + 1 } : l));
  return [...lines, { barcode, qty: 1 }];
}

export interface ReservationDraft {
  customer_name: string;
  customer_mobile: string;
  lines: DraftLine[];
  advance: string;
  mode: Tender;
  reference: string;
}

export function emptyDraft(): ReservationDraft {
  return {
    customer_name: "",
    customer_mobile: "",
    lines: [],
    advance: "",
    mode: "cash",
    reference: "",
  };
}

/** What is wrong with the draft before it is sent, or "" when it can go. The
 *  server checks everything again; this only saves a round trip. */
export function draftProblem(draft: ReservationDraft): string {
  if (!draft.customer_name.trim()) return "Type the customer's name.";
  if (draft.customer_mobile.replace(/\D/g, "").slice(-10).length !== 10)
    return "Type the customer's 10-digit mobile number.";
  if (!draft.lines.length) return "Scan at least one piece to reserve.";
  if (draft.advance.trim() && !rupeesToPaise(draft.advance))
    return "Type the advance in rupees, or leave it blank.";
  return "";
}

/** The request body for `POST /sell/reservations`. */
export function reservationRequest(id: string, store: string, draft: ReservationDraft) {
  const amount = draft.advance.trim() ? rupeesToPaise(draft.advance) : null;
  return {
    id,
    store,
    customer_name: draft.customer_name.trim(),
    customer_mobile: draft.customer_mobile.trim(),
    lines: draft.lines.map((l) => ({ barcode: l.barcode, qty: l.qty })),
    advance: amount
      ? { amount_paise: amount, mode: draft.mode, reference: draft.reference.trim() }
      : null,
  };
}
