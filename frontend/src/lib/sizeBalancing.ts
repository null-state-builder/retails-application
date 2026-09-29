// What the Size Balancing page says (store operations ticket 34, ST-TRF-1). The
// server finds the transfers, applies the 8-week and 3-pieces-or-Rs-3,000 rules
// and decides who may approve (`outbound.size_balancing`); these only put that
// into words, so the page and its tests say the same thing.

import { formatINR } from "./format";

export interface SettingsT {
  weeks: number;
  min_pieces: number;
  min_mrp_paise: number;
}

export interface LineT {
  style_code: string;
  colour: string;
  size: string;
  qty: number;
  mrp_paise: number | null;
}

/** "SHIRT-1 Blue, M" - an item as the store knows it. */
export function itemText(line: Pick<LineT, "style_code" | "colour" | "size">): string {
  return `${[line.style_code, line.colour].filter(Boolean).join(" ")}, ${line.size}`;
}

/** The rule in one sentence, from the server's own settings. */
export function ruleText(s: SettingsT): string {
  return (
    `Suggested when another store holds a size you lack beyond ${s.weeks} weeks of its own sales, ` +
    `and only if the transfer moves at least ${s.min_pieces} pieces or ${formatINR(s.min_mrp_paise)} at MRP.`
  );
}

/** "3 pieces, ₹4,500 at MRP (1 piece has no MRP)". */
export function totalText(pieces: number, mrpPaise: number, unknownPieces: number): string {
  const parts = [`${pieces} ${pieces === 1 ? "piece" : "pieces"}`, `${formatINR(mrpPaise)} at MRP`];
  const text = parts.join(", ");
  if (!unknownPieces) return text;
  return `${text} (${unknownPieces} ${unknownPieces === 1 ? "piece has" : "pieces have"} no MRP)`;
}

export function mrpText(paise: number | null): string {
  return paise ? formatINR(paise) : "No MRP";
}

export function stateText(state: string, withdrawnReason: string): string {
  switch (state) {
    case "approved":
      return "Approved: transfer request raised";
    case "rejected":
      return "Rejected";
    case "withdrawn":
      switch (withdrawnReason) {
        case "replaced":
          return "Replaced by a newer suggestion";
        case "not_needed":
          return "No longer needed";
        case "not_checked":
          return "Switched off at one of the stores";
        default:
          return "Withdrawn";
      }
    default:
      return "Waiting for a decision";
  }
}

/** Where an approved suggestion's transfer request is listed. */
export const REQUESTS_PATH = "/goods/transfers/requests";
