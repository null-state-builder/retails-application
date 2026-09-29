// The Bills list: what this counter has done, wherever the bills are (OPS-08,
// store and warehouse operations PRD §9.2).
//
// A bill lives in one of two places, and a person looking for one does not know
// or care which. Head office holds every bill it has been told about; the
// counter's own queue holds the ones it has not been able to tell anyone about
// yet - the line went down, or it simply has not drained. A bill rung up ten
// minutes ago may exist nowhere but in the browser it was rung up in, and it is
// exactly the bill somebody comes back about.
//
// So the screen shows both lists as one, and this module is the joining: the
// same bill must appear **once**, and an unsynced bill must say so. Dedupe is by
// bill number, because that is the identity the till mints and the server keeps -
// the till numbers a bill before the server has ever seen it, and the server
// stores that number rather than minting its own.
//
// Everything here is pure, over plain rows. The Dexie read and the HTTP read
// both happen in the screen; what is hard to get right - which copy wins, what
// counts as being inside a date range - is here, where it has tests.

import type { QueuedBill } from "./types";

/** One row of the Bills list, from either side. */
export interface BillRow {
  doc_number: string;
  billed_at: string;
  customer_name: string;
  customer_mobile: string;
  net_paise: number;
  /** How the bill was paid, for the row's one-line summary. */
  tenders: {
    mode: string;
    amount_paise: number;
    upi_state?: string | null;
    /** Ticket 19: the gift voucher a `gift_voucher` tender spent. */
    gift_voucher?: string;
  }[];
  /** False while this bill exists only in this counter's queue. */
  synced: boolean;
  /** The financial year and sequence this counter numbered it, when the row
   *  came from the queue or the server sent them. Null on neither, in practice;
   *  optional so an older server row is still a row. */
  fy?: string;
  till_seq?: number;
}

/** A queued bill as a Bills row: everything the list shows, and nothing it
 *  does not - the lines stay in the queue until somebody opens the bill. */
export function queuedAsRow(bill: QueuedBill): BillRow {
  return {
    doc_number: bill.doc_number,
    billed_at: bill.billed_at,
    customer_name: bill.customer?.name ?? "",
    customer_mobile: bill.customer?.mobile ?? "",
    net_paise: bill.totals.net_paise,
    tenders: bill.tenders.map((tender) => ({
      mode: tender.mode,
      amount_paise: tender.amount_paise,
      upi_state: tender.upi_state ?? null,
      ...(tender.gift_voucher ? { gift_voucher: tender.gift_voucher } : {}),
    })),
    synced: false,
    fy: bill.fy,
    till_seq: bill.till_seq,
  };
}

/**
 * The server's list and this counter's queue, as one list (PRD §9.2).
 *
 * **The server copy wins on a bill both sides hold**, which is the opposite of
 * the return-search rule next door (`mergeRecentBills`, where the local copy
 * wins because it may be newer than anything head office knows). The difference
 * is what the two lists are for: the return search wants the freshest picture of
 * what a bill still owes, while this list is answering "has this gone in yet?",
 * and a bill the server is holding has. Keeping the local copy here would leave
 * a synced bill wearing "not yet synced" until the queue row was swept.
 *
 * Newest first, because that is the order a counter thinks in.
 */
export function mergeBillRows(local: BillRow[], server: BillRow[]): BillRow[] {
  const held = new Set(server.map((row) => row.doc_number));
  const merged = [...server, ...local.filter((row) => !held.has(row.doc_number))];
  return merged.sort((a, b) => b.billed_at.localeCompare(a.billed_at));
}

/**
 * The rows billed within a range of days, both ends included.
 *
 * Applied to the **queue** only. The server does its own filtering, in the
 * store's own timezone and over an index; this is here so the local half of the
 * list obeys the same date box rather than showing every unsynced bill whatever
 * day is asked for. The comparison is on the local calendar day, because a
 * cashier asking for "today" means the day they are standing in.
 *
 * A blank end of the range is open: `from` alone means "since", `to` alone means
 * "up to", and neither means everything.
 */
export function billsInRange(rows: BillRow[], from: string, to: string): BillRow[] {
  return rows.filter((row) => {
    const day = dayOf(row.billed_at);
    if (!day) return false;
    if (from && day < from) return false;
    if (to && day > to) return false;
    return true;
  });
}

/** The rows a typed term finds: who bought it, the number on the slip, or the
 *  sequence a person reads off the end of it. Applied to the queue half only,
 *  for the same reason as the date range. */
export function billsMatching(rows: BillRow[], term: string): BillRow[] {
  const wanted = term.trim().toLocaleLowerCase();
  if (!wanted) return rows;
  const digits = wanted.replace(/\D/g, "");
  return rows.filter((row) => {
    if (row.doc_number.toLocaleLowerCase().includes(wanted)) return true;
    if (row.customer_name.toLocaleLowerCase().includes(wanted)) return true;
    if (digits && row.customer_mobile.replace(/\D/g, "").includes(digits)) return true;
    return Boolean(digits) && String(row.till_seq ?? "") === digits;
  });
}

/** How a bill was paid, in one line - "Cash ₹1,499" becomes "Cash", and a split
 *  reads "Cash + UPI". The amounts are on the row already; what this answers is
 *  "how", which is the question somebody scanning a day is asking. */
export function tenderSummary(row: BillRow): string {
  if (!row.tenders.length) return "—";
  const words: Record<string, string> = { cash: "Cash", card: "Card", upi: "UPI" };
  return row.tenders.map((tender) => words[tender.mode] ?? tender.mode).join(" + ");
}

/** The calendar day an ISO timestamp falls on, as `YYYY-MM-DD`, or "" when it is
 *  not a timestamp at all. Local, deliberately: see `billsInRange`. */
export function dayOf(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}

/** Today, in the same spelling the date boxes use. */
export function todayKey(now: Date = new Date()): string {
  return dayOf(now.toISOString()) || "";
}
