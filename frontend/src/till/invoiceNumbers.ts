// The counter's tax invoice numbers in the new series (store operations ticket 04).
//
// From the start date (a 1 April), at a store where the switch is on, every bill
// carries a tax invoice number `XXX/26-27/n` - `XXX` the store's own 3-letter
// prefix - beside its display number (`DEO-T1-74`, which stays) and its Tally key.
// A till bills offline, so it holds its numbers before the network goes: head
// office sends it blocks, one month each, on every online sync
// (`POST /api/sell/till/number-blocks`), and the counter takes them in order at
// Save & Print, inside the same local transaction as the bill.
//
// Two rules the counter keeps here and nowhere else:
//
//   · **A number is checked before it is printed** - at most 16 characters, only
//     letters, digits, `-` and `/`. One that breaks either is refused, and so is a
//     bill with no number left for its month: the counter says so and does not
//     print a tax invoice it cannot number. Online, a sync fetches more.
//   · **A block is used for its own month only.** At month end head office records
//     every number of it that no bill used as cancelled (GSTR-1 Table 13), so a
//     bill dated in another month must never take one.

import { META, readMeta } from "./db";
import type { TillDb } from "./db";
import type { TillTransport } from "./transport";

/** One block as the counter holds it: the server's range and how far into it we are. */
export interface HeldNumberBlock {
  id: number;
  prefix: string;
  /** `26-27`. */
  fy: string;
  /** `YYYY-MM`, the month the block is for. */
  month: string;
  first: number;
  last: number;
  /** The number the next bill takes. Past `last` when the block is used up. */
  next: number;
}

/** What the counter knows about the new series, kept so it bills with no network. */
export interface TillNumbering {
  on: boolean;
  /** `YYYY-MM-DD`, a 1 April. */
  new_format_from: string;
  prefix: string;
  block_size: number;
  blocks: HeldNumberBlock[];
  /** Why head office sent no block when one was needed, in words. */
  problem: string;
}

/** The server's answer (`TillNumberingSerializer`). */
export interface NumberingPayload {
  on: boolean;
  new_format_from: string;
  prefix: string;
  block_size: number;
  blocks: { id: number; prefix: string; fy: string; month: string; first: number; last: number }[];
  problem: string;
}

export const MAX_NUMBER_LENGTH = 16;
const ALLOWED = /^[A-Za-z0-9/-]+$/;

/** A bill the counter will not number: no invoice number is left for its month. */
export class InvoiceNumbersOutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvoiceNumbersOutError";
  }
}

/** The device's own calendar day, `YYYY-MM-DD` - the counter stands in India. */
export function localDay(at: Date): string {
  const month = String(at.getMonth() + 1).padStart(2, "0");
  const day = String(at.getDate()).padStart(2, "0");
  return `${at.getFullYear()}-${month}-${day}`;
}

/** `DEA/26-27/74` - the mirror of `masters.document_series.render` for invoices. */
export function renderInvoiceNumber(prefix: string, fy: string, n: number): string {
  return `${prefix}/${fy}/${n}`;
}

/** Why `number` may not be printed, or null if it may (CGST Rule 46). */
export function invoiceNumberProblem(number: string): string | null {
  if (number.length > MAX_NUMBER_LENGTH) {
    return `${number} is ${number.length} characters; a tax invoice number may have 16 at most.`;
  }
  if (!ALLOWED.test(number))
    return `${number} uses a character other than letters, digits, - and /.`;
  return null;
}

/** Does a bill dated `day` take a number in the new series? */
export function newFormatApplies(state: TillNumbering | null, day: string): boolean {
  return Boolean(state?.on) && day >= (state?.new_format_from ?? "9999-12-31");
}

/** How many numbers the counter still holds for `month` (`YYYY-MM`). */
export function numbersLeft(state: TillNumbering | null, month: string): number {
  return (state?.blocks ?? [])
    .filter((block) => block.month === month)
    .reduce((sum, block) => sum + Math.max(0, block.last - block.next + 1), 0);
}

