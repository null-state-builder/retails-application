// Reports > Inventory (store operations ticket 43, ST-RPT-2): the plain words
// for one report. Pure helpers, so the wording is tested without a screen. The
// columns come from the server with each answer, so a store role's table and
// spreadsheet never carry a cost, margin or GMROI column the server did not send.

import { indiaDate } from "./salesReport";

export type InventoryGroupBy = "store" | "brand" | "category" | "season";

/** Sell-through over the chosen dates, or over each season from its start. */
export type InventorySpan = "period" | "season";

export const INVENTORY_GROUPINGS: { key: InventoryGroupBy; label: string }[] = [
  { key: "store", label: "Store" },
  { key: "brand", label: "Brand" },
  { key: "category", label: "Category" },
  { key: "season", label: "Season" },
];

export const INVENTORY_SPANS: { key: InventorySpan; label: string }[] = [
  { key: "period", label: "The chosen dates" },
  { key: "season", label: "The season to date" },
];

export interface InventoryFilters {
  store: string;
  date_from: string;
  date_to: string;
  group_by: InventoryGroupBy;
  span: InventorySpan;
}

/** This month so far, by store, sell-through over those dates. */
export function defaultInventoryFilters(now: Date = new Date()): InventoryFilters {
  const today = indiaDate(now);
  return {
    store: "",
    date_from: `${today.slice(0, 8)}01`,
    date_to: today,
    group_by: "store",
    span: "period",
  };
}

/** Only the filters that are set, as query parameters; the period span is the default. */
export function inventoryParams(filters: InventoryFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) {
    if (!value || (key === "span" && value === "period")) continue;
    out[key] = value;
  }
  return out;
}
