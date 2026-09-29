import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

/**
 * A popover that hangs off a control inside the sidebar card: portaled out of
 * it, and placed by measuring its trigger.
 *
 * Two of them exist - the rail's section flyout and the profile panel - and
 * both need the same thing for the same reason. The card's `.nav` scrolls
 * (`overflow-y: auto`), which per the CSS spec forces its `overflow-x` to
 * compute as `auto` too, so a popover positioned beside the card as an ordinary
 * descendant is clipped by that, invisibly. Both are therefore `position:
 * fixed`, portaled to `document.body`, and given a place in viewport
 * coordinates.
 *
 * That is one behaviour with four parts that have to stay in step, which is why
 * it is written once rather than twice:
 *
 *  · measure on open, from the trigger;
 *  · re-measure on scroll and resize, because both move the trigger without
 *    React re-rendering anything;
 *  · clamp against the popover's own height, because a `position: fixed` box
 *    below the fold cannot be scrolled into view;
 *  · close on outside click and on Escape - the two ways every popup in this
 *    shell closes.
 *
 * `openKey` is the *identity* of what is open, not a boolean: the rail has one
 * flyout per section sharing one trigger ref, and moving straight from one
 * section to the next has to re-place the popover, which a boolean that stayed
 * `true` would not do.
 *
 * A third caller (Billing's scan-box prompts, #243) needed two more things a
 * button-only, right-of-trigger hook could not give it: the trigger is an
 * `<input>`, and the prompt has to hang *below* the box rather than beside it,
 * because it shares the row with the payment rail and a right-anchored panel
 * would land on top of that. Both are parameters rather than forks - `T` for
 * the element type, `side` for which edge it is measured from - so the
 * sidebar flyout and the profile panel, which pass neither, get back exactly
 * the behaviour they had.
 */
/** The least room worth placing a popover in - below its trigger or, once it
 *  flips, above it. Also the floor on the emitted `maxHeight`, so a trigger
 *  scrolled off the bottom of the page cannot produce a negative CSS length. */
const MIN_ROOM = 120;

/** Where a popover goes, in viewport coordinates: exactly one of `top` and
 *  `bottom` is set, and a caller applies the object whole rather than picking
 *  fields out of it - a `"below"` caller that applied only `top` would leave a
 *  flipped popover wherever the last placement put it. */
export interface PopoverPlacement {
  top?: number;
  bottom?: number;
  left: number;
  maxHeight?: number;
}

/** Pure placement math for {@link usePositionedPopover}, pulled out so it can
 *  be pinned down in a test without a DOM: the three bugs this has already had
 *  (round 2 clamped the wrong axis, round 3's `maxHeight` formula never fired,
 *  and #249's field near the bottom of the rail got a hundred pixels to show
 *  five rows in) were all placement-formula mistakes a plain unit test
 *  catches. */
export function placePopover(
  trigger: { top: number; bottom: number; left: number; right: number },
  viewport: { width: number; height: number },
  popoverSize: number,
  side: "right" | "below",
  margin = 8,
): PopoverPlacement {
  if (side === "below") {
    const maxLeft = viewport.width - popoverSize - margin;
    const left = Math.max(margin, Math.min(trigger.left, maxLeft));
    const below = viewport.height - (trigger.bottom + margin) - margin;
    const above = trigger.top - margin - margin;
    // Below the trigger is where a prompt belongs, and it stays there for as
    // long as there is more room there - which for the scan box, sitting in
    // the top strip, is always (#243).
    //
    // The customer typeahead (#249) hangs off a field near the *bottom* of the
    // rail, where "below" is a hundred-odd pixels: five customers in a box the
    // height of two, with the rest behind a scrollbar the cashier has to find
    // while somebody reads their number out. Anchoring to the trigger's top
    // edge instead - `bottom` rather than `top`, so no height has to be
    // measured and nothing has to flash in the wrong place first - puts the
    // whole list on screen. This is the clamp promise the hook already makes
    // for the `"right"` side, kept for the other one.
    //
    // `above >= MIN_ROOM` as well as `above > below`, so the flip only happens
    // when it actually buys something: on a viewport too short for either half,
    // flipping to a `bottom` anchor with the floor applied would push the box's
    // *top* edge off the screen, and a `position: fixed` node cannot be
    // scrolled back into view. Cramped below beats unreachable above.
    if (above > below && above >= MIN_ROOM) {
      return { bottom: viewport.height - trigger.top + margin, left, maxHeight: above };
    }
    return { top: trigger.bottom + margin, left, maxHeight: Math.max(MIN_ROOM, below) };
  }
  const maxTop = viewport.height - popoverSize - margin;
  const top = Math.max(margin, Math.min(trigger.top, maxTop));
  const left = trigger.right + margin;
  return { top, left };
}

