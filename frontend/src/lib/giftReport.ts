// Reports > Gift Stock (store operations ticket 14, ST-CMP-7): the filters and
// views of one report. The cells and columns are the GST report's own helpers:
// the server sends the columns with each answer, and leaves out cost and credit
// for a viewer who may not see them.

import { defaultGstFilters } from "./gstReport";

export type GiftView = "gstin" | "pieces";

export const GIFT_VIEWS: { key: GiftView; label: string }[] = [
  { key: "gstin", label: "By GSTIN" },
  { key: "pieces", label: "Each gift piece" },
];

export interface GiftFilters {
  store: string;
  date_from: string;
  date_to: string;
  view: GiftView;
}

/** Last month, whole: Accounts reverses the credit for the month just ended. */
export function defaultGiftFilters(now: Date = new Date()): GiftFilters {
  const { store, date_from, date_to } = defaultGstFilters(now);
  return { store, date_from, date_to, view: "gstin" };
}

/** Only the filters that are set, as query parameters. */
export function giftParams(filters: GiftFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}
