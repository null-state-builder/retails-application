import { useState } from "react";
import { createPortal } from "react-dom";

import { Money } from "../../../lib/format";
import type { PricedBill } from "../../../till/cart";
import { TAX_KIND_WORDS } from "../../../till/gstin";
import type { B2bTaxKind } from "../../../till/gstin";
import { ratePercent, taxBreakup, taxLabel } from "../../../till/tax";
import { usePositionedPopover } from "../../../shell/usePositionedPopover";

/** The panel's CSS max height, for the hook's first clamp - see
 *  `usePositionedPopover`'s `fallbackSize`. */
const PANEL_HEIGHT = 320;

/**
 * The bill's tax figure, and the whole of it one click away (#247, grill Q7).
 *
 * This is the other half of taking GST off the grid. The rate badge on a line
 * catches a wrong slab; this answers the question a customer actually asks -
 * "what is the tax on this?" - and it has to answer it the way the paper they
 * are about to be handed does, or the counter has two stories.
 *
 * So the heads come from the same `taxKind` the receipt prints from, and a
 * retail bill shows none: the customer's copy carries a single "Tax included"
 * line, because a bill with no GSTIN on it is not a tax invoice and nobody is
 * claiming credit against it. Inventing a CGST/SGST split here that the paper
 * does not carry would be this screen telling a cashier something the customer
 * cannot see.
 *
 * Floating, and portaled out (Q11: nothing pushes the layout) - the footer is
 * pinned and every pixel of it is somebody's number.
 */
export function TaxFigure({
  bill,
  kind,
}: {
  bill: PricedBill;
  /** Exactly what `toDraft`/`receipt.ts` will use, including the counter that
   *  does not know its own state and therefore raises no tax invoice at all. */
  kind: B2bTaxKind;
}) {
  const [open, setOpen] = useState(false);
  const breakup = taxBreakup(bill, kind);
  const label = taxLabel(bill.gst_paise);
  // The figures in this band are conditional - "You saved", "Given back" and
  // "Rounding" appear and disappear as the bill changes - so the trigger slides
  // sideways while the panel is open and a `position: fixed` panel would stay
  // pointing at where it used to be. The hook re-places whenever `openKey`
  // changes, so the key carries the two figures that move it.
  const panel = usePositionedPopover(
    open ? `tax:${bill.gst_paise}:${bill.net_paise}:${bill.saved_paise}` : null,
    () => setOpen(false),
    PANEL_HEIGHT,
  );

  return (
    <div className="bill-figure">
      <span className="bill-figure-label">{label}</span>
      <button
        type="button"
        className="bill-tax-open"
        ref={panel.triggerRef}
        data-testid="bill-tax"
        aria-expanded={open}
        // The figure has always been the number a cashier reads out; making it a
        // button must not make it a *different* number, so the accessible name
        // carries the invitation rather than the visible text.
        aria-label={`${label} - see the breakup`}
        onClick={() => setOpen((was) => !was)}
      >
        {/* A magnitude, with the direction in the words above it - the
            receipt's own presentation (`receipt.ts`: "Tax included ₹-655.78"
            is arithmetically right and not a sentence anybody puts on a
            customer's copy). */}
        <Money paise={breakup.shown_paise} />
      </button>

      {open &&
        panel.at &&
        createPortal(
          <div
            ref={panel.popoverRef}
            className="card bill-tax-panel"
            data-testid="bill-tax-panel"
            style={{ top: panel.at.top, left: panel.at.left }}
          >
            <p className="eyebrow">{label}</p>
            {breakup.rows.length === 0 ? (
              <p className="muted-cell" data-testid="bill-tax-empty">
                Nothing on this bill carries tax yet.
              </p>
            ) : (
              <table className="data bill-tax-rows">
                <thead>
                  <tr>
                    <th>Rate</th>
                    <th className="num">Taxable</th>
                    <th className="num">Tax</th>
                  </tr>
                </thead>
                <tbody>
                  {breakup.rows.map((row) => (
                    <tr key={row.rate} data-testid={`bill-tax-rate-${row.rate}`}>
                      <td>{ratePercent(row.rate)}</td>
                      <td className="num">
                        <Money paise={row.taxable_paise} />
                      </td>
                      <td className="num">
                        <Money paise={row.gst_paise} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {breakup.split.length > 0 && (
              <div className="bill-tax-split" data-testid="bill-tax-split">
                {breakup.split.map((head) => (
                  <div className="bill-row" key={head.label}>
                    {/* Word for word what `receipt.ts` puts on the paper - "CGST
                        given back" and not a minus sign. */}
                    <span>
                      {head.label} {breakup.given_back ? "given back" : "included"}
                    </span>
                    <Money paise={head.paise} />
                  </div>
                ))}
                <p className="muted-cell">
                  {TAX_KIND_WORDS[kind]} - as it prints on the customer&rsquo;s copy.
                </p>
              </div>
            )}
            {breakup.split.length === 0 && breakup.rows.length > 0 && (
              <p className="muted-cell" data-testid="bill-tax-retail">
                A retail bill: the customer&rsquo;s copy shows one tax line, not a split. Enter a
                GSTIN to raise a tax invoice.
              </p>
            )}
          </div>,
          document.body,
        )}
    </div>
  );
}
