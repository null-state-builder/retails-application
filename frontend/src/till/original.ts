// Finding the bill a piece was bought on (#184, D10 step 5).
//
// **Local first, server when online**, and the order is not an optimisation.
// A bill the counter rang up ten minutes ago may still be in the queue - the line
// went down, or it simply has not drained yet - and the server cannot answer
// about it at all. That is the commonest exchange there is: the customer walks
// back in with the wrong size. So the counter's own queue is asked first, and
// only then head office.
//
// The two answers differ in where they came from, but both can back the same new
// Sale: the accept pipeline resolves an unsynced original once the queue drains
// in order, while a synced bill can be checked immediately.
//
// Everything a returned line is worth is read off the bill rather than off the
// price list: what comes back is what was paid (D2), which is a fact about that
// bill and about no other.

import type { ApiSchemas } from "../lib/api";
import type { TillDb } from "./db";
import type { Exchange, ExchangeOriginal, OriginalLine } from "./exchange";
import { searchCustomers } from "./lookup";
import type { CachedBill, QueuedBill, TillItem, TillKnownCustomer } from "./types";

/** A bill found, and where it was found. */
export interface FoundBill {
  original: ExchangeOriginal;
  lines: OriginalLine[];
  billed_at: string;
  customer_name: string;
  customer_mobile: string;
  /** The Sale's own authoritative net, including any exchange it originally
   * carried. Never reconstructed from the filtered returnable lines. */
  net_paise: number;
  /** True when this bill is still in the counter's own queue. */
  local: boolean;
  /** The buyer's GSTIN on that bill, "" on a B2C bill. An offline exchange
   *  against a B2B bill is refused (ticket 05). Optional: an older draft or
   *  server may not say. */
  buyer_gstin?: string;
  /** Ticket 13 (B60): what the bank offers paid of the bill, spread over its
   *  lines when a piece comes back. Absent from an older draft or server: none. */
  bank_offer_paise?: number;
  /** Ticket 13: the store that issued it and the GSTIN it was issued under.
   *  Absent on a bill this counter holds, which is its own store's. */
  store_code?: string;
  store_gstin?: string;
}

/** What the bank offers paid of a bill, from its tenders (ticket 11's payments). */
export function bankOfferOf(tenders: { mode: string; amount_paise?: number }[]): number {
  return tenders
    .filter((tender) => tender.mode === "bank_offer")
    .reduce((n, tender) => n + (tender.amount_paise ?? 0), 0);
}

/** How a bill is asked for: the sequence number off the printed slip. */
export function billSeqFrom(typed: string): number | null {
  const trimmed = typed.trim();
  if (!trimmed) return null;
  // A person reads "74" off the slip as often as the whole key, and the whole key
  // ends in the same number - so the last run of digits is what they mean either
  // way. `26-27/DEO/SAL/74` and `74` are the same question.
  const digits = trimmed.match(/(\d+)\s*$/);
  const seq = digits ? Number(digits[1]) : NaN;
  return Number.isInteger(seq) && seq > 0 ? seq : null;
}

/**
 * The bill this counter numbered `seq` and has not yet sent, or null.
 *
 * Read out of the queue, which is the only copy of it that exists anywhere.
 * `returned_qty` is worked out from the other bills in the same queue: an
 * exchange against this one, rung up while both were still local, is a piece
 * already given back and the counter must not offer it twice.
 */
export async function findQueuedBill(
  db: TillDb,
  fy: string,
  seq: number,
): Promise<FoundBill | null> {
  const queue = await db.queue.orderBy("id").toArray();
  const bill = queue.find((row) => row.fy === fy && row.till_seq === seq);
  if (!bill) return null;
  const given = alreadyGivenBack(queue, fy, seq);
  const items = await db.items.toArray();
  return fromQueued(bill, given, items);
}

/** A queued bill by the exact document number encoded in its receipt barcode.
 * Kept separate from the sequence lookup because configured voucher-series
 * suffixes need not end in digits. */
