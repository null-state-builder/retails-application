// What a scan means, decided on the device (#181, D10 §4, requirement A2).
//
// A barcode is a scan-alias, not an identity. The same tag under two buying
// cohorts is two lots at two ticket prices, and the counter has to say which one
// walked out of the shop - offline, in the time between the beep and the line
// appearing.
//
// The ladder is the requirement's, exactly:
//
//   · one live batch under the barcode - straight through, no question;
//   · several - the **oldest live** one, because that is what a store sells first;
//   · genuinely ambiguous - a one-tap "which season?", the only question this
//     screen is ever allowed to ask mid-sale, and it never blocks the bill.
//
// "Oldest" is the season master's own ordering and not the alphabet: seasons are
// named, never dated (`masters.Season` is explicit), so FW25 sorting before SS26
// is an accident of the letters. The ordering rides down in the dataset for that
// reason, and this file is the mirror of `sell.services.resolve._season_rank`.
//
// One honest difference from the server. `resolve_piece` breaks its first tie on
// the season denormalised onto the store's stock row, which the dataset does not
// send (`stock` is a barcode and a quantity, H2). It costs nothing: the till
// writes the season it picked onto the line, and the accept pipeline honours an
// exact `(barcode, season)` outright rather than re-deciding. The two only need
// to agree about what a *good* answer looks like, and they do.

import type { TillDb } from "./db";
import type { TillItem, TillKnownCustomer, TillSeason, TillStock } from "./types";

/** Everything the counter knows that bears on a scan. */
export interface ScanWorld {
  items: TillItem[];
  stock: TillStock[];
  seasons: TillSeason[];
}

export interface ScanResult {
  /** The code as it will be billed - trimmed of whatever the wedge sent. */
  barcode: string;
  /** The piece the line takes unless a person says otherwise. Null when the
   *  counter has never heard of this barcode: a sold-before-inward line (#186),
   *  which is a manual line rather than a refused customer. */
  chosen: TillItem | null;
  /** Every season this barcode is known in, the choosable one first. */
  candidates: TillItem[];
  /** What the counter's own copy says is on the shelf under this barcode. Per
   *  barcode, not per season - `StockOnHand` is one row per (store, barcode), so
   *  there is no per-season count to show. */
  stock: number;
  /** More than one season to choose from, so the line offers the one-tap ask. */
  ambiguous: boolean;
}

/**
 * Rank two season names: negative when `a` should be billed first.
 *
 * A closed season sorts behind every open one however old it is, then the
 * master's own `sort_order`, then the name - so a counter scanning the same
 * piece twice gets the same answer twice. A season the master has never heard of
 * sorts last, not first: it is far more likely a typo on a PT than the oldest
 * thing in the shop.
 */
export function olderSeasonFirst(seasons: TillSeason[], a: string, b: string): number {
  const [closedA, orderA] = rank(seasons, a);
  const [closedB, orderB] = rank(seasons, b);
  return closedA - closedB || orderA - orderB || a.localeCompare(b);
}

const UNKNOWN: [number, number] = [1, Number.MAX_SAFE_INTEGER];

function rank(seasons: TillSeason[], name: string): [number, number] {
  const known = seasons.find((s) => s.code === name || s.name === name);
  if (!known) return UNKNOWN;
  return [known.status === "closed" ? 1 : 0, known.sort_order];
}

/**
 * The pieces a typed fragment could mean - the scan box's other job.
 *
 * D10 has one box doing both: a wedge types a barcode into it, and a person
 * types a name or a design number into it for the tag that will not scan or is
 * not there at all. Which of the two happened is not a mode the cashier picks -
 * a code that resolves is a scan and anything else is a search.
 *
 * One row per (barcode, season), because that is what a line is. Ranked by where
 * the match landed - the design number a person read off a tag beats a word
 * buried in an item name - and capped, because this feeds a list somebody reads
 * standing up.
 */
export function searchPieces(items: TillItem[], query: string, limit = 8): TillItem[] {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  const hits: { item: TillItem; rank: number }[] = [];
  for (const row of items) {
    const rank = matchRank(row, q);
    if (rank !== null) hits.push({ item: row, rank });
  }
  return hits
    .sort((a, b) => a.rank - b.rank || a.item.design.localeCompare(b.item.design))
    .slice(0, limit)
    .map((hit) => hit.item);
}

function matchRank(row: TillItem, q: string): number | null {
  const design = row.design.toLowerCase();
  if (design.startsWith(q)) return 0;
  if (row.barcode.toLowerCase().includes(q)) return 1;
  if (design.includes(q)) return 2;
  if (row.brand.toLowerCase().includes(q)) return 3;
  if (row.item.toLowerCase().includes(q)) return 4;
  return null;
}

/** How many digits the counter has to have typed before the phone book answers
 *  (#249). Three is the ticket's number, and it is a floor on the *typed*
 *  value: fewer than that and half the shop matches, which is a list nobody
 *  reads and a scroll of IndexedDB nobody needed. */
export const CUSTOMER_SEARCH_MIN = 3;

