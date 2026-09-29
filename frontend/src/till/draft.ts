// The bill that survives a crash (#244, grill ruling 30 Jul 2026).
//
// The in-progress bill is not money and it is not a fact - it is what the
// screen currently shows, and it autosaves continuously so that a dead line, a
// closed tab or a power cut hands it straight back rather than making the
// cashier re-scan everything a customer already stood through once. One row,
// at a fixed key, written through on every cart or customer-field change and
// read back once on mount.
//
// Flag, never block (Rule 8): a failed write is a safety net that missed, not
// a reason to stop the bill. It logs and the counter carries on - the till's
// blocked banner is for something else entirely, and this file never raises
// one.

import { newKey } from "./cart";
import type { Cart } from "./cart";
import type { TillDb } from "./db";
import { newLegKey } from "./exchange";
import { tillToday } from "./pricing";
import type { TillCustomer } from "./types";

/** The one row this table ever holds - an outbound key, so nothing on the
 *  payload itself has to name it. */
const DRAFT_KEY = "current";

/** Everything needed to restore mid-scan - the cart (lines, exchange and
 *  payment together), the customer strip, which printed bill this counter was
 *  re-entering (if any) and the date off that paper, and when this row was
 *  last written.
 *
 *  Restoring `paper`/`paperAt` onto the screen is not this file's job: paper
 *  mode is deliberately never held in React state (`Billing.tsx`'s
 *  `outstandingPaperSeq`), always re-derived from the address bar, and a
 *  restore that pushed it back into the URL is `Billing.tsx`'s call to make -
 *  this file only decides *whether* the snapshot as a whole should land. */
export interface DraftPayload {
  cart: Cart;
  customer: TillCustomer;
  paper: number | null;
  /** The date and time off the printed copy, `<input type="datetime-local">`
   *  shape (`Billing.tsx`'s `localNow`) - null whenever `paper` is, since
   *  there is no paper without a re-entry in progress. */
  paperAt: string | null;
  savedAt: string;
}

/** Write the draft through. Never throws: a draft write standing between a
 *  cashier and the next scan would turn a safety net into a gate. */
export async function persistDraft(
  db: TillDb,
  cart: Cart,
  customer: TillCustomer,
  paper: number | null,
  paperAt: string | null,
): Promise<void> {
  try {
    await db.draft.put(
      { cart, customer, paper, paperAt, savedAt: new Date().toISOString() },
      DRAFT_KEY,
    );
  } catch (error) {
    console.error("persistDraft failed - the bill continues without a safety net", error);
  }
}

/** The draft as it last stood, or null when there is nothing parked. Never
 *  throws, the same as `persistDraft`: a blocked or corrupt read must not
 *  stop the bill from starting - it just starts without a restore. */
export async function readDraft(db: TillDb): Promise<DraftPayload | null> {
  try {
    return (await db.draft.get(DRAFT_KEY)) ?? null;
  } catch (error) {
    console.error("readDraft failed - starting the bill without a restore", error);
    return null;
  }
}

/** Cleared on commit success, New bill, and Hold - the three moments the
 *  screen in front of the cashier stops being the bill this row remembers.
 *
 *  Never throws, the same as `persistDraft`: the bill this call follows has
 *  already committed, held, or been deliberately cleared on screen, and a
 *  storage error here must not turn into an error note on a sale that already
 *  went through. */
export async function clearDraft(db: TillDb): Promise<void> {
  try {
    await db.draft.delete(DRAFT_KEY);
  } catch (error) {
    console.error("clearDraft failed - the bill continues without a safety net", error);
  }
}

// --- landing the read back on screen (the mount race, #244 binding rule 6, --
// --- and the 2 Aug 2026 atomic-restore and draft-age rulings) ---------------
//
// The read is issued at mount and IndexedDB answers whenever it answers, so by
// the time it lands the screen may already have moved on - a piece scanned or
// marked for return before the read came back. Whichever is on screen by
// then is realer than a saved draft, so a draft never overwrites it.
//
// The round-2 defect was asking that question twice - once for the cart, once
// for the customer strip - so a return landing on the cart could leave the
// customer strip's own (separate) "is it empty" check free to pull in a name,
// mobile and GSTIN from a different, crashed bill. One predicate now, asked
// once over the whole snapshot: cart lines, exchange legs and customer fields
// together. Anything real anywhere on that list drops the draft whole; nothing
// real anywhere applies it whole. Never a blend of the two.
//
// Pure, so the mount race's outcome - and the same-day and paper checks that
// gate it - are values to assert on rather than a timing a test has to win.

