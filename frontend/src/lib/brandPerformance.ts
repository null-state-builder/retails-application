// Reports > Brand Performance (store operations ticket 45, ST-RPT-3): the plain
// words for one report. Pure helpers, so the wording is tested without a screen.
// The columns come from the server with each answer, so a store role's table and
// spreadsheet never carry a margin, commission or GMROI column the server did not
// send; the server also names which columns are estimates until OQ-50 is decided.

import { indiaDate } from "./salesReport";

export type BrandPerformanceGroupBy = "brand_store" | "brand";

export const BRAND_PERFORMANCE_GROUPINGS: { key: BrandPerformanceGroupBy; label: string }[] = [
  { key: "brand_store", label: "Brand and store" },
  { key: "brand", label: "Brand, every store" },
];

export interface BrandPerformanceFilters {
  store: string;
  date_from: string;
  date_to: string;
  group_by: BrandPerformanceGroupBy;
}

/** This month so far, each brand at each store. */
export function defaultBrandPerformanceFilters(now: Date = new Date()): BrandPerformanceFilters {
  const today = indiaDate(now);
  return {
    store: "",
    date_from: `${today.slice(0, 8)}01`,
    date_to: today,
    group_by: "brand_store",
  };
}

/** Only the filters that are set, as query parameters; brand and store is the default. */
export function brandPerformanceParams(filters: BrandPerformanceFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) {
    if (!value || (key === "group_by" && value === "brand_store")) continue;
    out[key] = value;
  }
  return out;
}

/** Is this column one the server labels an estimate (it waits on OQ-50)? */
export function isEstimate(key: string, estimateFields: readonly string[]): boolean {
  return estimateFields.includes(key);
}
