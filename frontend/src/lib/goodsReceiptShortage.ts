import type { GrnCoverage } from "./goodsReceiving";

/** The server pairs claims to physical counts. Shortage decisions cite the
 * claim key, never a counted lot. Accepted decisions are already accounted in
 * remaining_shortage_qty; only a pending request reserves its remaining amount
 * in this form. The original invoice/count comparison stays unchanged. */
export function receiptShortages(coverage: GrnCoverage) {
  const claims = new Map((coverage.invoice?.lines ?? []).map((line) => [line.line_key, line]));
  return coverage.invoice_comparison.flatMap((comparison) => {
    const claim = claims.get(comparison.claim_line_key);
    if (!claim || comparison.difference >= 0) return [];
    const accounted = coverage.dispositions
      .filter(
        (row) =>
          row.kind === "accept_shortage" &&
          row.source_line_key === claim.line_key &&
          row.state === "pending",
      )
      .reduce((sum, row) => sum + row.qty, 0);
    const remaining = Math.max(0, comparison.remaining_shortage_qty - accounted);
    return remaining > 0 ? [{ claim, comparison, remaining }] : [];
  });
}