export interface PositionedPopover<T extends HTMLElement = HTMLButtonElement> {
  /** Where to render it, in viewport coordinates. `null` until it has been
   *  placed - render nothing before that, or it flashes at the top-left
   *  corner on the way to its real spot. `maxHeight` is only set on the
   *  `"below"` side, where the popover grows downward by an amount CSS alone
   *  cannot bound: a `100vh`-based rule has no way to know how far down the
   *  page the trigger sits, so a popover starting well below the top of the
   *  viewport would still be let to run past the bottom. Apply it as an
   *  inline style on the popover; `"right"` callers keep clamping `top`
   *  against their own measured height instead, unaffected.
   *
   *  Exactly one of `top` and `bottom` is set, and `"below"` callers must
   *  apply both (the undefined one is a no-op in a React style object): a
   *  trigger with more room above it than below is anchored by its top edge
   *  instead, and a caller passing only `top` would leave that popover
   *  wherever the last placement put it. `"right"` always sets `top`. */
  at: PopoverPlacement | null;
  /** Goes on the control the popover hangs off. */
  triggerRef: RefObject<T>;
  /** Goes on the portaled popover. It holds the node for the outside-click
   *  test, and re-places against the popover's real height the moment it is in
   *  the DOM - before that there is nothing to measure. */
  popoverRef: (node: HTMLDivElement | null) => void;
}

export function usePositionedPopover<T extends HTMLElement = HTMLButtonElement>(
  openKey: string | null,
  onClose: () => void,
  /** Stand-in size for the first placement, before the popover has ever
   *  mounted: height on the `"right"` side (clamped against the bottom of the
   *  screen), width on the `"below"` side (clamped against the right edge).
   *  Pass the popover's CSS max on whichever axis that is, so the first clamp
   *  is conservative rather than optimistic. */
  fallbackSize = 0,
  /** Which edge of the trigger the popover is measured from. `"right"` (the
   *  rail flyout, the profile panel) clamps vertically, because the trigger
   *  sits in a column that can run the height of the screen. `"below"` (the
   *  scan box) clamps horizontally instead, because the trigger sits in a row
   *  near the right edge of the page. */
  side: "right" | "below" = "right",
): PositionedPopover<T> {
  const [at, setAt] = useState<PopoverPlacement | null>(null);
  const triggerRef = useRef<T>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  // Held in a ref so the listener effect can key on `openKey` alone. Callers
  // pass an inline closure, which is a new function on every render, and an
  // effect keyed on it would tear down and rebind two document listeners on
  // every render of the sidebar.
  const close = useRef(onClose);
  close.current = onClose;

  const place = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const popoverSize =
      side === "below"
        ? (popoverRef.current?.offsetWidth ?? fallbackSize)
        : (popoverRef.current?.offsetHeight ?? fallbackSize);
    const { top, bottom, left, maxHeight } = placePopover(
      rect,
      { width: window.innerWidth, height: window.innerHeight },
      popoverSize,
      side,
    );
    setAt((current) =>
      current &&
      current.top === top &&
      current.bottom === bottom &&
      current.left === left &&
      current.maxHeight === maxHeight
        ? current
        : { top, bottom, left, maxHeight },
    );
  }, [fallbackSize, side]);

  useEffect(() => {
    if (!openKey) {
      setAt(null);
      return;
    }
    place();
    window.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", place, true);
    };
  }, [openKey, place]);

  const attachPopover = useCallback(
    (node: HTMLDivElement | null) => {
      popoverRef.current = node;
      if (node) place();
    },
    [place],
  );

  // Checked against both halves of the popover: the trigger stayed where it
  // was rendered, the panel was portaled to `document.body`, and neither one
  // alone is "the popover".
  useEffect(() => {
    if (!openKey) return;
    const off = new AbortController();
    const { signal } = off;
    document.addEventListener(
      "pointerdown",
      (e) => {
        const target = e.target as Node;
        if (triggerRef.current?.contains(target)) return;
        if (popoverRef.current?.contains(target)) return;
        close.current();
      },
      { signal },
    );
    document.addEventListener(
      "keydown",
      (e) => {
        if (e.key === "Escape") close.current();
      },
      { signal },
    );
    return () => off.abort();
  }, [openKey]);

  return { at, triggerRef, popoverRef: attachPopover };
}
