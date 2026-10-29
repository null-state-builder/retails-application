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

/** True when the reporting copy a report is built from has never been built:
 *  every figure is unknown then, however the server filled the rows. The
 *  server says so in `missing` as `NOT_BUILT`. A second copy it also reads
 *  says `<COPY>_NOT_BUILT`, which makes only that copy's figures unknown. */
export function figuresUnknown(missing: readonly { code: string }[]): boolean {
  return missing.some((m) => m.code === "NOT_BUILT");
}

/** What a report tells `maskUnknownFigures` about its own columns. */
export interface FigureSources {
  /** Columns that tell rows apart and are never figures (the GST rate a
   *  by-rate row stands for). Words and dates are always kept. */
  keep?: readonly string[];
  /** Per second copy, by the prefix of its `<COPY>_NOT_BUILT` code: the
   *  columns worked out from it. */
  copies?: Readonly<Record<string, readonly string[]>>;
}

/** The row with the figures nobody can know blanked, so `gstCell` prints a
 *  dash where the server sent a nought: every figure while the main copy is
 *  missing, else only the columns a missing second copy feeds. Words, dates
 *  and `keep` columns are never blanked. */
export function maskUnknownFigures<T extends GstRow>(
  row: T,
  columns: readonly GstColumn[],
  missing: readonly { code: string }[],
  sources: FigureSources = {},
): T {
  const all = figuresUnknown(missing);
  const fromCopies = new Set(
    missing.flatMap((m) =>
      m.code.endsWith("_NOT_BUILT")
        ? (sources.copies?.[m.code.slice(0, -"_NOT_BUILT".length)] ?? [])
        : [],
    ),
  );
  if (!all && !fromCopies.size) return row;
  const out: GstRow = { ...row };
  for (const c of columns) {
    if (c.kind === "text" || c.kind === "date" || sources.keep?.includes(c.key)) continue;
    if (all || fromCopies.has(c.key)) out[c.key] = null;
  }
  return out as T;
}

/** A column's heading on screen: the server writes "(Rs)" for the Excel
 *  export; the screen shows the rupee sign. */
export function columnHeading(label: string): string {
  return label.replace(/\s*\(Rs\)/g, " (₹)");
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
      return Number(value).toLocaleString("en-IN");
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
