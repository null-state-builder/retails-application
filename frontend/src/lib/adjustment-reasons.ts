/**
 * Why stock is off the books — one list, for every screen that asks (#78).
 *
 * The codes are the server's `AdjustmentReason`. They were spelled out
 * separately on the Stock Adjustments screen and again on Stock Count, and the
 * two disagreed: `shrinkage` read as "Shrinkage" on one and "Theft / shrinkage"
 * on the other, so the same posted reason looked like two different answers
 * depending on which page you opened. One list, so it cannot happen again.
 *
 * The wording is the store's, not the ledger's: somebody standing at a rail
 * with a scanner is answering "why is this missing?", and "shrinkage" on its own
 * is not a word they would use.
 */

export interface AdjustmentReasonOption {
  value: string;
  label: string;
}

export const ADJUSTMENT_REASONS: AdjustmentReasonOption[] = [
  { value: "shrinkage", label: "Theft / shrinkage" },
  { value: "miscount", label: "Miscount" },
  { value: "damage", label: "Damage" },
  { value: "surplus_found", label: "Found — more here than the books say" },
  { value: "other", label: "Something else" },
];

/** A stored reason code in the business's words; an unknown code reads as itself
 *  rather than as a blank, so a reason added on the server is never invisible. */
export function adjustmentReasonLabel(code: string): string {
  return ADJUSTMENT_REASONS.find((r) => r.value === code)?.label || code;
}
