/**
 * The cross-store availability search, minus the rendering (#175, D10 §3).
 *
 * The screen's judgements live here rather than inside the component, so the
 * ones that matter — what counts as a searchable term, what a quoted time means
 * once it leaves the browser, and exactly what body "Request this" posts — are
 * unit-testable without a DOM, which is this project's convention.
 */

import type { LocationT } from "./transfer-locations";

/** Mirrors `StockAvailabilityView.MIN_TERM`. Held here so the screen can say so
 *  before it asks, rather than showing the server's refusal as an error. */
export const MIN_TERM = 3;

/** One SKU at one store — the innermost entry, and the thing a request is
 *  raised from. Quantities only: the endpoint carries no cost by construction
 *  (`masters.scope_exceptions.CROSS_STORE_AVAILABILITY`), so there is nothing
 *  money-shaped for this screen to render even by accident. */
export interface AvailabilityStoreT {
  store: string;
  store_name: string;
  color: string;
  sku_code: string;
  hsn: string;
  season: string;
  qty: number;
}

export interface AvailabilitySizeT {
  size: string;
  stores: AvailabilityStoreT[];
}

export interface AvailabilityDesignT {
  design: string;
  brand: string;
  item: string;
  sizes: AvailabilitySizeT[];
}

export interface AvailabilityResponseT {
  results: AvailabilityDesignT[];
  truncated: boolean;
}

/** One line of the table under a design heading: a size, and one place holding
 *  it. Flattened for rendering because "L · RAN-WH · Blue · 7" is one row to the
 *  person reading it, while the nesting is what made the sizes group. */
export interface AvailabilityRowT extends AvailabilityStoreT {
  size: string;
}

export function rowsFor(design: AvailabilityDesignT): AvailabilityRowT[] {
  return design.sizes.flatMap((s) => s.stores.map((store) => ({ ...store, size: s.size })));
}

/** How many pieces this design has anywhere — the line the store reads first. */
export function totalQty(design: AvailabilityDesignT): number {
  return rowsFor(design).reduce((sum, row) => sum + row.qty, 0);
}

/** A `datetime-local` value as the instant the API stores, or null.
 *
 * The input hands over wall-clock letters with no zone ("2026-08-02T17:30");
 * the field is a moment. Converting here rather than posting the raw string is
 * what stops a time quoted at half five reading back as half eleven. An
 * unparseable or empty value is *no quote given*, never a wrong one.
 */
export function quotedArrivalIso(local: string): string | null {
  const trimmed = local.trim();
  if (!trimmed) return null;
  const when = new Date(trimmed);
  return Number.isNaN(when.getTime()) ? null : when.toISOString();
}

/** The id of the location with this code — the search answers in codes, and a
 *  request is raised against a store row. Null when the list has not arrived or
 *  the place is not one this person may send to. */
export function storeIdByCode(locations: LocationT[], code: string): number | null {
  return locations.find((l) => l.code === code)?.id ?? null;
}

/** A row the counter has pointed at, with the design it sits under — the two
 *  always travel together, because a row alone names a size and a place but not
 *  the style they belong to. */
export interface PickedRowT {
  design: AvailabilityDesignT;
  row: AvailabilityRowT;
}

export interface RequestDraftT extends PickedRowT {
  requestingStoreId: number;
  fulfillingStoreId: number;
  qty: number;
  /** Raw `datetime-local` value; converted here, so callers never post one. */
  expectedArrivalLocal: string;
  notes: string;
}

/**
 * What "Request this" posts.
 *
 * `source` is fixed rather than passed: every ask built by this screen came from
 * the search, and making it an argument would only create a way to raise one
 * that lies about where it came from.
 */
export function requestBody(draft: RequestDraftT) {
  const { design, row } = draft;
  return {
    requesting_store: draft.requestingStoreId,
    fulfilling_store: draft.fulfillingStoreId,
    notes: draft.notes,
    source: "cross_store_search" as const,
    expected_arrival_at: quotedArrivalIso(draft.expectedArrivalLocal),
    lines: [
      {
        sku_code: row.sku_code,
        design: design.design,
        color: row.color,
        size: row.size,
        brand: design.brand,
        season: row.season,
        item: design.item,
        hsn: row.hsn,
        qty: draft.qty,
      },
    ],
  };
}