export async function findQueuedBillByDoc(
  db: TillDb,
  docNumber: string,
): Promise<FoundBill | null> {
  const wanted = docNumber.trim().toLocaleLowerCase();
  if (!wanted) return null;
  const queue = await db.queue.orderBy("id").toArray();
  const bill = queue.find((row) => row.doc_number.toLocaleLowerCase() === wanted);
  if (!bill) return null;
  const given = alreadyGivenBack(queue, bill.fy, bill.till_seq);
  const items = await db.items.toArray();
  return fromQueued(bill, given, items);
}

/** Unsynced bills whose snapshotted customer matches the third find-bill door.
 * This is deliberately local even while online: the bill rung up five minutes
 * ago may not exist at head office yet. */
export async function searchQueuedBillsByCustomer(
  db: TillDb,
  key: "mobile" | "name",
  term: string,
): Promise<FoundBill[]> {
  const query = key === "mobile" ? digitsOf(term) : term.trim().toLocaleLowerCase();
  if (!query) return [];
  const queue = await db.queue.orderBy("id").reverse().toArray();
  const items = await db.items.toArray();
  return queue
    .filter((bill) => {
      const value =
        key === "mobile"
          ? digitsOf(bill.customer?.mobile ?? "")
          : (bill.customer?.name ?? "").toLocaleLowerCase();
      return value.includes(query);
    })
    .map((bill) => fromQueued(bill, alreadyGivenBack(queue, bill.fy, bill.till_seq), items));
}

/** Matching people from the all-KDPS customer master. Mobile uses its index;
 * name is the rarer deliberate scan, capped before it can become a result dump. */
export async function searchKnownCustomers(
  db: TillDb,
  key: "mobile" | "name",
  term: string,
  limit = 8,
): Promise<TillKnownCustomer[]> {
  if (key === "mobile") return searchCustomers(db, term, limit);
  const wanted = term.trim().toLocaleLowerCase();
  if (wanted.length < 2) return [];
  try {
    return await db.customers
      .filter((row) => row.name.toLocaleLowerCase().includes(wanted))
      .limit(limit)
      .toArray();
  } catch {
    return [];
  }
}

// ── Cached originals (OPS-09, PRD §10.3-10.4) ───────────────────────────────
//
// The third place a bill can be found, and the one that makes an exchange
// possible with no network. The order the counter asks in is unchanged and is
// still not an optimisation: **queue, then cache, then head office**. A bill this
// counter rang up ten minutes ago exists only in the queue; a bill from last week
// is in the cache; anything older, or from before this device synced, is head
// office's and needs a line.

/** What a counter is told when it asks about a bill it is not holding. */
export const UNCACHED_ORIGINAL_CODE = "UNCACHED_ORIGINAL";
export const UNCACHED_ORIGINAL = "This bill is not on this till — reconnect to look it up";

/** A cached bill by its `(fy, till_seq)`, as `FoundBill`. */
export async function findCachedBill(
  db: TillDb,
  fy: string,
  seq: number,
): Promise<FoundBill | null> {
  const row = await db.bills.filter((bill) => bill.fy === fy && bill.till_seq === seq).first();
  return row ? fromCached(row) : null;
}

/** A cached bill by the exact number printed on the customer's copy - either the
 *  Tally key or this device's own `{Store}-{Counter}-{Seq}` display number, since
 *  a person reads whichever one their receipt shows them. */
export async function findCachedBillByDoc(
  db: TillDb,
  docNumber: string,
): Promise<FoundBill | null> {
  const wanted = docNumber.trim().toLocaleLowerCase();
  if (!wanted) return null;
  const row = await db.bills
    .filter(
      (bill) =>
        bill.doc_number.toLocaleLowerCase() === wanted ||
        bill.till_number.toLocaleLowerCase() === wanted,
    )
    .first();
  return row ? fromCached(row) : null;
}

