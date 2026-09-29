// Reports > Shrinkage (store operations ticket 44, ST-INV-4): the plain words
// for one report. Pure helpers, so the wording is tested without a screen. The
// columns come from the server with each answer, so a store role's table and
// spreadsheet never carry a cost column the server did not send.

import { indiaDate } from "./salesReport";

export type ShrinkageGroupBy = "store" | "brand" | "category" | "month";

export const SHRINKAGE_GROUPINGS: { key: ShrinkageGroupBy; label: string }[] = [
  { key: "store", label: "Store" },
  { key: "brand", label: "Brand" },
  { key: "category", label: "Category" },
  { key: "month", label: "Month" },
];

export interface ShrinkageFilters {
  store: string;
  date_from: string;
  date_to: string;
  group_by: ShrinkageGroupBy;
}

/** This month so far, by store. */
export function defaultShrinkageFilters(now: Date = new Date()): ShrinkageFilters {
  const today = indiaDate(now);
  return { store: "", date_from: `${today.slice(0, 8)}01`, date_to: today, group_by: "store" };
}

/** Only the filters that are set, as query parameters. */
export function shrinkageParams(filters: ShrinkageFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}
