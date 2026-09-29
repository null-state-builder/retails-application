// Keeping the cursor where the scanner types (#181, D10 §4).
//
// A keyboard-wedge scanner has no idea what a web page is. It types the barcode
// into whatever happens to be focused and sends Enter, so "the scan box is
// always focused" is not a nicety - it is the difference between a scan becoming
// a line and a scan becoming stray characters in the customer's mobile number.
//
// Two departures from `ScanScreen`, which does the same job for the counting
// surface and is deliberately not reused here (it is a full-screen count UI, the
// wrong shape for a bill):
//
//   · **The box is visible and controlled.** D10 has the scan box doubling as
//     type-to-search, which needs a value to suggest from; ScanScreen's sink is
//     invisible and uncontrolled because a counting screen has nothing to
//     suggest. A wedge fires about thirteen characters in forty milliseconds,
//     which is thirteen state updates React coalesces without noticing.
//   · **What "owns the focus" is asked, not listed.** ScanScreen allows two
//     class names through by name, which is fine for a screen with two inputs
//     and wrong for a bill: a cashier typing a discount, a quantity, a mobile
//     number or an amount of cash must be allowed to finish. So anything a
//     person can type into keeps the focus, and everything else - a button they
//     just pressed, a stray click on the page - hands it back. That is also how
//     "the cursor returns to the scan box after every action" is implemented:
//     the poll does it, so no button has to remember to.

import { useCallback, useEffect, useRef } from "react";
import type { RefObject } from "react";

/** How often to check where the cursor is. Fast enough that a stray click costs
 *  no scan, slow enough to be invisible; ScanScreen's number, and it has held. */
const PATROL_MS = 400;

export interface ScanBox {
  /** Put this on the visible scan input. */
  ref: RefObject<HTMLInputElement>;
  /** Send the cursor back now, rather than waiting for the next patrol. */
  focus: () => void;
}

/**
 * Hold the cursor in one input.
 *
 * `enabled` is false while the counter has no business scanning - no local
 * price list yet, say - so the patrol does not fight a person trying to read the
 * message that says why.
 *
 * Anything inside `[data-wedge-ignore]` keeps the focus too, for a popover or a
 * dialogue that owns the keyboard while it is open.
 */
export function useScanBox(enabled = true): ScanBox {
  const ref = useRef<HTMLInputElement>(null);

  const focus = useCallback(() => ref.current?.focus(), []);

  useEffect(() => {
    if (!enabled) return;
    focus();
    const patrol = window.setInterval(() => {
      const active = document.activeElement;
      if (active === ref.current || ownsTheKeyboard(active)) return;
      focus();
    }, PATROL_MS);
    return () => window.clearInterval(patrol);
  }, [enabled, focus]);

  return { ref, focus };
}

/** As much of an element as this decision needs.
 *
 *  A structural shape rather than `Element`, so the rule can be tested as the
 *  plain function it is: the suite runs in node, and a real element would drag
 *  a DOM implementation in behind it. `Element` satisfies this as it stands. */
export interface FocusTarget {
  tagName: string;
  isContentEditable?: boolean;
  closest(selector: string): unknown;
}

/** Is this element one a person is legitimately typing into? */
export function ownsTheKeyboard(active: FocusTarget | null): boolean {
  if (!active) return false;
  if (active.closest("[data-wedge-ignore]")) return true;
  // Only an `HTMLElement` carries this; on anything else it is undefined, which
  // is the right answer anyway.
  if (active.isContentEditable) return true;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName);
}
