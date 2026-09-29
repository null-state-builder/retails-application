import { useEffect, useRef, useState } from "react";

import { paiseToRupees, rupeesToPaise } from "../../../lib/format";

/**
 * An amount in rupees, held as integer paise.
 *
 * The typed text is state of its own so a half-written "12." survives the
 * keystroke that made it: parsing on every change and writing the parse back
 * would delete the decimal point as the cashier types it. Only text that is
 * actually an amount reaches the cart (`rupeesToPaise`, ADR-0004 - never
 * `Number(x) * 100`).
 *
 * An emptied box answers `null` rather than nought, and the caller says which it
 * means. For most boxes they are the same thing; for cash received they are not
 * - blank means "exactly the cash tender" and nought means "nothing was handed
 * over", and those are two different bills.
 *
 * **Text that is not an amount leaves no earlier amount in force.** A caller that
 * passes `onInvalid` is told the moment the box stops holding a number, keeps
 * the typed text on screen, and is expected to block on it (PRD §9.1: "invalid
 * input never silently keeps an earlier valid amount"). Without `onInvalid` the
 * box behaves as it always has - the half-written "12." is simply held back
 * until it is an amount - because for a box with no rule behind it there is
 * nothing to block.
 */
export function RupeeInput({
  testId,
  label,
  paise,
  locked,
  placeholder,
  prefillPaise = null,
  maxPaise,
  onInvalid,
  onChange,
}: {
  testId: string;
  label: string;
  paise: number;
  locked: boolean;
  placeholder: string;
  /**
   * What this box adopts if the cashier **taps** it while it is standing empty
   * - `null` when it is already filled, or when there is nothing left to offer
   * (#246: "tapping into an empty amount box pre-fills whatever is still owed").
   *
   * Whether the box is empty is the **caller's** judgement, not this box's text.
   * The cash row displays the balance it is about to absorb while nobody has yet
   * typed in it, and that row is the one the prefill exists for; a rule based on
   * the text being blank would skip exactly it.
   *
   * Adopting a figure is an ordinary edit - it goes out through `onChange` like
   * any keystroke and can be typed straight over, which is what makes a split a
   * split.
   */
  prefillPaise?: number | null;
  /** A hard local ceiling for an amount such as a policy-bound discount. */
  maxPaise?: number;
  /**
   * Told `true` while the box holds something that is not an amount this box may
   * take - a letter, a minus sign, or a figure past `maxPaise` - and `false` the
   * moment it holds one again (an empty box is not invalid; it is empty).
   *
   * A caller that passes this gets the ceiling enforced as a **refusal**: the
   * typed figure stays on screen, unchanged and unwritten, until a person fixes
   * it. A caller that does not gets the older behaviour, where the figure is
   * quietly clamped to the ceiling - which is right for a discount box whose
   * ceiling is head office's policy rather than a typing mistake.
   */
  onInvalid?: (invalid: boolean) => void;
  onChange: (paise: number | null) => void;
}) {
  const [text, setText] = useState(paise ? paiseToRupees(paise) : "");
  const shown = useRef(paise);
  const box = useRef<HTMLInputElement>(null);
  // Counted rather than flagged: a prefill that lands on the figure already
  // shown - the cash row adopting the balance it was displaying anyway - leaves
  // `text` identical, so an effect watching the text would not run on the one
  // row this matters most for.
  const [prefills, setPrefills] = useState(0);

  useEffect(() => {
    // Follow the cart when something other than this box moved the number - a
    // season swap changing the ticket price, or a new bill clearing it.
    if (paise === shown.current) return;
    shown.current = paise;
    setText(paise ? paiseToRupees(paise) : "");
  }, [paise]);

  useEffect(() => {
    // A box that just adopted the balance is selected whole, so the next
    // keystroke replaces it: the ticket's flow is "tap UPI, it offers ₹2,848,
    // overtype ₹2,000", and a cashier who has to clear the offer first would
    // rather it had never been made.
    if (prefills > 0) box.current?.select();
  }, [prefills]);

  return (
    <input
      ref={box}
      className="input bill-cell mono"
      inputMode="decimal"
      data-testid={testId}
      aria-label={label}
      disabled={locked}
      placeholder={placeholder}
      value={text}
      // `onClick`, deliberately not `onFocus`. Focus is not intent: a Tab
      // through the panel, or any focus the page restores on its own, would
      // otherwise put the whole balance into whichever box it landed on. On an
      // all-cash bill that means Card silently takes the lot, cash falls to
      // nought and is dropped from the tenders, the balance closes to zero and
      // nothing flags it - a card sale posted for money that went in the
      // drawer, found at the day close if at all. Grill Q1 settles it too:
      // "everything is a scan or a click", and this is the click.
      onClick={() => {
        if (locked || prefillPaise === null || prefillPaise <= 0) return;
        shown.current = prefillPaise;
        setText(paiseToRupees(prefillPaise));
        setPrefills((n) => n + 1);
        onInvalid?.(false);
        onChange(prefillPaise);
      }}
      onChange={(e) => {
        setText(e.target.value);
        if (e.target.value.trim() === "") {
          shown.current = 0;
          onInvalid?.(false);
          onChange(null);
          return;
        }
        // Text that is not an amount yet ("12.") is held on screen and kept off
        // the cart until it is one - and, where a caller asked to be told, it is
        // told, so nothing downstream goes on believing the figure before it.
        const parsed = rupeesToPaise(e.target.value);
        if (parsed === null) {
          onInvalid?.(true);
          return;
        }
        if (maxPaise !== undefined && parsed > maxPaise && onInvalid) {
          // The typed figure stays exactly as typed. Clamping here would put a
          // number on screen nobody asked for and let the bill close on it.
          shown.current = parsed;
          onInvalid(true);
          return;
        }
        // Keep the box itself honest, not just the cart it writes through. A
        // parent re-render normally reflects a capped value back here, but a
        // disconnected browser must never leave a larger typed amount on screen
        // while that render is delayed or interrupted.
        const accepted = maxPaise === undefined ? parsed : Math.min(parsed, maxPaise);
        shown.current = accepted;
        if (accepted !== parsed) setText(paiseToRupees(accepted));
        onInvalid?.(false);
        onChange(accepted);
      }}
    />
  );
}
