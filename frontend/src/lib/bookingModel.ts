// One Bookings screen over two booking engines (store and warehouse operations
// PRD §12: "the two booking screens become one destination per persona").
//
// Every store runs on one stock system, and each system keeps its own bookings:
// the older one (`/api/bookings`, the real KDPS stores today) and goods-v1
// (`/api/goods-v1/bookings`). Both stay underneath. What a person sees is one
// list, one booking page and one form, so this file turns a row from either
// engine into the same card, with the same status words and the same progress.
//
// Pure: no API calls and no React, so it is tested on its own and neither
// engine's page has to know the other's shape.

export type BookingEngine = "legacy" | "goods";

/** The status words both engines share. Nobody sees "old", "new" or "vendor". */
export type BookingStatus =
  | "draft"
  | "waiting"
  | "booked"
  | "part"
  | "all"
  | "closed"
  | "cancelled";

export const STATUS_WORD: Record<BookingStatus, string> = {
  draft: "Draft",
  waiting: "Waiting for approval",
  booked: "Booked",
  part: "Part arrived",
  all: "All arrived",
  closed: "Closed early",
  cancelled: "Cancelled",
};

export const STATUS_TONE: Record<BookingStatus, string> = {
  draft: "grey",
  waiting: "amber",
  booked: "green",
  part: "amber",
  all: "blue",
  closed: "navy",
  cancelled: "red",
};

/** Still something to do or to wait for. Everything else is done. */
export function isOpenStatus(status: BookingStatus): boolean {
  return status === "draft" || status === "waiting" || status === "booked" || status === "part";
}

/** How far a booked, unended booking has got, from pieces booked and arrived. */
function arrivalStatus(booked: number, arrived: number): BookingStatus {
  if (arrived <= 0) return "booked";
  return arrived < booked ? "part" : "all";
}

const LEGACY_STATUS: Record<string, BookingStatus> = {
  draft: "draft",
  submitted: "waiting",
  booked: "booked",
  partially_received: "part",
  received: "all",
  closed: "closed",
  cancelled: "cancelled",
};

/** The older engine's status. An unknown one reads as booked rather than blank. */
export function legacyStatus(status: string): BookingStatus {
  return LEGACY_STATUS[status] ?? "booked";
}

/** Goods-v1 has four document states; a confirmed one says how far it has got
 *  by what has arrived against it. */
export function goodsStatus(state: string, booked: number, arrived: number): BookingStatus {
  if (state === "draft") return "draft";
  if (state === "short_closed") return "closed";
  if (state === "cancelled") return "cancelled";
  return arrivalStatus(booked, arrived);
}

/** One booking as the list draws it, whichever engine holds it. */
export interface BookingCard {
  engine: BookingEngine;
  /** The id in its own engine: a number for the older one, a uuid for goods-v1. */
  id: string;
  /** Null until a goods-v1 draft is confirmed. */
  number: string | null;
  status: BookingStatus;
  brand: string;
  vendor: string;
  season: string;
  store: string;
  booked: number;
  arrived: number;
  /** YYYY-MM-DD, when the booking names one. */
  expected: string | null;
  createdAt: string;
}

/** A row of `GET /api/bookings`, as much of it as the card reads. */
export interface LegacyBookingRow {
  id: number;
  number: string;
  status: string;
  brand_name: string;
  vendor_name: string;
  season_name?: string;
  season_code: string;
  destination_store_name: string | null;
  booked_total: number;
  received_total: number;
  created_at?: string;
  lines?: { store_name: string | null }[];
}

/** A row of `GET /api/goods-v1/bookings`, as much of it as the card reads. */
export interface GoodsBookingRow {
  id: string;
  number: string | null;
  state: string;
  created_at: string;
  due_at?: string | null;
  booked_qty?: number;
  arrived_qty?: number;
  names?: {
    vendor?: string | null;
    brand?: string | null;
    season?: string | null;
    site?: string | null;
  };
}