/**
 * The spellings of a part-typed mobile that could be sitting in the phone book.
 *
 * The book is keyed on the bare ten-digit form - the accept boundary collapses
 * `+91 98765-43210`, `09876543210` and `9876543210` to one customer
 * (`sell.services.customers.normalise_mobile`, mirrored here). But a *prefix*
 * cannot be collapsed the same way: that rule reads the whole number's length,
 * and a cashier three digits in has not typed a length yet. `91` is also a
 * perfectly ordinary start to an Indian mobile, so stripping it is a guess
 * either way.
 *
 * So this guesses neither and searches both: the digits as typed, plus the
 * digits with a country or trunk prefix taken off. Candidates shorter than the
 * floor are dropped rather than searched - `091` would otherwise ask the book
 * for everything starting `91`.
 */
export function mobilePrefixes(typed: string): string[] {
  const digits = (typed.match(/\d/g) ?? []).join("");
  if (digits.length < CUSTOMER_SEARCH_MIN) return [];
  const candidates = [digits];
  if (digits.startsWith("91")) candidates.push(digits.slice(2));
  if (digits.startsWith("0")) candidates.push(digits.slice(1));
  return [...new Set(candidates)].filter((p) => p.length >= CUSTOMER_SEARCH_MIN);
}

/**
 * Who the counter has billed before, under the number being typed (#249, D-3).
 *
 * Read straight off Dexie's `mobile` index rather than out of the till's
 * in-memory world, and that is the whole reason this is a function taking a
 * `db` instead of another array on `ScanWorld`: the phone book is the one
 * all-KDPS, unbounded table on the counter (grill Q6 - a Deoghar regular is
 * recognised in Ranchi), `useTillWorld` reloads after every bill, and holding
 * it in memory would re-read the entire business's customers from IndexedDB at
 * each sale, worsening every month, at the one screen where a stall is a queue
 * of people (design.md, amended at #245).
 *
 * The window read from the index is wider than the five shown, so that which
 * five a cashier sees is decided here (`namedFirst`, and the sharing-out
 * below) rather than by whichever numbers happen to sort lowest.
 *
 * Never throws: a phone book that cannot be read is a typeahead that says
 * nothing, not a bill that stops (Rule 5).
 */
export async function searchCustomers(
  db: TillDb,
  typed: string,
  limit = 5,
): Promise<TillKnownCustomer[]> {
  const prefixes = mobilePrefixes(typed);
  if (!prefixes.length) return [];
  try {
    // One read per candidate spelling, and then the five are shared out between
    // them a row at a time rather than ranked in one heap.
    //
    // Both halves of that matter. A single union read takes the lowest keys of
    // the whole match set, and every `91983…` sorts below every `983…` - so on
    // a book big enough to fill the window, a cashier typing `+91 983` would
    // get a screenful of numbers that merely start `91` and not one of the ones
    // they meant. Ranking the merged rows afterwards does not fix it: the same
    // ordering wins again. Sharing the list out does, and it is the honest
    // answer to a genuinely ambiguous prefix - neither reading of what was
    // typed is allowed to crowd the other off the screen.
    const windows = await Promise.all(
      prefixes.map(async (prefix) =>
        (await db.customers.where("mobile").startsWith(prefix).limit(limit * 5).toArray()).sort(
          namedFirst,
        ),
      ),
    );
    const picked = new Map<string, TillKnownCustomer>();
    for (let depth = 0; picked.size < limit && depth < limit; depth += 1) {
      for (const window of windows) {
        const row = window[depth];
        if (row && !picked.has(row.mobile)) picked.set(row.mobile, row);
        if (picked.size === limit) break;
      }
    }
    return [...picked.values()].sort(namedFirst);
  } catch {
    return [];
  }
}

/** A master built out of bills is full of mobiles nobody ever gave a name with,
 *  and five nameless numbers are a list that answers nothing - so a row
 *  somebody named comes first, and equals are ordered by the number itself so
 *  the same three digits give the same five twice. */
function namedFirst(a: TillKnownCustomer, b: TillKnownCustomer): number {
  return Number(Boolean(b.name)) - Number(Boolean(a.name)) || a.mobile.localeCompare(b.mobile);
}

/**
 * How a piece reads to a person - on the screen and on the paper alike.
 *
 * One function rather than two, because the receipt and the suggestion list are
 * describing the same garment: a customer comparing the two should not have to
 * work out that "LP Polo · LP-2231 · L · Black" and "LP Polo, LP-2231, L, Black"
 * are the same line. Fields the books left empty drop out rather than leaving
 * the separators stranded.
 */
export function describePiece(piece: {
  brand: string;
  item: string;
  design: string;
  size: string;
  color: string;
}): string {
  return [piece.brand, piece.item, piece.design, piece.size, piece.color].filter(Boolean).join(" · ");
}

/** What a scanned code resolves to, and what the counter should be told about it. */
export function resolveScan(code: string, world: ScanWorld): ScanResult {
  const barcode = code.trim();
  const candidates = world.items
    .filter((row) => row.barcode === barcode)
    .sort((a, b) => olderSeasonFirst(world.seasons, a.season, b.season));
  return {
    barcode,
    chosen: candidates[0] ?? null,
    candidates,
    stock: world.stock.find((row) => row.barcode === barcode)?.qty ?? 0,
    ambiguous: candidates.length > 1,
  };
}