/** Cached bills whose snapshotted customer matches, newest first. */
export async function searchCachedBillsByCustomer(
  db: TillDb,
  key: "mobile" | "name",
  term: string,
): Promise<FoundBill[]> {
  const query = key === "mobile" ? digitsOf(term) : term.trim().toLocaleLowerCase();
  if (!query) return [];
  const rows = await db.bills.toArray();
  return rows
    .filter((bill) => {
      const value =
        key === "mobile" ? digitsOf(bill.customer_mobile) : bill.customer_name.toLocaleLowerCase();
      return value.includes(query);
    })
    .sort((a, b) => b.billed_at.localeCompare(a.billed_at))
    .map(fromCached);
}

/** A cached bill in the one shape the return picker reads.
 *
 *  `local: false` on purpose, and it is not a slip. `local` means "this counter
 *  printed it and head office has not seen it", which decides how the accept
 *  pipeline resolves the original. A cached bill is head office's own row, sent
 *  back down - so the server can resolve it the moment the exchange arrives. */
export function fromCached(bill: CachedBill): FoundBill {
  return {
    original: { fy: bill.fy, till_seq: bill.till_seq, doc_number: bill.doc_number },
    billed_at: bill.billed_at,
    customer_name: bill.customer_name,
    customer_mobile: bill.customer_mobile,
    buyer_gstin: bill.buyer_gstin ?? "",
    net_paise: bill.net_paise,
    local: false,
    bank_offer_paise: bankOfferOf(bill.tenders ?? []),
    // A bill's own return legs are pieces that have already come back, and are
    // not themselves returnable - the same filter a server bill gets. Nor is an
    // alteration charge (ticket 22): a service, not a piece.
    lines: bill.lines
      .filter((line) => line.direction !== "return" && line.kind !== "alteration")
      .map((line) => ({
        line_no: line.line_no,
        barcode: line.barcode,
        season: line.season,
        design: line.design,
        color: line.color,
        size: line.size,
        brand: line.brand,
        item: line.item,
        hsn: line.hsn,
        qty: line.qty,
        net_paise: line.net_paise,
        gst_rate: line.gst_rate,
        gst_paise: line.gst_paise,
        manual_desc: line.manual_desc,
        direction: line.direction,
        returned_qty: line.returned_qty,
        returned_paise: line.returned_paise,
        returned_bank_paise: line.returned_bank_paise ?? 0,
      })),
  };
}

/** The bill this device is holding under `seq`, from the queue or the cache.
 *
 *  Queue first, because a bill rung up ten minutes ago is not in the cache and
 *  never will be until it syncs. */
export async function findHeldBill(db: TillDb, fy: string, seq: number): Promise<FoundBill | null> {
  return (await findQueuedBill(db, fy, seq)) ?? (await findCachedBill(db, fy, seq));
}

/** The same, by the number printed on a customer's copy. */
export async function findHeldBillByDoc(db: TillDb, docNumber: string): Promise<FoundBill | null> {
  return (await findQueuedBillByDoc(db, docNumber)) ?? (await findCachedBillByDoc(db, docNumber));
}

/** A picked master row identifies a person by mobile. Names are deliberately
 * not reused for the history request: two customers can share one, and the
 * server's bounded fuzzy-name search cannot promise which one's bills fit. */
export function billSearchForCustomer(customer: TillKnownCustomer): {
  key: "mobile";
  term: string;
} {
  return { key: "mobile", term: customer.mobile };
}

function digitsOf(value: string): string {
  return value.replace(/\D/g, "");
}

function fromQueued(
  bill: QueuedBill,
  given: Map<number, { qty: number; paise: number; bank: number }>,
  items: TillItem[],
): FoundBill {
  return {
    original: { fy: bill.fy, till_seq: bill.till_seq, doc_number: bill.doc_number },
    billed_at: bill.billed_at,
    customer_name: bill.customer?.name ?? "",
    customer_mobile: bill.customer?.mobile ?? "",
    buyer_gstin: bill.customer?.gstin ?? "",
    net_paise: bill.totals.net_paise,
    local: true,
    bank_offer_paise: bankOfferOf(bill.tenders),
    lines: bill.lines
      .filter((line) => line.direction !== "return" && line.kind !== "alteration")
      .map((line) => {
        const back = given.get(line.line_no) ?? { qty: 0, paise: 0, bank: 0 };
        const piece = items.find(
          (row) => row.barcode === line.barcode && row.season === (line.season ?? ""),
        );
        return {
          line_no: line.line_no,
          barcode: line.barcode,
          season: line.season ?? "",
          ...describedBy(piece),
          qty: line.qty,
          net_paise: line.net_paise,
          gst_rate: line.gst_rate,
          gst_paise: line.gst_paise,
          manual_desc: line.manual_desc ?? "",
          direction: "sale",
          returned_qty: back.qty,
          returned_paise: back.paise,
          returned_bank_paise: back.bank,
        };
      }),
  };
}

