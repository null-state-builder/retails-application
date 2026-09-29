// The three things standing between a counter and a lost bill (#189, D10).
//
// Everything else in `src/till/` assumes the counter's database is there and is
// this tab's to write. This file is where those two assumptions are checked, and
// it is deliberately the only place that talks to the browser's storage and lock
// APIs, so what the till believes about its own device is decided once.
//
//   · **Persistence.** `navigator.storage.persist()` asks the browser not to
//     evict us under pressure. What it would evict is a queue of printed,
//     paid-for bills, so the ask is made at every store open rather than once.
//   · **The sentinel.** A browser that cleared site data leaves a counter that
//     starts again at bill 1 with no idea anything happened. The marker that
//     detects it cannot live in the database it is watching, so it sits in
//     `localStorage` - the only other durable place a browser offers - and the
//     till refuses to bill until somebody has recovered it deliberately.
//   · **The lock.** One till per store is the invariant the whole numbering
//     design rests on. IndexedDB already stops two tabs *interleaving* a commit,
//     but it will happily let a second tab open the same counter and start
//     billing beside the first. `navigator.locks` is what notices, and the second
//     tab is told rather than allowed.
//
// The block itself (`billingBlock`) is a pure function over those facts, for the
// same reason `status.ts` is: "this counter may not bill" has to mean the same
// thing on the Billing screen, on Till & Sync and in a test.

import { AUTHORITY_EXPIRED } from "./authority";

/** Where the "this device has been a till" marker lives, per store. */
export function seenKey(storeCode: string): string {
  return `kdps-till-seen-${storeCode}`;
}

/** Where "this device lost the counter's data and has not recovered yet" lives.
 *
 *  A second marker rather than an inference, and the reason is a page reload. The
 *  detector below asks "was this a till, and is its database empty" - but the
 *  first sync after a loss writes rows, so on the next reload the database is no
 *  longer empty and the loss would appear never to have happened. The flag is
 *  written the moment it is noticed and cleared only by a recovery somebody
 *  asked for, so a reload cannot lift the block by itself. */
export function lostKey(storeCode: string): string {
  return `kdps-till-lost-${storeCode}`;
}

/** Ask the browser to keep this counter's database. Never throws: an older
 *  browser without the API is a device we still have to sell from. */
export async function askForPersistentStorage(): Promise<boolean> {
  try {
    return (await navigator.storage?.persist?.()) ?? false;
  } catch {
    return false;
  }
}

/** Remember that this device is a counter, and that its data is currently good. */
export function markTillSeen(storeCode: string): void {
  localStorage.setItem(seenKey(storeCode), "1");
  localStorage.removeItem(lostKey(storeCode));
}

/**
 * Has this device been a till before and lost the database since?
 *
 * Best-effort, and honest about it: both markers live in `localStorage`, which a
 * browser clearing *all* site data would take with it - at which point the
 * device looks like a machine that has never been a till, and the register call
 * at boot is what stops it re-issuing numbers. What this catches is the common
 * case, IndexedDB evicted under storage pressure or a "clear cached images and
 * files", where `localStorage` survives and the counter would otherwise start
 * again at bill 1 with nobody any the wiser.
 *
 * Writes the loss down when it finds one, so a reload cannot un-notice it.
 */
export async function detectStorageLoss(
  db: { meta: { count: () => Promise<number> } },
  storeCode: string,
): Promise<boolean> {
  if (localStorage.getItem(lostKey(storeCode)) === "1") return true;
  if (localStorage.getItem(seenKey(storeCode)) !== "1") return false;
  if ((await db.meta.count()) > 0) return false;
  localStorage.setItem(lostKey(storeCode), "1");
  return true;
}

// ------------------------------------------------------------------ lock -----

/** The lock name for a store's counter. Per store, not per device: two shops on
 *  one machine are two counters and neither blocks the other. */
export function lockName(storeCode: string): string {
  return `kdps-till-${storeCode}`;
}

/**
 * The minimum of `navigator.locks` this needs, so a test can be a second tab.
 *
 * Typed by hand rather than taken from the DOM lib because only one call shape is
 * used and a fake has to be able to answer it.
 */
export interface LockManagerLike {
  request(
    name: string,
    options: { mode: "exclusive"; ifAvailable: true },
    callback: (lock: unknown | null) => Promise<void>,
  ): Promise<void>;
}

/**
 * This tab's claim on the store's counter.
 *
 * `ifAvailable` rather than a queued request, and that is the whole design: a
 * second tab must find out *now* that it is second, not sit waiting for a lock
 * that the first tab holds until it closes. So the request returns immediately
 * with a lock or with null, and `held` is the answer.
 *
 * Holding it is a promise that does not settle until `release`. That is how the
 * Web Locks API works - the lock lives as long as the callback's promise - and it
 * means a tab that is closed, crashed or put to sleep by the OS drops the lock
 * without anybody having to clean up after it. A lock file on a shop floor that
 * needed tidying up after a power cut would be worse than no lock.
 */
export class CounterLock {
  private releaseHeld: (() => void) | null = null;
  private holding = false;
  /** Do we still want it? Not the same question as whether we have it, and the
   *  gap between the two is a real hazard: a grant is asynchronous, and this
   *  engine may have been stopped while the browser was deciding. Without this,
   *  a lock granted after `release` would be held by nobody, for ever, by a tab
   *  that has already moved on - and every counter in that tab afterwards would
   *  be told it was the second one. */
  private wanted = false;
  /** An ask the browser has not answered yet, so a second ask joins it rather
   *  than racing it - see `acquire`. */
  private asking: Promise<boolean> | null = null;
  /** The lock manager itself failed. Counted with "there is no lock manager":
   *  an API that told us nothing is not evidence of a second till. */
  private broken = false;

