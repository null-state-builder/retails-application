// Reports > Staff Performance (store operations ticket 46, ST-RPT-4): the
// filters and the target amounts for one report. Pure helpers, so they are
// tested without a screen. The columns come from the server with each answer.

import { indiaDate } from "./salesReport";

export interface StaffFilters {
  store: string;
  date_from: string;
  date_to: string;
}

/** This month so far, at all my stores. */
export function defaultStaffFilters(now: Date = new Date()): StaffFilters {
  const today = indiaDate(now);
  return { store: "", date_from: `${today.slice(0, 8)}01`, date_to: today };
}

/** Only the filters that are set, as query parameters. */
export function staffParams(filters: StaffFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}

/** Targets are set per month: the month the period starts in, YYYY-MM. */
export function targetMonth(filters: StaffFilters): string {
  return filters.date_from.slice(0, 7);
}

const AMOUNT = /^\d+(\.\d{1,2})?$/;

/** A rupee amount as typed ("1,25,000.50") in whole paise, or null if it is not one. */
export function rupeesToPaise(text: string): number | null {
  const cleaned = text.trim().replace(/,/g, "");
  if (!AMOUNT.test(cleaned)) return null;
  const [whole, fraction = ""] = cleaned.split(".");
  return Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
}

/** A stored target as the rupees the box shows; blank for none. */
export function paiseToRupeeText(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "";
  const rupees = Math.floor(paise / 100);
  const rest = paise % 100;
  return rest ? `${rupees}.${String(rest).padStart(2, "0")}` : String(rupees);
}
