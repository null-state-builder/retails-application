// Stock reads (E171-E176) and the origin journey (E181) — the wire shapes and
// the pure display rules the "Stock" and "Origin journey" screens share.
//
// Nothing here recomputes a quantity, an eligibility reason or a value: those
// are the server's own answers (`stockledger/goods_reads.py`). What lives here
// is how a *reader* labels, filters and groups what the server already decided.
//
// That includes how complete a past answer is (R27): the server declares its
// own limitations, and this file only names them in the reader's words.

import type { paths } from "./api-schema";

/** `state` on a stock row (R23): which part of the row the caller is asking
 *  about. Journal `state` is a posting kind (P01-P18) instead — a different
 *  filter, not offered here. */
export const STOCK_STATES = [
  "available",
  "accepted",
  "not_accepted",
  "held",
  "reserved",
  "unvalued",
] as const;
export type StockState = (typeof STOCK_STATES)[number];

export const STOCK_STATE_LABEL: Record<StockState, string> = {
  available: "Available",
  accepted: "Accepted",
  not_accepted: "Not accepted",
  held: "Held",
  reserved: "Reserved",
  unvalued: "Unvalued",
};

export const BASES = ["quantity", "cost", "ticket"] as const;
export type Basis = (typeof BASES)[number];

export const BASIS_LABEL: Record<Basis, string> = {
  quantity: "Quantity only",
  cost: "Cost value",
  ticket: "Ticket (MRP) value",
};

/** `stockledger.goods_engine.ELIGIBILITY_REASONS`, in the reader's words. An
 *  unlisted code still renders — humanised from its own spelling — rather
 *  than disappearing. */
export const ELIGIBILITY_LABELS: Record<string, string> = {
  IDENTITY_UNRESOLVED: "No SKU resolved",
  VALUE_MISSING: "Not valued",
  NOT_OFFICIAL: "No live official record",
  NOT_ACCEPTED: "Not yet accepted",
  CONDITION_NOT_GOOD: "Not in good condition",
  LOCATION_NOT_ELIGIBLE: "Location not eligible",
  HOLD_ACTIVE: "On hold",
  RESERVED: "Reserved",
  SITE_NOT_GOODS_READY: "Site not goods-ready",
  COUNT_FROZEN: "A stock count is in progress",
  IN_TRANSIT: "In transit",
  WRONG_SITE: "Wrong site",
  OUTSIDE_EFFECTIVE_PERIOD: "Outside its effective period",
};

export function eligibilityLabel(code: string): string {
  return ELIGIBILITY_LABELS[code] || code.replace(/_/g, " ").toLowerCase();
}

/** `StockDTO` (design §6.1, `goods_engine.build_stock_rows`/`transit_rows`). */
export interface StockRow {
  site_id: string | null;
  location_id: string | null;
  sku_id: string | null;
  description: string;
  origin_id: string | null;
  source_kind: string;
  condition: string;
  physical_qty: number;
  valued_qty: number;
  accepted_qty: number;
  held_qty: number;
  reserved_qty: number;
  ats_qty: number;
  transferable_qty: number;
  eligibility_reasons: string[];
  /** R27 (`goods_reads.limitations`): empty on a live read. */
  limitations: string[];
  as_of: string;
  record_contract: "goods-v1";
  /** OPS-03. The season frozen on this row's origin, as it stands now: a later
   *  governed correction shows here, and the season the row opened under stays
   *  beside it in `season_original`. Absent on a row with no origin to read. */
  season_id?: string | null;
  season_label?: string;
  season_unknown_historical?: boolean;
  season_original?: {
    season_id: string | null;
    season_label: string;
    season_unknown_historical: boolean;
  } | null;
  cost_value_paise?: string | null;
  ticket_value_paise?: string | null;
  // In-transit rows only (E174).
  transfer_id?: string;
  source_site_id?: string;
  destination_site_id?: string;
}

export function unvaluedQty(row: StockRow): number {
  return Math.max(0, row.physical_qty - row.valued_qty);
}

/** The Stock screen's `season=` filter value for the one explicit unknown
 *  historical cohort (OPS-03): a meaning rather than an id, so the link
 *  survives a reseeded master. */
export const SEASON_UNKNOWN_HISTORICAL = "unknown_historical";

/** What a row's season reads as. The server sends the season's own name, so
 *  this only fills the gap for a row with no origin to read a season off. */
export function seasonLabel(row: StockRow): string {
  return row.season_label || "—";
}

/** `StockSummaryDTO` (E172). Value totals appear only for a non-quantity basis
 *  (`goods_reads.summary`), and even then only once every counted piece is
 *  valued — a partial or unknown total is `null`, never a guessed zero. */
export interface StockSummary {
  scope: {
    entity_id: string | null;
    site_ids: string[];
    sbu_ids: string[];
    brand_ids: string[];
    scope_kind: string;
  };
  basis: Basis;
  as_of: string;
  /** R27 (`goods_reads.limitations`): empty on a live read. */
  limitations: string[];
  totals: {
    physical_qty: number;
    valued_qty: number;
    unvalued_qty: number;
    ats_qty: number;
    transferable_qty: number;
    transit_qty: number;
    value_paise?: string | null;
    valued_value_paise?: string | null;
    value_completeness?: "complete" | "partial" | "unknown";
  };
}

/** The closed limitation vocabulary, straight from the generated client
 *  (`goods_reads.READ_LIMITATIONS`) so a code the server stops or starts
 *  sending cannot drift from what this screen knows how to say. */