/** What this read decided, for `Billing.tsx` to act on:
 *
 *   · `drop` - real content is already on screen; the draft is not applied.
 *   · `apply` - nothing real is on screen, the draft was saved today, and its
 *     paper state (if any) still matches what the counter is on: restore the
 *     whole snapshot.
 *   · `stale` - otherwise identical to `apply`, but the draft is from a
 *     previous business day (Rule 11, "deadlines are data, not memory" -
 *     `savedAt` is checked, never assumed). Not auto-applied; parked for the
 *     cashier to resume or drop.
 *   · `paper-conflict` - the draft was mid a paper re-entry, and the paper
 *     number it names is no longer one `outstandingPaperSeq` regards as
 *     outstanding, or the address bar already asks for a different one.
 *     Applying the cart and customer anyway while silently dropping or
 *     rewriting the paper state is exactly the "mix a restored half with an
 *     on-screen state nobody chose" failure rule 0 forbids - so this, too, is
 *     parked for the cashier rather than resolved silently. */
export type DraftRestoration =
  | { kind: "drop" }
  | { kind: "apply"; draft: DraftPayload }
  | { kind: "stale"; draft: DraftPayload }
  | { kind: "paper-conflict"; draft: DraftPayload };

/** Is there anything on screen a restore would have to mix with, rather than
 *  replace whole - a cart line, an exchange leg, or a customer field somebody
 *  already typed. */
function somethingRealOnScreen(onScreen: { cart: Cart; customer: TillCustomer }): boolean {
  return (
    onScreen.cart.lines.length > 0 ||
    Boolean(onScreen.cart.exchange) ||
    Boolean(onScreen.customer.name || onScreen.customer.mobile || onScreen.customer.gstin)
  );
}

/**
 * The one restore decision for the whole snapshot (2 Aug 2026 rulings).
 *
 * `paperOk` is computed by the caller, not here: whether a draft's `paper`
 * still matches what the counter and the address bar currently say needs
 * `outstandingPaperSeq` and a live `TillSnapshot`, which are `Billing.tsx`'s
 * to read, not this file's. A draft with no paper claim (`paper: null`) is
 * `paperOk` whenever the counter is not mid a *different* paper re-entry
 * either - see `Billing.tsx`'s call site.
 */
export function restoredDraft(
  onScreen: { cart: Cart; customer: TillCustomer },
  draft: DraftPayload,
  today: string,
  paperOk: boolean,
): DraftRestoration {
  if (somethingRealOnScreen(onScreen)) return { kind: "drop" };
  if (!paperOk) return { kind: "paper-conflict", draft };
  return tillToday(new Date(draft.savedAt)) === today
    ? { kind: "apply", draft }
    : { kind: "stale", draft };
}

// --- crash-restore line identity (ruled, 2 Aug 2026) ------------------------
//
// `newKey`/`newLegKey` restart their counters at l1/x1 on every page load, so a
// draft's saved keys can collide with the very next scan's - and a shared key
// means an edit or a removal hits both lines, and worse, a PIN authorisation
// keyed by `line.key` (`pin.ts`'s `covers`) silently covers a line nobody
// approved. So every line and leg a draft restores gets a brand-new key, minted
// from the same two generators a fresh scan uses rather than a third one of
// this file's own - the collision this issue exists to close.

/** Every restored line and leg, rekeyed, with every reference to an old key
 *  inside the same draft remapped in the same pass - a PIN authorisation's
 *  `ref` most of all, since that is what binds one manager's approval to
 *  exactly one line (binding rule 0a).
 *
 *  Called once in the mount effect, before `setCart` - never inside a state
 *  updater, which React 18 StrictMode double-invokes in dev, and minting keys
 *  there would run the generators twice and split the mapping between the
 *  lines and the refs that point at them. */
export function rekeyDraft(draft: DraftPayload): DraftPayload {
  const map = new Map<string, string>();
  const lines = draft.cart.lines.map((line) => {
    const key = newKey();
    map.set(line.key, key);
    return { ...line, key };
  });
  const exchange = draft.cart.exchange
    ? {
        ...draft.cart.exchange,
        lines: draft.cart.exchange.lines.map((leg) => {
          const key = newLegKey();
          map.set(leg.key, key);
          return { ...leg, key };
        }),
      }
    : null;
  const authorisation = draft.cart.authorisation
    ? {
        ...draft.cart.authorisation,
        // A late-return ask names the original bill, not a mutable cart line.
        // The fallback leaves that document number exactly as it stood.
        asks: draft.cart.authorisation.asks.map((ask) => ({
          ...ask,
          ref: map.get(ask.ref) ?? ask.ref,
        })),
      }
    : null;
  return { ...draft, cart: { ...draft.cart, lines, exchange, authorisation } };
}