/** What every other bill still in this queue has already given back off `seq`. */
function alreadyGivenBack(
  queue: QueuedBill[],
  fy: string,
  seq: number,
): Map<number, { qty: number; paise: number; bank: number }> {
  const given = new Map<number, { qty: number; paise: number; bank: number }>();
  for (const bill of queue) {
    const exchange = bill.exchange;
    if (exchange?.original.fy !== fy || exchange.original.till_seq !== seq) continue;
    for (const leg of exchange.lines) {
      const seen = given.get(leg.original_line) ?? { qty: 0, paise: 0, bank: 0 };
      given.set(leg.original_line, {
        qty: seen.qty + leg.qty,
        paise: seen.paise + leg.refund_paise,
        bank: seen.bank + (leg.bank_offer_paise ?? 0),
      });
    }
  }
  return given;
}

/** The seven merchandising words for a piece the counter still stocks - or
 *  blanks, which `describeOriginal` falls back through to the typed description
 *  and then to the barcode. */
function describedBy(piece: TillItem | undefined) {
  return {
    design: piece?.design ?? "",
    color: piece?.color ?? "",
    size: piece?.size ?? "",
    brand: piece?.brand ?? "",
    item: piece?.item ?? "",
    hsn: piece?.hsn ?? "",
  };
}

/** The read shape of `GET /api/sell/sales/{doc_number}`, generated from the
 * server serializer through the OpenAPI seam (ADR-0001). */
export type SaleDetail = ApiSchemas["SaleRead"];

function requiredPaise(value: number | undefined, field: string): number {
  if (value === undefined) throw new Error(`Head office omitted ${field} from the bill.`);
  return value;
}

/** A bill head office holds, in the same shape as a queued one. */
export function fromServer(detail: SaleDetail): FoundBill {
  return {
    original: {
      fy: detail.fy,
      till_seq: detail.till_seq,
      doc_number: detail.doc_number ?? "",
    },
    billed_at: detail.billed_at,
    customer_name: detail.customer_name ?? "",
    customer_mobile: detail.customer_mobile ?? "",
    buyer_gstin: detail.buyer_gstin ?? "",
    net_paise: requiredPaise(detail.net_paise, "net_paise"),
    local: false,
    bank_offer_paise: bankOfferOf(detail.tenders ?? []),
    store_code: detail.store_code,
    store_gstin: detail.store_gstin ?? "",
    // A bill's own exchange legs are lines too, and they are not returnable -
    // they are pieces that already came back. Nor is an alteration charge.
    lines: detail.lines
      .filter((line) => line.direction !== "return" && line.kind !== "alteration")
      .map((line) => ({
        line_no: line.line_no,
        barcode: line.barcode,
        season: line.season ?? "",
        design: line.design ?? "",
        color: line.color ?? "",
        size: line.size ?? "",
        brand: line.brand ?? "",
        item: line.item ?? "",
        hsn: line.hsn ?? "",
        qty: line.qty,
        net_paise: requiredPaise(line.net_paise, `line ${line.line_no} net_paise`),
        gst_rate: line.gst_rate ?? "0.00",
        gst_paise: requiredPaise(line.gst_paise, `line ${line.line_no} gst_paise`),
        manual_desc: line.manual_desc ?? "",
        direction: line.direction ?? "sale",
        returned_qty: line.returned_qty,
        returned_paise: line.returned_paise,
        returned_bank_paise: line.returned_bank_paise ?? 0,
      })),
  };
}

/** Include return legs that this till has committed but head office has not yet
 * accepted. Without them a server bill can offer the same piece twice. */
