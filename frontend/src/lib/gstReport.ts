// Reports > GST (store operations ticket 47, ST-RPT-5): the plain words and
// cells for one report. Pure helpers, so the wording is tested without a
// screen. The columns come from the server with each answer, so the table and
// the spreadsheet can never disagree about what a view holds.

import { apiErrorMessage } from "./api";
import { formatINR } from "./format";
import { indiaDate } from "./salesReport";

export type GstView = "rate" | "hsn" | "b2b" | "credit_notes" | "documents";

export const GST_VIEWS: { key: GstView; label: string }[] = [
  { key: "rate", label: "By rate" },
  { key: "hsn", label: "By HSN (Table 12)" },
  { key: "b2b", label: "B2B invoices and IRN" },
  { key: "credit_notes", label: "Credit notes" },
  { key: "documents", label: "Documents issued (Table 13)" },
];

export interface GstFilters {
  store: string;
  date_from: string;
  date_to: string;
  view: GstView;
}

export type CellKind = "text" | "money" | "number" | "rate" | "date";

export interface GstColumn {
  key: string;
  label: string;
  kind: CellKind;
}

export type GstRow = Record<string, unknown> & { key?: string };

/** Last month, whole: GST is filed for the month that has just ended. */
export function defaultGstFilters(now: Date = new Date()): GstFilters {
  const today = indiaDate(now);
  const year = Number(today.slice(0, 4));
  const month = Number(today.slice(5, 7)); // 1-12, this month
  const lastYear = month === 1 ? year - 1 : year;
  const lastMonth = month === 1 ? 12 : month - 1;
  const days = new Date(Date.UTC(lastYear, lastMonth, 0)).getUTCDate();
  const mm = String(lastMonth).padStart(2, "0");
  return {
    store: "",
    date_from: `${lastYear}-${mm}-01`,
    date_to: `${lastYear}-${mm}-${String(days).padStart(2, "0")}`,
    view: "rate",
  };
}

/** Only the filters that are set, as query parameters. */
export function gstParams(filters: GstFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}

/** One cell in words. Absent is a dash, never a nought or an empty box. */
export function gstCell(value: unknown, kind: CellKind): string {
  if (value === null || value === undefined || value === "") return "—";
  switch (kind) {
    case "money":
      return formatINR(Number(value));
    case "rate":
      return `${Number(value)}%`;
    case "number":
      return String(Number(value));
    case "date":
      return new Date(`${String(value)}T00:00:00+05:30`).toLocaleDateString("en-IN", {
        timeZone: "Asia/Kolkata",
        day: "numeric",
        month: "short",
        year: "numeric",
      });
    default:
      return String(value);
  }
}

/** Numbers and money read right-aligned; words and dates do not. */
export function alignsRight(kind: CellKind): boolean {
  return kind === "money" || kind === "number" || kind === "rate";
}

/** A refused download arrives as a Blob; read the server's sentence out of it. */
export async function blobErrorMessage(reason: unknown): Promise<string> {
  const data = (reason as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text()) as unknown;
      return apiErrorMessage({ response: { data: parsed } });
    } catch {
      return "The export could not be made. Please try again.";
    }
  }
  return apiErrorMessage(reason);
}