  constructor(
    private readonly storeCode: string,
    private readonly locks: LockManagerLike | undefined = navigator.locks as
      | LockManagerLike
      | undefined,
  ) {}

  /** Does this tab own the counter?
   *
   *  True when there is no lock API at all, deliberately. The lock is a warning
   *  layer over an invariant IndexedDB already enforces inside a commit, so a
   *  browser too old to offer it is a browser we still sell from - refusing to
   *  bill because an advisory API is missing would turn a nicety into a lost
   *  day's trade. */
  held(): boolean {
    return this.locks === undefined || this.broken || this.holding;
  }

  /**
   * Try to take it. Safe to call repeatedly - a tab that is already second asks
   * again on the sync interval, so closing the first tab hands the counter over
   * without a reload.
   *
   * What is **not** awaited here is the request itself. `locks.request` resolves
   * when its callback does, and this callback deliberately does not until
   * `release` - so awaiting it would hang the caller for as long as the lock is
   * held, which is the whole session. The answer comes out of the callback the
   * moment the browser has decided, and the request is left running underneath.
   */
  acquire(): Promise<boolean> {
    const locks = this.locks;
    if (locks === undefined || this.broken || this.holding) return Promise.resolve(true);
    // Single-flight. `refresh` asks on the commit path and on two timers, and a
    // second `ifAvailable` request made while the first is still deciding finds
    // the name taken *by this very tab* and answers false - which would publish
    // "already open in another tab" over a counter that had just been granted
    // the lock, and leave it refusing to bill until the next refresh.
    if (this.asking) return this.asking;
    this.wanted = true;
    const asking = new Promise<boolean>((decided) => {
      void locks
        .request(
          lockName(this.storeCode),
          { mode: "exclusive", ifAvailable: true },
          async (lock) => {
            // Granted to an engine that has since been stopped. Returning here
            // ends the callback, which is what gives the lock straight back -
            // holding it would strand the counter behind a tab that has moved on.
            if (!lock || !this.wanted) {
              decided(false);
              return;
            }
            this.holding = true;
            decided(true);
            // Held until `release` - see the class comment.
            await new Promise<void>((resolve) => {
              this.releaseHeld = () => {
                this.releaseHeld = null;
                this.holding = false;
                resolve();
              };
            });
          },
        )
        // A lock manager that threw is a browser telling us nothing useful, and
        // nothing is not evidence of a second till - so this counter goes on
        // billing, for the reason `held` gives.
        .catch(() => {
          this.broken = true;
          decided(true);
        });
    });
    this.asking = asking;
    // Cleared once the browser has decided, not once the lock is given back: the
    // request the callback is sitting inside outlives this promise by design.
    return asking.finally(() => {
      this.asking = null;
    });
  }

  /** Give it up - the engine stopping, or a sign-out.
   *
   *  Withdrawing the *want* is the load-bearing half. A grant that has not landed
   *  yet has no `releaseHeld` to call, and one that lands afterwards would
   *  otherwise take a lock nobody is waiting on and never let it go. */
  release(): void {
    this.wanted = false;
    this.releaseHeld?.();
  }
}

// ----------------------------------------------------------------- block -----

export interface BillingBlockInput {
  /** The browser threw this counter's database away and nobody has recovered it. */
  storageLost: boolean;
  /** This tab owns the counter. False means another tab has it open. */
  lockHeld: boolean;
  /** The 24-hour window this device bills offline under has closed (OPS-09,
   *  PRD §10.1). Nothing already recorded is affected - see `billingBlock`. */
  authorityExpired?: boolean;
  /** The counter is in its transfer pause (change PRD §10.2). */
  paused?: boolean;
}

/** What a paused counter says, on Save & Print and on Till & Sync. */
export const BILLING_PAUSED =
  "Billing is paused while stock is sent out of this store. Resume it from Till & Sync.";

/** Thrown from inside the commit transaction when the counter is paused, so a
 *  bill cannot slip past a stale screen or a second path to the commit. */
export class TillPausedError extends Error {
  constructor() {
    super(BILLING_PAUSED);
    this.name = "TillPausedError";
  }
}

/**
 * Why this counter may not take a bill, or "" if it may.
 *
 * Two reasons, and they are the same kind of reason: in both cases the till does
 * not know, on its own, which number the next bill should carry. Everything else
 * that can be wrong at a counter - no network, a refused bill, an empty price
 * list - is either the designed state or somebody else's problem, and none of it
 * stops a sale (Rule 8, and grill Q5's "billing nahi ruke").
 *
 * The wording is what a store person reads on the Save & Print row, so it says
 * what happened and what to do about it, in that order.
 */
export function billingBlock(input: BillingBlockInput): string {
  if (input.storageLost) {
    return (
      "This device lost the counter's local data, so it does not know which bill " +
      "number it is on. Recover the counter from Till & Sync before billing."
    );
  }
  if (!input.lockHeld) {
    return (
      "This counter is already open in another tab or window, and one store bills " +
      "from one place. Close this tab and go back to the first one."
    );
  }
  // The third reason, and the same kind as the other two: the till does not know,
  // on its own, that it is still the machine allowed to number this series
  // (PRD §10.1). What it emphatically does *not* do is touch anything already
  // recorded - queued bills stay queued, the queue still drains on reconnect, and
  // renewing puts the counter straight back to work.
  if (input.authorityExpired) return AUTHORITY_EXPIRED;
  // The fourth, and the only one a person chose: the store's stock was let go
  // for a transfer, and this counter's shelf is no longer the truth until it has
  // taken a fresh one (change PRD §10.2, Anand, 25 September 2026).
  if (input.paused) return BILLING_PAUSED;
  return "";
}
