import { Fragment, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { ScanBarcode, X } from "lucide-react";

import { formatINR, Money } from "../../../lib/format";
import { clampManualDiscount, qtyFrom } from "../../../till/cart";
import type { CartLine, PricedLine } from "../../../till/cart";
import { hasSlab, ratePercent } from "../../../till/tax";
import { newSplit, percentFrom, splitValue, whySplitIsWrong } from "../../../till/split";
import { usePositionedPopover } from "../../../shell/usePositionedPopover";
import { RupeeInput } from "./RupeeInput";

/** The counter's visible columns. Size folds into Item below 1440px, leaving a
 * readable item column instead of a sideways scrollbar at the 1366px reference
 * width. GST remains reachable through its badge and the footer breakup. */
const COLUMN_WIDTHS = [
  "24px",
  "auto",
  "52px",
  "88px",
  "76px",
  "82px",
  "88px",
  "104px",
  "28px",
] as const;

/** The columns staff read today (D10 §4), in the order they read them. */
export function Lines({
  lines,
  salespeople,
  locked,
  onEdit,
  onSalesperson,
  onPicked,
  onRemove,
  onUndo,
  canUndo,
  footer,
  splitAllowed = false,
}: {
  lines: PricedLine[];
  salespeople: { id: string; name: string }[];
  /** Ticket 08: whether this store's till may split a line between two people. */
  splitAllowed?: boolean;
  locked: boolean;
  onEdit: (key: string, patch: Partial<CartLine>) => void;
  onSalesperson: (key: string, salesperson: string | null) => void;
  onPicked: () => void;
  onRemove: (key: string) => void;
  onUndo: () => void;
  canUndo: boolean;
  footer: ReactNode;
}) {
  return (
    <section className="bill-grid-card" data-testid="bill-grid-card">
      <div className="bill-grid-scroll">
        {!lines.length ? (
          <div className="bill-empty" data-testid="bill-empty">
            <BarcodeEmpty />
            <strong>Ready to scan</strong>
            <span>Pull the scanner trigger. The cursor stays in the scan bar, always.</span>
          </div>
        ) : (
          <table className="bill-grid" data-testid="bill-lines">
            {/* Fixed widths, not content widths. All twelve columns D10 names have
            to be on the counter's screen at once - a Net column that scrolled
            off the right would be the one number the cashier reads aloud. */}
            <colgroup>
              {COLUMN_WIDTHS.map((width, i) => (
                <col
                  key={i}
                  className={i === 2 ? "bill-size-column" : undefined}
                  style={i === 1 ? undefined : { width }}
                />
              ))}
            </colgroup>
            <thead>
              <tr>
                <th>#</th>
                <th>Item</th>
                <th className="bill-size-column">Size</th>
                <th className="num">Qty</th>
                <th className="num">Rate</th>
                <th className="num">Discount</th>
                <th className="num">Net</th>
                <th>Salesperson</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <Fragment key={line.key}>
                  <tr className="bill-line" data-testid={`bill-line-${line.line_no}`}>
                    <td className="bill-line-number bill-col-no">{line.line_no}</td>
                    <td className="bill-col-item">
                      <ItemIdentityCell
                        line={line}
                        locked={locked}
                        onEdit={onEdit}
                        onPicked={onPicked}
                      />
                      {/* Operationally important badges visible in main row. */}
                      <div className="bill-item-meta">
                        {line.kind === "alteration" ? (
                          // Ticket 22: a service, so no brand, no tag and no offer.
                          <span
                            className="bill-tag"
                            data-testid={`bill-alteration-${line.line_no}`}
                          >
                            SAC {line.hsn} · GST {Number(line.gst_rate)}%
                          </span>
                        ) : (
                          <>
                            {!line.sold_before_inward && (
                              <span title={`${line.brand} · ${line.barcode}`}>{line.brand}</span>
                            )}
                            <span className="mono bill-barcode" title={line.barcode}>
                              {line.barcode}
                            </span>
                            {line.offer_credits.map((credit) => (
                              <span className="bill-offer" key={credit.offer_id}>
                                {credit.offer_name || "Offer"}
                              </span>
                            ))}
                            {line.sold_before_inward && <span className="bill-tag">Off tag</span>}
                            {line.no_discount && <span className="bill-tag">No discount</span>}
                          </>
                        )}
                      </div>
                      <span className="bill-size-fold mono">Size {line.size}</span>
                    </td>
                    <td className="bill-size-column bill-col-size mono">{line.size}</td>
                    <td className="num bill-col-qty" data-label="Qty">
                      <QtyCell line={line} locked={locked} onEdit={onEdit} onPicked={onPicked} />
                    </td>
                    <td className="num bill-col-rate" data-label="Rate">
                      <RateCell line={line} locked={locked} onEdit={onEdit} />
                    </td>
                    <td
                      className={
                        line.disc_paise > 0
                          ? "num bill-col-disc bill-discount-carrying"
                          : "num bill-col-disc"
                      }
                      data-label="Discount"
                    >
                      <DiscountCell line={line} locked={locked} onEdit={onEdit} />
                    </td>
                    <td className="num bill-col-net" data-label="Net">
                      <Money paise={line.net_paise} />
                      <span className="bill-net-tax">
                        incl GST <GstBadge line={line} />
                      </span>
                    </td>
                    <td className="bill-col-who" data-label="Salesperson">
                      <select
                        className={
                          line.salesperson === null
                            ? "select bill-cell bill-salesman-missing"
                            : "select bill-cell"
                        }
                        disabled={locked}
                        data-testid={`bill-salesman-${line.line_no}`}
                        aria-label={`Salesperson, line ${line.line_no}`}
                        value={line.salesperson ?? ""}
                        onChange={(e) => onSalesperson(line.key, e.target.value || null)}
                      >
                        <option value="">Nobody</option>
                        {salespeople.map((s) => (
                          <option key={s.id} value={s.id}>
                            {s.name}
                          </option>
                        ))}
                      </select>
                      {splitAllowed && !line.split && line.kind !== "alteration" && (
                        <button
                          type="button"
                          className="bill-split-open"
                          disabled={locked}
                          data-testid={`bill-split-${line.line_no}`}
                          aria-label={`Split line ${line.line_no} with a second salesperson`}
                          onClick={() => onEdit(line.key, { split: newSplit() })}
                        >
                          Split
                        </button>
                      )}
                    </td>
                    <td className="bill-col-del">
                      <button
                        type="button"
                        className="line-del"
                        disabled={locked}
                        data-testid={`bill-remove-${line.line_no}`}
                        aria-label={`Remove line ${line.line_no}`}
                        onClick={() => onRemove(line.key)}
                      >
                        <X size={14} />
                      </button>
                    </td>
                  </tr>
                  {line.split && (
                    <SplitRow
                      line={line}
                      salespeople={salespeople}
                      locked={locked}
                      onEdit={onEdit}
                    />
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <footer className="bill-grid-footer">
        <div className="bill-grid-totals">{footer}</div>
        <button
          type="button"
          className="btn bill-grid-undo"
          data-testid="bill-undo"
          title="Undo (no keyboard shortcut)"
          disabled={!canUndo || locked}
          onClick={onUndo}
        >
          Undo
        </button>
      </footer>
    </section>
  );
}

/**
 * Ticket 08: the line shared with a second salesperson, by whole percentages.
 *
 * The first person is the line's own salesperson, picked in the row above. The
 * second box fills itself with what is left of 100 when the first changes, and
 * either can be typed. Anything that does not stand (not 100%, 0 or 100, the
 * same person twice) is said here in words, and the bill will not close on it.
 */
function SplitRow({
  line,
  salespeople,
  locked,
  onEdit,
}: {
  line: PricedLine;
  salespeople: { id: string; name: string }[];
  locked: boolean;
  onEdit: (key: string, patch: Partial<CartLine>) => void;
}) {
  const split = line.split;
  if (!split) return null;
  const firstName = salespeople.find((s) => s.id === line.salesperson)?.name ?? "First";
  const problem = whySplitIsWrong(line.salesperson, split);
  const values = problem
    ? null
    : splitValue(line.net_paise, [split.first_percent, split.second_percent]);
  const shown = (percent: number) => (Number.isNaN(percent) ? "" : String(percent));
  return (
    <tr className="bill-split-row" data-testid={`bill-split-row-${line.line_no}`}>
      <td />
      <td colSpan={8}>
        <div className="bill-split">
          <span className="eyebrow">Split</span>
          <label>
            {firstName}
            <input
              className="input bill-cell bill-split-percent"
              inputMode="numeric"
              disabled={locked}
              data-testid={`bill-split-first-${line.line_no}`}
              aria-label={`First salesperson's percent, line ${line.line_no}`}
              value={shown(split.first_percent)}
              onChange={(e) => {
                const first = percentFrom(e.target.value);
                onEdit(line.key, {
                  split: {
                    ...split,
                    first_percent: first,
                    second_percent: Number.isNaN(first) ? split.second_percent : 100 - first,
                  },
                });
              }}
            />
            %
          </label>
          <label>
            with
            <select
              className="select bill-cell"
              disabled={locked}
              data-testid={`bill-split-with-${line.line_no}`}
              aria-label={`Second salesperson, line ${line.line_no}`}
              value={split.with ?? ""}
              onChange={(e) =>
                onEdit(line.key, { split: { ...split, with: e.target.value || null } })
              }
            >
              <option value="">Pick</option>
              {salespeople.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
            <input
              className="input bill-cell bill-split-percent"
              inputMode="numeric"
              disabled={locked}
              data-testid={`bill-split-second-${line.line_no}`}
              aria-label={`Second salesperson's percent, line ${line.line_no}`}
              value={shown(split.second_percent)}
              onChange={(e) =>
                onEdit(line.key, {
                  split: { ...split, second_percent: percentFrom(e.target.value) },
                })
              }
            />
            %
          </label>
          {values && (
            <span className="bill-split-values" data-testid={`bill-split-values-${line.line_no}`}>
              <Money paise={values[0]} /> + <Money paise={values[1]} />
            </span>
          )}
          {problem && (
            <span className="warn-note" data-testid={`bill-split-problem-${line.line_no}`}>
              {problem}
            </span>
          )}
          <button
            type="button"
            className="btn bill-split-remove"
            disabled={locked}
            data-testid={`bill-split-remove-${line.line_no}`}
            onClick={() => onEdit(line.key, { split: null })}
          >
            Remove split
          </button>
        </div>
      </td>
    </tr>
  );
}

function BarcodeEmpty() {
  return (
    <span className="bill-empty-barcode" aria-hidden="true">
      <ScanBarcode size={26} />
    </span>
  );
}

/**
 * Which slab this piece was taxed at, and nothing else (#247, grill Q7).
 *
 * "Enough to catch a wrong slab at a glance" was the ruling, and the rate is the
 * whole of that: a ₹4,999 jacket showing 5% is the mistake worth seeing from
 * across the counter, and the rupee figure beside it never helped anybody see
 * it. The bill's own tax total, and where it came from, is one click away in the
 * footer.
 *
 * A line nothing has priced yet is blank rather than "0%": that piece is not
 * zero-rated, it is unpriced, and the box asking for its price is two columns
 * away saying so.
 */
function GstBadge({ line }: { line: PricedLine }) {
  // `hasSlab`, the same test the breakup panel filters on - two spellings of
  // "this line has no slab" can disagree, and a badge saying 0% beside a panel
  // that left the line out is the counter telling two stories.
  if (!hasSlab(line.gst_rate)) return <span className="muted-cell">-</span>;
  return (
    <span
      className="bill-gst-badge"
      data-testid={`bill-gst-${line.line_no}`}
      // The rupees are still one hover away for anybody who wants them - the
      // ruling took them off the screen, not out of the world.
      title={`GST on this line: ${formatINR(line.gst_paise)}`}
    >
      {ratePercent(line.gst_rate)}
    </span>
  );
}

/** What every editable cell in the line grid needs, and nothing more. */
interface CellProps {
  line: PricedLine;
  locked: boolean;
  onEdit: (key: string, patch: Partial<CartLine>) => void;
}

/**
 * The item identity cell: item name (or manual-desc input for off-tag lines)
 * wrapped in a keyboard-accessible disclosure button that opens an anchored
 * popover with secondary metadata (design, colour, season, HSN).
 *
 * Secondary metadata is hidden by default to keep each row compact. The
 * season selector moves here while preserving the existing onEdit repricing
 * path. Operationally critical badges (offer, off-tag, no-discount) stay in
 * the main row's bill-item-meta strip (rendered by the caller).
 */
function ItemIdentityCell({
  line,
  locked,
  onEdit,
  onPicked,
}: CellProps & { onPicked: () => void }) {
  const [open, setOpen] = useState(false);
  const openKey = open ? `item-detail-${line.key}` : null;
  const close = () => setOpen(false);
  const popoverId = `bill-item-detail-${line.key}`;
  // fallbackSize = 300 (popover max-width in CSS), side = "below"
  const { at, triggerRef, popoverRef } = usePositionedPopover<HTMLButtonElement>(
    openKey,
    close,
    300,
    "below",
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="bill-item-disclosure-btn"
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        aria-label={
          open
            ? `Close details for line ${line.line_no}`
            : `Show details for line ${line.line_no}: ${line.item || line.design}`
        }
        onClick={() => setOpen((v) => !v)}
      >
        <ItemCell line={line} locked={locked} onEdit={onEdit} />
      </button>

      {open &&
        at &&
        createPortal(
          <div
            ref={popoverRef}
            id={popoverId}
            role="dialog"
            aria-label={`Details for line ${line.line_no}`}
            className="bill-item-popover"
            style={{ top: at.top, bottom: at.bottom, left: at.left, maxHeight: at.maxHeight }}
          >
            <p className="eyebrow">Item details &middot; line {line.line_no}</p>

            {line.design && (
              <div className="bill-item-popover-row">
                <label>Design</label>
                <span title={line.design}>{line.design}</span>
              </div>
            )}
            {line.color && (
              <div className="bill-item-popover-row">
                <label>Colour</label>
                <span title={line.color}>{line.color}</span>
              </div>
            )}
            {/* Season: selector if ambiguous, plain text otherwise. */}
            {!line.sold_before_inward && line.kind !== "alteration" && (
              <div className="bill-item-popover-row">
                <label>Season</label>
                {line.alternatives.length >= 2 ? (
                  <select
                    className="select bill-cell bill-season"
                    disabled={locked}
                    data-testid={`bill-season-${line.line_no}`}
                    aria-label={`Season, line ${line.line_no}`}
                    value={line.season}
                    onChange={(e) => {
                      const picked = line.alternatives.find((a) => a.season === e.target.value);
                      if (!picked) return;
                      onEdit(line.key, {
                        season: picked.season,
                        mrp_paise: picked.mrp_paise ?? 0,
                        needs_price: picked.mrp_paise == null,
                      });
                      onPicked();
                    }}
                  >
                    {line.alternatives.map((a) => (
                      <option key={a.season} value={a.season}>
                        {a.season}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span>{line.season}</span>
                )}
              </div>
            )}
            {line.hsn && (
              <div className="bill-item-popover-row">
                <label>HSN</label>
                <span>{line.hsn}</span>
              </div>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}

/**
 * What the piece is: the books' word for it, or the cashier's (#186).
 *
 * A sold-before-inward line has no item name because nothing has ever recorded
 * one, so the cell becomes the box where somebody writes what left the shop. It
 * is not optional and the bill will not close without it (`whyItCannotClose`):
 * the server refuses a barcode it cannot place with no words beside it, and it
 * refuses it *after* the receipt has printed, halting the queue behind it.
 *
 * The description is also the only thing this line will ever say about itself
 * until the PT lands - it is what prints on the customer's copy and what the
 * store reads when it goes looking for the piece.
 */
function ItemCell({ line, locked, onEdit }: CellProps) {
  if (!line.sold_before_inward) {
    return <strong className="bill-item-name">{line.item || line.design}</strong>;
  }
  return (
    <input
      className="input bill-cell"
      data-testid={`bill-desc-${line.line_no}`}
      aria-label={`What this piece is, line ${line.line_no}`}
      autoComplete="off"
      placeholder="What is it?"
      // The box is still narrower than a sentence, and this line's description is
      // the only record of what left the shop - so it is readable on hover as
      // well as on the receipt.
      title={line.manual_desc}
      disabled={locked}
      value={line.manual_desc}
      onChange={(e) => onEdit(line.key, { manual_desc: e.target.value })}
    />
  );
}

/** The ticket price. Editable only because a piece can reach a shelf with no
 *  MRP recorded (contract, step 3) - and then a human reads it off the tag.
 *
 *  The test is `needs_price`, never the current amount. Keying off the amount
 *  would unmount the box on the first digit typed - "1" is 100 paise, which is
 *  a price - leaving a ₹1,499 garment stuck at ₹1 with no way back, and a bill
 *  so internally consistent that the server would take it. */
function RateCell({ line, locked, onEdit }: CellProps) {
  if (!line.needs_price) return <Money paise={line.mrp_paise} />;
  return (
    <RupeeInput
      testId={`bill-rate-${line.line_no}`}
      label={`Price from the tag, line ${line.line_no}`}
      paise={line.mrp_paise}
      locked={locked}
      placeholder="Price"
      onChange={(paise) => onEdit(line.key, { mrp_paise: paise ?? 0 })}
    />
  );
}

/**
 * A manual discount, the cap it lives under (B2), and what the rulebook gave.
 *
 * The two discounts share a cell because they share a column in the customer's
 * head - "what came off this shirt" - but they never share a number. The box is
 * the cashier's and the cap measures it; the chip below is head office's and is
 * not editable here at all, because the way to change an offer is to change the
 * offer.
 */
function DiscountCell({ line, locked, onEdit }: CellProps) {
  const lockedOnOffer = !line.manual_discount_allowed;
  const allowedPaise = lockedOnOffer ? 0 : line.cap_paise;
  // A held/restored bill or a freshly-triggered threshold offer can make an
  // amount that was valid when typed invalid now. Keep that existing value
  // editable until the cashier removes it; otherwise the close guard blocks the
  // bill while the disabled field leaves no way to fix it.
  const needsCorrection = line.disc_paise > allowedPaise;
  return (
    <>
      <span
        title={
          lockedOnOffer
            ? "This line already has an offer; Head Office has turned manual stacking off."
            : `Head Office allows up to ${line.cap_percent}% manual discount on this line.`
        }
      >
        <RupeeInput
          testId={`bill-disc-${line.line_no}`}
          label={`Discount, line ${line.line_no}`}
          paise={line.disc_paise}
          locked={locked || (!needsCorrection && allowedPaise === 0)}
          placeholder="0"
          maxPaise={allowedPaise}
          onChange={(paise) =>
            onEdit(line.key, {
              disc_paise: clampManualDiscount(paise ?? 0, allowedPaise),
            })
          }
        />
      </span>
      {needsCorrection && (
        <span className="warn-note" data-testid={`bill-disc-correction-${line.line_no}`}>
          {lockedOnOffer
            ? "Remove this manual discount: Head Office has turned offer stacking off."
            : `Reduce this discount to ${formatINR(allowedPaise)}: Head Office's limit is ${line.cap_percent}%.`}
        </span>
      )}
      {/* Information, not a gate - and deliberately so. The rulebook (#183) does
          obey this flag: no offer, of any layer, reaches a no-discount piece.
          But nothing in the corpus says a *cashier's* keyed-in discount is
          barred on such a style, and enforcing one here would be inventing brand
          policy - so the cap governs this line like any other and the cashier is
          told what they are discounting. Whether the flag should also bind a
          manual discount is Anand's to rule on. */}
      {line.no_discount && <span className="muted-cell">no-discount style</span>}
    </>
  );
}

/**
 * How many pieces of this line, as whole pieces.
 *
 * A plain controlled `type="number"` is wrong here, and the way it is wrong is
 * expensive. Typing "1.5" into one goes: "1" is 1; "." makes the value
 * momentarily invalid, so the browser reports it as empty and the controlled
 * value snaps back to "1", eating the dot; then "5" lands beside the 1 and the
 * cashier has silently billed **fifteen** pieces. So the text is held here, the
 * way `RupeeInput` holds a half-written amount, and only `qtyFrom` decides what
 * the cart gets - which truncates, so "1.5" is one piece and never 15 and never
 * a fraction on the write path.
 */
function QtyCell({
  line,
  locked: lockedBill,
  onEdit,
  onPicked,
}: CellProps & { onPicked: () => void }) {
  // Ticket 22: an alteration charge is always one.
  const locked = lockedBill || line.kind === "alteration";
  const [text, setText] = useState(String(line.qty));
  const shown = useRef(line.qty);

  useEffect(() => {
    if (line.qty === shown.current) return;
    shown.current = line.qty;
    setText(String(line.qty));
  }, [line.qty]);

  const step = (delta: number) => {
    const qty = Math.max(1, line.qty + delta);
    shown.current = qty;
    setText(String(qty));
    onEdit(line.key, { qty });
    onPicked();
  };

  return (
    <span className="bill-qty-stepper">
      <button
        type="button"
        disabled={locked || line.qty <= 1}
        onClick={() => step(-1)}
        aria-label={`Remove one, line ${line.line_no}`}
      >
        −
      </button>
      <input
        className="input bill-cell"
        inputMode="numeric"
        disabled={locked}
        data-testid={`bill-qty-${line.line_no}`}
        aria-label={`Quantity, line ${line.line_no}`}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          const qty = qtyFrom(e.target.value);
          shown.current = qty;
          onEdit(line.key, { qty });
        }}
        // Whatever half-written thing is in the box, the count it actually billed
        // is what the cashier should be looking at once they leave it.
        onBlur={() => setText(String(line.qty))}
      />
      <button
        type="button"
        disabled={locked}
        onClick={() => step(1)}
        aria-label={`Add one, line ${line.line_no}`}
      >
        +
      </button>
    </span>
  );
}
