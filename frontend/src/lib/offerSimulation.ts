// Offer simulation (store operations ticket 30, ST-OFR-3): what a draft offer
// would have cost on real past bills. The server does the work and decides
// what each viewer is sent; this file only turns its answer into table rows.

import type { ApiRead, ApiSchemas } from "./api";
import { formatINR } from "./format";

export type Simulation = ApiRead<ApiSchemas["OfferSimulation"]>;
export type SimulationPeriod = Simulation["periods"][number];

type Kind = "count" | "money" | "percent";

export interface Figure {
  key: keyof SimulationPeriod;
  label: string;
  kind: Kind;
  /** Sent only to a viewer who may see cost; never drawn otherwise. */
  cost?: boolean;
}

export const FIGURES: Figure[] = [
  { key: "bills", label: "Bills looked at", kind: "count" },
  { key: "bills_affected", label: "Bills the offer would touch", kind: "count" },
  { key: "pieces", label: "Pieces affected", kind: "count" },
  { key: "mrp_paise", label: "MRP of those pieces", kind: "money" },
  { key: "discount_paise", label: "Estimated discount", kind: "money" },
  { key: "discount_pct", label: "Estimated discount, % of MRP", kind: "percent" },
  { key: "actual_disc_paise", label: "Discount actually given on them", kind: "money" },
  { key: "gifts", label: "Gifts earned", kind: "count" },
  { key: "margin_paise", label: "Margin with the offer (estimate)", kind: "money", cost: true },
  { key: "margin_pct", label: "Margin with the offer, %", kind: "percent", cost: true },
  { key: "actual_margin_paise", label: "Margin those pieces made", kind: "money", cost: true },
  { key: "margin_effect_paise", label: "Margin effect", kind: "money", cost: true },
];

/** The rows this answer draws: cost rows only when sent, gifts only when earned. */
export function figuresFor(data: Pick<Simulation, "shows_cost" | "periods">): Figure[] {
  const gifts = data.periods.some((period) => Number(period.gifts) > 0);
  return FIGURES.filter((figure) => {
    if (figure.cost && !data.shows_cost) return false;
    if (figure.key === "gifts" && !gifts) return false;
    return true;
  });
}

export function cellText(period: SimulationPeriod, figure: Figure): string {
  const value = period[figure.key] as number | null | undefined;
  if (value === null || value === undefined) return "—";
  if (figure.kind === "money") return formatINR(value);
  if (figure.kind === "percent") return `${value}%`;
  return value.toLocaleString("en-IN");
}

export function periodDates(period: SimulationPeriod): string {
  const day = (iso: string) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", {
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  return `${day(period.date_from)} – ${day(period.date_to)}`;
}
