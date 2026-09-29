// Reports > Sales (store operations ticket 10, ST-RPT-1): the plain words and
// columns for one report. Pure helpers, so the wording is tested without a
// screen. What a viewer may see is the server's decision: a column for cost,
// margin or target exists only when the server sent it.

import { formatINR } from "./format";

export type GroupBy = "day" | "store" | "brand" | "category" | "salesperson" | "tender";

export const GROUPINGS: { key: GroupBy; label: string }[] = [
  { key: "day", label: "Day" },
  { key: "store", label: "Store" },
  { key: "brand", label: "Brand" },
  { key: "category", label: "Category" },
  { key: "salesperson", label: "Salesperson" },
  { key: "tender", label: "Tender" },
];

export interface SalesFilters {
  store: string;
  date_from: string;
  date_to: string;
  group_by: GroupBy;
}

/** India's date, YYYY-MM-DD, whatever the browser's own time zone. */
export function indiaDate(now: Date = new Date()): string {
  return now.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

/** This month so far: the 1st to today, India time. */
export function defaultFilters(now: Date = new Date()): SalesFilters {
  const today = indiaDate(now);
  return { store: "", date_from: `${today.slice(0, 8)}01`, date_to: today, group_by: "day" };
}

/** Only the filters that are set, as query parameters. */
export function filterParams(filters: SalesFilters): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) if (value) out[key] = value;
  return out;
}

export interface Row {
  key: string;
  label: string;
  bills: number;
  pieces: number | null;
  value_paise: number;
  gross_paise: number | null;
  disc_paise: number | null;
  asp_paise: number | null;
  abv_paise: number | null;
  upt: number | null;
  discount_pct: number | null;
  target_paise?: number | null;
  target_pct?: number | null;
  cost_paise?: number | null;
  margin_paise?: number | null;
  margin_pct?: number | null;
}

type Kind = "count" | "pieces" | "money" | "ratio" | "percent";

export interface ColumnDef {
  key: keyof Row;
  label: string;
  kind: Kind;
}

/** The table's columns for this answer: target and cost columns only when sent. */
export function columnsFor(body: { shows_cost: boolean; shows_target: boolean }): ColumnDef[] {
  const out: ColumnDef[] = [
    { key: "bills", label: "Bills", kind: "count" },
    { key: "pieces", label: "Pieces", kind: "pieces" },
    { key: "value_paise", label: "Value", kind: "money" },
    { key: "asp_paise", label: "Avg selling price", kind: "money" },
    { key: "abv_paise", label: "Avg bill value", kind: "money" },
    { key: "upt", label: "Units per bill", kind: "ratio" },
    { key: "discount_pct", label: "Discount %", kind: "percent" },
  ];
  if (body.shows_target) {
    out.push(
      { key: "target_paise", label: "Target", kind: "money" },
      { key: "target_pct", label: "Target achieved", kind: "percent" },
    );
  }
  if (body.shows_cost) {
    out.push(
      { key: "cost_paise", label: "Cost", kind: "money" },
      { key: "margin_paise", label: "Margin", kind: "money" },
      { key: "margin_pct", label: "Margin %", kind: "percent" },
    );
  }
  return out;
}

/** One cell in words. Absent or unknown is a dash, never a nought. */
export function cellText(value: unknown, kind: Kind): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  switch (kind) {
    case "money":
      return formatINR(n);
    case "percent":
      return `${n.toFixed(1)}%`;
    case "ratio":
      return n.toFixed(2);
    case "pieces":
      return Number.isInteger(n) ? String(n) : n.toFixed(2);
    default:
      return String(n);
  }
}

/** "as of 27 Sep 2026, 14:05", or the plain truth when there is no copy yet. */
export function asOfText(asOf: string | null | undefined): string {
  if (!asOf) return "No reporting copy yet";
  const when = new Date(asOf).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  return `As of ${when}`;
}