export type StockReadLimitation = NonNullable<
  NonNullable<
    paths["/api/goods-v1/stockledger/summary"]["get"]["responses"][200]["content"]["application/json"]
  >["limitations"]
>[number];

/** Why the limitation applies at all — the one sentence above the list. */
export const LIMITATION_INTRO =
  "This is a past view, replayed from the journal (change PRD R27). Quantities, holds " +
  "and reservations are as they stood then. The server says these parts of the answer " +
  "are still read from today's records, because no history is kept for them:";

export const LIMITATION_LABELS: Record<StockReadLimitation, string> = {
  current_site_readiness:
    "Site readiness is today's, not how it stood then — a reason that depends on it may " +
    "not be the reason that applied at the time.",
  current_location_kind:
    "A location's kind is today's, not how it stood then — a location re-purposed since " +
    "is judged by what it is now.",
  current_cost_projection:
    "Cost and ticket (MRP) records are today's, not how they stood then — a price " +
    "corrected since is shown at its corrected value.",
};

/** An unlisted code still renders, humanised from its own spelling, rather
 *  than disappearing: an undeclared limitation would read as no limitation. */
export function limitationLabel(code: string): string {
  return (
    LIMITATION_LABELS[code as StockReadLimitation] ||
    `Not replayed: ${code.replace(/_/g, " ").toLowerCase()}.`
  );
}

/** The part of a stock read's state this file needs. Matches the `ReadState`
 *  the Stock screen already keeps, structurally, so the screen hands its
 *  reads over as they are. */
export interface ReadAnswer<T> {
  loading: boolean;
  data: T | null;
  deniedField: boolean;
  deniedAction: boolean;
  failure: string;
}

/** What a read said about the question it was last *asked*, or null.
 *
 *  `goodsScreen.useGoodsFetch` keeps the previous response while a new URL
 *  loads and keeps it when the new read is refused, so a read that has not
 *  answered yet is still holding the answer to the question before it. Reading
 *  a limitation off that would warn about the wrong answer — the old live view
 *  under a fresh past one, or worse, a past warning over live rows. */
function answered<T>(read: ReadAnswer<T> | null): T | null {
  if (!read || read.loading || read.deniedField || read.deniedAction || read.failure) return null;
  return read.data;
}

/** The limitations to show for one screenful, taken from whichever read the
 *  server actually answered — never worked out from the `as_of` the caller
 *  typed. The server clamps a future or malformed timestamp back to a live
 *  read (`goods_reads._as_of`) and then declares nothing, and a read the
 *  caller is refused or that is still in flight declares nothing either; in
 *  every one of those cases this is empty, which is the truthful answer rather
 *  than a guessed warning.
 *
 *  The summary is asked first because it answers even when the row page is
 *  empty; an empty page over a past watermark is still a past view. */
export function readLimitations(
  summary: ReadAnswer<Pick<StockSummary, "limitations">> | null,
  rows: ReadAnswer<{ items: StockRow[] }> | null,
): string[] {
  return answered(summary)?.limitations ?? answered(rows)?.items?.[0]?.limitations ?? [];
}

/** Whether `siteType` (the session's own `sites[].type`, Store.StoreType) is a
 *  warehouse — the column label a stock row should carry (R28: a warehouse
 *  reports transferable, never sellable). Judged from the site's own kind,
 *  never guessed back from which quantity a row happens to carry: a fully
 *  held warehouse row carries neither `ats_qty` nor `transferable_qty`, and a
 *  quantity-based guess reads a row like that as an ordinary (and wrongly
 *  sellable-labelled) store row. */
export function isWarehouseKind(siteType: string | undefined): boolean {
  return siteType === "warehouse";
}

// ---------------------------------------------------------------------------
// Origin journey (E181)
// ---------------------------------------------------------------------------

/** `JourneyDTO` (`stockledger.goods_reads.journey_page`). */
export interface JourneyEvent {
  event_id: string;
  event_kind: string;
  document_id: string | null;
  number: string | null;
  version: number | null;
  source_site_id: string | null;
  destination_site_id: string | null;
  qty: number;
  source_evidence_ref: string;
  event_at: string;
  recorded_at: string;
  value_paise?: string;
}

/** The published event names in order (R26): `origin_created`,
 *  `coverage_cover`/`coverage_counter`, `posting_Pxx`, `acceptance_<outcome>`,
 *  `hold_<effect>`, `reservation_<effect>`. Humanised for the timeline, never
 *  read as a serial history of an individual piece. */
export function journeyEventLabel(kind: string): string {
  if (kind === "booking_confirmed") return "Booked";
  if (kind === "goods_arrived") return "Goods arrived";
  if (kind === "origin_created") return "Origin created";
  if (kind === "coverage_cover") return "Covered by a PT";
  if (kind === "coverage_counter") return "Coverage reversed";
  // Goods ticket 15D: the pieces physically left custody (P21), whatever their value.
  if (kind === "posting_P21") return "Disposed of (P21)";
  if (kind.startsWith("posting_")) return `Stock posted (${kind.slice("posting_".length)})`;
  if (kind.startsWith("acceptance_")) {
    const outcome = kind.slice("acceptance_".length).replace(/_/g, " ");
    return `Accepted: ${outcome}`;
  }
  if (kind.startsWith("hold_")) return `Hold ${kind.slice("hold_".length)}d`;
  if (kind.startsWith("reservation_")) return `Reservation ${kind.slice("reservation_".length)}d`;
  return kind.replace(/_/g, " ");
}
