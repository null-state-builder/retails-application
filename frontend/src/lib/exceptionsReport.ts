// Reports > Exceptions (store operations ticket 48, ST-RPT-6): the plain words
// for one report. Pure helpers, so the wording is tested without a screen. The
// columns come from the server with each answer.

import { indiaDate } from "./salesReport";

export type ExceptionsGroupBy = "store" | "staff" | "list";

export const EXCEPTIONS_GROUPINGS: { key: ExceptionsGroupBy; label: string }[] = [
  { key: "store", label: "By store" },
  { key: "staff", label: "By staff member" },
  { key: "list", label: "Each exception" },
];

export interface ExceptionsFilters {
  store: string;
  date_from: string;
  date_to: string;
  group_by: ExceptionsGroupBy;
}

/** This month so far, by store. */
export function defaultExceptionsFilters(now: Date = new Date()): ExceptionsFilters {
  const today = indiaDate(now);
  return {
    store: "",
    date_from: `${today.slice(0, 8)}01`,
    date_to: today,
    group_by: "store",
  };
}

/** Only the filters that are set, as query parameters. */
export function exceptionsParams(filters: ExceptionsFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}
