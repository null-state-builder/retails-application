// Brands > Margin Share (store operations ticket 27, ST-BRD-4): the plain words
// for the monthly statement. Pure helpers, so they are tested without a screen.
// The columns come from the server with each answer, as the other reports' do,
// so the table and the spreadsheet can never disagree.

import { indiaDate } from "./salesReport";

export interface MarginFilters {
  store: string;
  /** YYYY-MM. */
  month: string;
  /** A brand's id, or blank for every brand. */
  brand: string;
}

export interface MarginFigures {
  value_paise: number;
  kdps_paise: number | null;
  brand_paise: number | null;
  unsplit_paise?: number;
}

/** This month, India time, every brand, all my stores. */
export function defaultMarginFilters(now: Date = new Date()): MarginFilters {
  return { store: "", month: indiaDate(now).slice(0, 7), brand: "" };
}

/** Only the filters that are set, as query parameters. */
export function marginParams(filters: MarginFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}

/** The brand's id from a statement row's key (`brand:12`), or null for a line
 *  whose brand is not one brand in the brand list, which has no statement. */
export function brandIdOf(key: string): string | null {
  const found = /^brand:(\d+)$/.exec(key);
  return found?.[1] ?? null;
}

/** Sale value = KDPS's share + brand's share + not split, the statement's own rule. */
export function splitAddsUp(row: MarginFigures): boolean {
  return (
    row.value_paise === (row.kdps_paise ?? 0) + (row.brand_paise ?? 0) + (row.unsplit_paise ?? 0)
  );
}

/** The file name a statement downloads under. */
export function exportName(filters: MarginFilters, brandName: string | null): string {
  const who = brandName
    ? brandName
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "")
    : "all-brands";
  return `margin-share-${who || "brand"}-${filters.month}.xlsx`;
}