/** Where the goods go, in words: the booking's store, or its lines' stores. */
function legacyStore(row: LegacyBookingRow): string {
  if (row.destination_store_name) return row.destination_store_name;
  const named = [...new Set((row.lines ?? []).map((l) => l.store_name).filter(Boolean))];
  return named.join(", ");
}

export function legacyCard(row: LegacyBookingRow): BookingCard {
  return {
    engine: "legacy",
    id: String(row.id),
    number: row.number,
    status: legacyStatus(row.status),
    brand: row.brand_name,
    vendor: row.vendor_name,
    season: row.season_name || row.season_code,
    store: legacyStore(row),
    booked: row.booked_total,
    arrived: row.received_total,
    expected: null,
    createdAt: row.created_at ?? "",
  };
}

export function goodsCard(row: GoodsBookingRow): BookingCard {
  const booked = row.booked_qty ?? 0;
  const arrived = Math.max(0, row.arrived_qty ?? 0);
  return {
    engine: "goods",
    id: row.id,
    number: row.number,
    status: goodsStatus(row.state, booked, arrived),
    brand: row.names?.brand ?? "",
    vendor: row.names?.vendor ?? "",
    season: row.names?.season ?? "",
    store: row.names?.site ?? "",
    booked,
    arrived,
    expected: row.due_at ? row.due_at.slice(0, 10) : null,
    createdAt: row.created_at,
  };
}

/** Arrived as a share of booked, 0-100. More than booked still reads 100. */
export function arrivedPercent(booked: number, arrived: number): number {
  if (booked <= 0) return 0;
  return Math.min(100, Math.round((Math.max(0, arrived) / booked) * 100));
}

/** The words a person half-remembers about a booking: number, brand, vendor,
 *  season, store. Case ignored; an empty search matches everything. */
export function cardMatches(card: BookingCard, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [card.number, card.brand, card.vendor, card.season, card.store]
    .filter(Boolean)
    .some((text) => (text as string).toLowerCase().includes(needle));
}

export type BookingFilter = "open" | "done" | "all";

/** Newest first; the list's one order. */
export function newestFirst(cards: BookingCard[]): BookingCard[] {
  return [...cards].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

/** The list for one chip, and how many each chip holds (after the search). */
export function filterCards(
  cards: BookingCard[],
  filter: BookingFilter,
  query: string,
): { shown: BookingCard[]; counts: Record<BookingFilter, number> } {
  const found = newestFirst(cards.filter((card) => cardMatches(card, query)));
  const open = found.filter((card) => isOpenStatus(card.status));
  const counts = { open: open.length, done: found.length - open.length, all: found.length };
  const shown =
    filter === "open" ? open : filter === "done" ? found.filter((c) => !isOpenStatus(c.status)) : found;
  return { shown, counts };
}

/** A goods-v1 booking id is a uuid; an older one is a number. The booking page
 *  at `/booking/<id>` picks its engine by that shape. */
export function engineOfId(id: string): BookingEngine {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)
    ? "goods"
    : "legacy";
}

/** The engine a store's bookings live in: the stock system it runs on. */
export function engineOfStore(stockContract: string | null | undefined): BookingEngine {
  return stockContract === "goods_v1" ? "goods" : "legacy";
}

/** One line's money facts, in integer paise; null is "nobody gave one". */
export interface CostedLine {
  qty: number;
  costPaise: number | null;
  mrpPaise: number | null;
}

/** Pieces, cost and MRP value over the lines. A money total is null when no
 *  line carries that figure: an unknown is never added up as a zero. */
export function lineTotals(lines: CostedLine[]): {
  pieces: number;
  costPaise: number | null;
  mrpPaise: number | null;
} {
  const sum = (pick: (line: CostedLine) => number | null) => {
    const given = lines.filter((line) => pick(line) !== null);
    return given.length ? given.reduce((total, line) => total + line.qty * (pick(line) ?? 0), 0) : null;
  };
  return {
    pieces: lines.reduce((total, line) => total + line.qty, 0),
    costPaise: sum((line) => line.costPaise),
    mrpPaise: sum((line) => line.mrpPaise),
  };
}

/** Integer paise from a goods-v1 money string ("129900"), or null. */
export function paiseOf(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
