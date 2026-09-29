// Return on each offer (store operations ticket 31, ST-OFR-1): what an offer
// earned and cost once it ran, against a baseline period the viewer states.
// The server does the work and decides what each viewer is sent; this file
// only turns its answer into table rows.

import type { ApiRead, ApiSchemas } from "./api";
import { formatINR } from "./format";

export type OfferReturn = ApiRead<ApiSchemas["OfferReturn"]>;
export type ReturnPeriod = OfferReturn["periods"][number];
type Change = OfferReturn["change"];

type Kind = "count" | "money" | "percent" | "rate";

export interface Figure {
  key: keyof ReturnPeriod & string;
  label: string;
  kind: Kind;
  /** Sent only to a viewer who may see cost; never drawn otherwise. */
  cost?: boolean;
  /** How the change against the baseline is written, where there is one. */
  change?: "percent" | "points";
}

export const FIGURES: Figure[] = [
  { key: "bills", label: "Bills", kind: "count" },
  { key: "pieces", label: "Pieces sold", kind: "count" },
  { key: "sales_paise", label: "Sales (what customers paid, with GST)", kind: "money" },
  { key: "discount_paise", label: "Discount given", kind: "money" },
  { key: "discount_pct", label: "Discount, % of MRP", kind: "percent", change: "points" },
  { key: "offer_discount_paise", label: "Of which this offer gave", kind: "money" },
  { key: "offer_pieces", label: "Pieces this offer discounted", kind: "count" },
  { key: "brand_funded_paise", label: "Brand-funded part", kind: "money", cost: true },
  { key: "margin_paise", label: "Gross margin", kind: "money", cost: true },
  { key: "margin_pct", label: "Gross margin, %", kind: "percent", cost: true, change: "points" },
  { key: "sales_per_day_paise", label: "Sales per day", kind: "money", change: "percent" },
  { key: "pieces_per_day", label: "Pieces per day", kind: "rate", change: "percent" },
  {
    key: "margin_per_day_paise",
    label: "Gross margin per day",
    kind: "money",
    cost: true,
    change: "percent",
  },
];

/** The rows this answer draws: cost rows only when the server sent cost. */
export function figuresFor(data: Pick<OfferReturn, "shows_cost">): Figure[] {
  return FIGURES.filter((figure) => !figure.cost || data.shows_cost);
}

export function cellText(period: ReturnPeriod, figure: Figure): string {
  const value = period[figure.key] as number | null | undefined;
  if (value === null || value === undefined) return "—";
  if (figure.kind === "money") return formatINR(value);
  if (figure.kind === "percent") return `${value}%`;
  if (figure.kind === "rate") return value.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  return value.toLocaleString("en-IN");
}

/** The change against the baseline; blank for a row that has none. */
export function changeText(change: Change, figure: Figure): string {
  if (!figure.change) return "";
  const value = (change as Record<string, number | null | undefined>)[figure.key];
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return figure.change === "points" ? `${sign}${value} pts` : `${sign}${value}%`;
}

export function periodDates(period: Pick<ReturnPeriod, "date_from" | "date_to">): string {
  const day = (iso: string) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", {
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  return `${day(period.date_from)} – ${day(period.date_to)}`;
}

export interface Baseline {
  from: string;
  to: string;
}

/** The query that asks for a baseline; none (the server's default) until both days are set. */
export function baselineQuery(baseline: Baseline | null): Record<string, string> {
  if (!baseline?.from || !baseline.to) return {};
  return { baseline_from: baseline.from, baseline_to: baseline.to };
}
