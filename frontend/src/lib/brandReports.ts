// Brands > Reports (store operations ticket 29, ST-BRD-2): the plain words and
// the layout editor's moves. Pure helpers, so they are tested without a screen.
// What a report holds, and who may have it, is the server's decision; these only
// name files and move columns about.

import { indiaDate } from "./salesReport";

export type ReportKind = "sale" | "soh";

export const KIND_WORDS: Record<ReportKind, string> = { sale: "Sale", soh: "SOH" };

export interface LayoutColumn {
  field: string;
  header: string;
  format: string;
  total: boolean;
  value?: string;
}

export interface LayoutTitleLine {
  text: string;
  size: number;
}

export interface LayoutBody {
  file_type: "xlsx" | "csv";
  sheet_name: string;
  font: string;
  header_size: number;
  header_border: "grid" | "box";
  title_lines: LayoutTitleLine[];
  columns: LayoutColumn[];
}

export interface LayoutField {
  key: string;
  label: string;
  kind: string;
  formats: string[];
  can_total: boolean;
}

/** Last month, YYYY-MM, India time: the month a brand's reports are usually for. */
export function lastMonth(now: Date = new Date()): string {
  const [year, month] = indiaDate(now).split("-").map(Number);
  return month === 1 ? `${year - 1}-12` : `${year}-${String(month - 1).padStart(2, "0")}`;
}

/** The name the server gives a report file (`reporting.brand_reports.build`). */
export function reportFileName(
  brandCode: string,
  kind: ReportKind,
  storeCode: string,
  month: string,
  isCsv: boolean,
): string {
  return `${brandCode.toUpperCase()}-${KIND_WORDS[kind]}-${storeCode}-${month}.${isCsv ? "csv" : "xlsx"}`;
}

/** A new column for `field`: its own words as the header, its first format. */
export function newColumn(field: LayoutField): LayoutColumn {
  const column: LayoutColumn = {
    field: field.key,
    header: field.key === "fixed" ? "Text" : field.label,
    format: field.formats[0] ?? "text",
    total: false,
  };
  if (field.key === "fixed") column.value = "";
  return column;
}

/** The column `field` now shows, keeping its header; its format and total made to fit. */
export function withField(column: LayoutColumn, field: LayoutField): LayoutColumn {
  const next: LayoutColumn = {
    field: field.key,
    header: column.header,
    format: field.formats.includes(column.format) ? column.format : (field.formats[0] ?? "text"),
    total: field.can_total ? column.total : false,
  };
  if (field.key === "fixed") next.value = column.value ?? "";
  return next;
}

/** `list` with the item at `index` moved `by` places (-1 up, +1 down); unchanged at an end. */
export function moved<T>(list: T[], index: number, by: -1 | 1): T[] {
  const to = index + by;
  if (to < 0 || to >= list.length) return list;
  const out = [...list];
  [out[index], out[to]] = [out[to], out[index]];
  return out;
}

/** `list` without the item at `index`. */
export function without<T>(list: T[], index: number): T[] {
  return list.filter((_, i) => i !== index);
}

/** Has the draft changed from what was saved? */
export function changed(draft: LayoutBody, saved: LayoutBody): boolean {
  return JSON.stringify(draft) !== JSON.stringify(saved);
}

/** "12,345" in Indian grouping. */
export function count(n: number): string {
  return n.toLocaleString("en-IN");
}