export async function withQueuedReturns(db: TillDb, found: FoundBill): Promise<FoundBill> {
  const queue = await db.queue.orderBy("id").toArray();
  const pending = alreadyGivenBack(queue, found.original.fy, found.original.till_seq);
  if (!pending.size) return found;
  return {
    ...found,
    lines: found.lines.map((line) => {
      const back = pending.get(line.line_no);
      return back
        ? {
            ...line,
            returned_qty: Math.min(line.qty, line.returned_qty + back.qty),
            returned_paise: Math.min(line.net_paise, line.returned_paise + back.paise),
            returned_bank_paise: (line.returned_bank_paise ?? 0) + back.bank,
          }
        : line;
    }),
  };
}

/** The against-bill card persisted inside a direct-cart exchange, if this draft
 * was written by a build new enough to carry it. */
export function foundFromExchange(exchange: Exchange | null): FoundBill | null {
  const bill = exchange?.original_bill;
  if (!exchange || !bill) return null;
  return { original: exchange.original, ...bill };
}

// ── Recent-bills helpers (for the three return-start doors) ──────────────────
//
// These are pure convenience over the existing queue and search paths - no new
// contract, no new server endpoint required for local bills. The server `?recent=N`
// query is fetched by the caller (Billing.tsx) and passed in as `server` rows.

/** A thin summary of a bill: enough for the recent-bills pick list without
 *  fetching or loading any line detail. */
export interface RecentBillSummary {
  doc_number: string;
  billed_at: string;
  customer_name: string;
  customer_mobile: string;
  net_paise: number;
  local: FoundBill | null;
}

/** Up to `limit` (default 3) most-recent bills from this counter's own queue,
 *  newest-first, as pick-list summaries.
 *
 *  Local first and always: a bill the counter rang up ten minutes ago may still
 *  be in the queue, and the server cannot answer about it at all. This is the
 *  same local-first principle that governs `findQueuedBill`. */
export async function recentQueuedBills(db: TillDb, limit = 3): Promise<RecentBillSummary[]> {
  const queue = await db.queue.orderBy("id").reverse().toArray();
  const items = await db.items.toArray();
  return queue.slice(0, limit).map((bill) => {
    const given = alreadyGivenBack(queue, bill.fy, bill.till_seq);
    const full = fromQueued(bill, given, items);
    return {
      doc_number: bill.doc_number,
      billed_at: bill.billed_at,
      customer_name: bill.customer?.name ?? "",
      customer_mobile: bill.customer?.mobile ?? "",
      net_paise: bill.totals.net_paise,
      local: full,
    };
  });
}

/** Merge a local-queue list and a server list of recent-bill summaries.
 *  Deduplicates by `doc_number` (local wins), sorts newest-first, returns at
 *  most `limit` rows.
 *
 *  A bill the counter has not yet synced appears on both sides: the queue copy
 *  wins so the caller never has to wait for an extra server round-trip. */
export function mergeRecentBills(
  local: RecentBillSummary[],
  server: RecentBillSummary[],
  limit = 3,
): RecentBillSummary[] {
  const seen = new Set(local.map((b) => b.doc_number));
  const merged = [...local, ...server.filter((b) => !seen.has(b.doc_number))];
  merged.sort((a, b) => b.billed_at.localeCompare(a.billed_at));
  return merged.slice(0, limit);
}

/** Resolve a recent-bill summary to a full `FoundBill`.
 *
 *  If the summary carries a `local` copy (queue bill), it is returned directly.
 *  Otherwise, `GET /api/sell/sales/{doc_number}` is fetched and converted by
 *  `fromServer` — identical path to the existing bill-search flow. */
export async function resolveRecentMatch(summary: RecentBillSummary): Promise<FoundBill> {
  if (summary.local) return summary.local;
  const res = await fetch(`/api/sell/sales/${encodeURIComponent(summary.doc_number)}`);
  if (!res.ok) throw new Error(`Head office returned ${res.status} for ${summary.doc_number}`);
  const detail: SaleDetail = await res.json();
  return fromServer(detail);
}
