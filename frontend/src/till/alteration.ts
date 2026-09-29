/**
 * Ticket 22 (ST-ORD-3): a paid alteration as its own bill line.
 *
 * The line is a service, not a piece: no stock, no offer, no manual discount,
 * no split, quantity one, taxed at the fixed rate head office sends for its
 * service code (5% under SAC 9988, baseline - CA to confirm) whatever the
 * charge. A free alteration has no line at all. The job card that says what the
 * tailor does is made on the Alterations screen, from the bill, online.
 *
 * The code, rate and wording come from the dataset (`alteration_charge`); the
 * counter holds no copy of its own, so the server and the till can never price
 * the line two ways.
 */

import { newKey } from "./cart";
import type { CartLine } from "./cart";
import type { TillAlterationCharge } from "./types";

/** What the line carries in place of a barcode (the server writes the same). */
export const ALTERATION_CODE = "ALTERATION";

/** The dataset's answer as the counter trusts it: a whole definition, or off. */
export function alterationChargeFrom(raw: unknown): TillAlterationCharge | null {
  if (!raw || typeof raw !== "object") return null;
  const { sac, gst_rate, description } = raw as Record<string, unknown>;
  if (typeof sac !== "string" || typeof gst_rate !== "string" || typeof description !== "string") {
    return null;
  }
  return { sac, gst_rate, description };
}

/** Is this cart line a paid alteration's own line? */
export function isAlteration(line: Pick<CartLine, "kind">): boolean {
  return line.kind === "alteration";
}

/** The charge typed in rupees, as whole paise - or null when it is not a charge. */
export function chargePaiseFrom(typed: string): number | null {
  const text = typed.trim();
  if (!/^\d+(\.\d{1,2})?$/.test(text)) return null;
  const [rupees, paise = ""] = text.split(".");
  const value = Number(rupees) * 100 + Number(paise.padEnd(2, "0"));
  return value > 0 ? value : null;
}

/** A paid alteration's own line, ready for the bill. */
export function addAlterationCharge(
  charge: TillAlterationCharge,
  chargePaise: number,
  salesperson: string | null,
  key = newKey(),
): CartLine {
  return {
    key,
    kind: "alteration",
    fixed_rate: charge.gst_rate,
    barcode: ALTERATION_CODE,
    season: "",
    design: "",
    brand: "",
    item: "Alteration",
    size: "",
    color: "",
    hsn: charge.sac,
    // No offer and no manual discount ever reach it (the server refuses both).
    no_discount: true,
    mrp_paise: chargePaise,
    needs_price: false,
    qty: 1,
    disc_paise: 0,
    salesperson,
    alternatives: [],
    stock: 0,
    manual_desc: charge.description,
    sold_before_inward: false,
  };
}