/**
 * Take the next invoice number for a bill dated `billedAt`, or null when the
 * bill keeps today's format.
 *
 * MUST run inside the bill's own Dexie transaction over `meta` (see
 * `numbering.writeBill`): the number and the bill are written together or not
 * at all, so a rolled-back bill gives its number back and no number is printed
 * on two bills.
 */
export async function takeInvoiceNumber(db: TillDb, billedAt: Date): Promise<string | null> {
  const state = await readMeta<TillNumbering | null>(db, META.numbering, null);
  const day = localDay(billedAt);
  if (!newFormatApplies(state, day)) return null;
  const month = day.slice(0, 7);
  const block = (state?.blocks ?? [])
    .filter((row) => row.month === month && row.next <= row.last)
    .sort((a, b) => a.first - b.first)[0];
  if (!state || !block) {
    throw new InvoiceNumbersOutError(
      "This counter has no tax invoice numbers left for this month, so the bill was not " +
        "saved. Connect to the network and press Sync now to get more." +
        (state?.problem ? ` Head office says: ${state.problem}` : ""),
    );
  }
  const number = renderInvoiceNumber(block.prefix, block.fy, block.next);
  const problem = invoiceNumberProblem(number);
  if (problem) throw new InvoiceNumbersOutError(`${problem} The bill was not saved.`);
  await db.meta.put({
    key: META.numbering,
    value: {
      ...state,
      blocks: state.blocks.map((row) =>
        row.id === block.id ? { ...row, next: row.next + 1 } : row,
      ),
    } satisfies TillNumbering,
  });
  return number;
}

/**
 * Ask head office for this month's and next month's blocks, telling it what the
 * counter still holds. Online only; a failure leaves what the counter holds
 * exactly as it was.
 *
 * The answer is merged, not written over: a bill may have taken a number while
 * the request was out, so a block the counter already holds keeps its own
 * `next`. A block the answer leaves out is one head office will not vouch for any
 * more (its month has ended) and is dropped.
 */
export async function refreshNumbering(
  db: TillDb,
  transport: TillTransport,
): Promise<TillNumbering | null> {
  if (!transport.numberBlocks) return null;
  const before = await readMeta<TillNumbering | null>(db, META.numbering, null);
  const held: Record<string, number> = {};
  for (const block of before?.blocks ?? []) {
    if (block.next <= block.last) held[String(block.id)] = block.next;
  }
  let answer: NumberingPayload;
  try {
    answer = await transport.numberBlocks(held);
  } catch {
    return before;
  }
  return db.transaction("rw", db.meta, async () => {
    const current = await readMeta<TillNumbering | null>(db, META.numbering, null);
    const mine = new Map((current?.blocks ?? []).map((block) => [block.id, block]));
    const next: TillNumbering = {
      on: answer.on,
      new_format_from: answer.new_format_from,
      prefix: answer.prefix,
      block_size: answer.block_size,
      problem: answer.problem,
      blocks: answer.blocks.map((block) => ({
        ...block,
        next: Math.max(mine.get(block.id)?.next ?? block.first, block.first),
      })),
    };
    await db.meta.put({ key: META.numbering, value: next });
    return next;
  });
}

/** One line for the Till & Sync screen. */
export function numberingNote(state: TillNumbering | null, today: Date = new Date()): string {
  if (!state) return "Not received yet - sync to find out.";
  if (!state.on) return "Switched off - bills keep today's number";
  const day = localDay(today);
  if (!newFormatApplies(state, day)) return `New format from ${state.new_format_from}`;
  const left = numbersLeft(state, day.slice(0, 7));
  const prefix = state.prefix || "no prefix";
  return `${prefix}: ${left} invoice number${left === 1 ? "" : "s"} left this month`;
}
