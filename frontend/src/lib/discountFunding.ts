// Reports > Discount Funding (store operations ticket 25, ST-OFR-2): the plain
// words for one report. Pure helpers, so they are tested without a screen. The
// columns come from the server with each answer, as the GST report's do, so
// the table and the spreadsheet can never disagree.

import { indiaDate } from "./salesReport";

export type FundingGrouping = "offer" | "brand" | "store";

export const FUNDING_GROUPINGS: { key: FundingGrouping; label: string }[] = [
  { key: "offer", label: "By offer" },
  { key: "brand", label: "By brand" },
  { key: "store", label: "By store" },
];

export interface FundingFilters {
  store: string;
  date_from: string;
  date_to: string;
  group_by: FundingGrouping;
}

export interface FundingFigures {
  discount_paise: number;
  brand_paise: number;
  kdps_paise: number;
  unknown_paise: number;
}

/** This month so far: the 1st to today, India time, by offer. */
export function defaultFundingFilters(now: Date = new Date()): FundingFilters {
  const today = indiaDate(now);
  return { store: "", date_from: `${today.slice(0, 8)}01`, date_to: today, group_by: "offer" };
}

/** Only the filters that are set, as query parameters. */
export function fundingParams(filters: FundingFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}

/** Discount = brand's share + KDPS's share + unknown, the report's own rule. */
export function splitAddsUp(row: FundingFigures): boolean {
  return row.discount_paise === row.brand_paise + row.kdps_paise + row.unknown_paise;
}
