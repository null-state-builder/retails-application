// The till, running (#180, D10 step 3).
//
// `db.ts` holds the counter's world, `numbering.ts` commits a bill into it and
// `sync.ts` moves rows in each direction. This is the thing that decides *when*:
// what happens at store open, what happens every minute, what happens the moment
// the line comes back, and what the sync light says while all of that is going on.
//
// It is a plain object with a subscribe/snapshot pair rather than a hook, for two
// reasons. The till outlives any one screen - a bill queued on the Sell page must
// keep draining while the cashier is looking at the Dashboard - and the sync
// engine has to be drivable from a test without a React renderer.
//
// Everything it does is safe to do twice. A drain is single-flight, a sync is
// idempotent by key, and the server's idempotency makes a re-offered bill a
// no-op. That matters because the triggers overlap by design: a till that comes
// back online during its own retry backoff should sync at once, not wait.
//
// One departure from `design.md`'s sketch of `guard.ts` survives (#189 built the
// rest of it): **the storage sentinel is in `localStorage`, not in `meta`**.
// design.md lists `storageSentinel` as a `meta` row, and a sentinel that lives
// inside the database cannot survive the database being thrown away - which is
// the one event it exists to detect. It has to sit outside, and `localStorage` is
// the only other durable place a browser offers. See `guard.ts`.

import { prepareOnline, submitOnline, unfinishedSubmission, reviseRejected } from "./online";
import type { PendingOnlineBill } from "./db";

import { financialYear } from "../lib/fiscal";

import {
  FIRST_WORDING,
  NOT_ASKED,
  drainConsents,
  queueConsent,
  standing,
  unsentFor,
} from "./consent";
import type { ConsentState, QueuedConsent, TillConsentWording } from "./consent";
import { META, hasUnscopedDatabase, readMeta, tillDb, writeMeta } from "./db";
import type { TillNumbering } from "./invoiceNumbers";
import { alterationChargeFrom } from "./alteration";
import type { HeldBill, TillDb } from "./db";
import {
  CounterLock,
  askForPersistentStorage,
  billingBlock,
  detectStorageLoss,
  markTillSeen,
} from "./guard";
import { dropHold, keepHold, listHolds, parkHold } from "./held";
import type { HeldPayload } from "./held";
import { commitBill, paperEntries, previewNextNumber, reenterPaperBill } from "./numbering";
import { refusalsOnFrom, whyOfflineRefuses } from "./onlineOnly";
import { whySplitIsOff } from "./split";
import { findHeldBill } from "./original";
import { deriveStatus } from "./status";
import type { SyncStatus } from "./status";
import { authorityState } from "./authority";
import type { AuthorityState } from "./authority";
import {
  clearHalt,
  drainQueue,
  forceBootstrap,
  pushHeld,
  deviceStateFrom,
  readDevice,
  reconcileRegister,
  renewAuthority,
  syncDown,
} from "./sync";
import type { TillDeviceState } from "./sync";
import { httpTransport, TillHttpError } from "./transport";
import type { TillTransport } from "./transport";
import { beginPause, isRefusal, movePause, positionNow, readPause } from "./pause";
import type {
  BillDraft,
  HandoverState,
  QueueHalt,
  QueuedBill,
  RegisterPayload,
  TillAlterationCharge,
  TillPauseState,
  TillTaxSettings,
} from "./types";

/** How often the counter pulls new prices and offers while it is online. */
export const DATASET_INTERVAL_MS = 5 * 60_000;
/** How often it offers whatever is in the queue. */
export const QUEUE_INTERVAL_MS = 60_000;

export interface TillCounts {
  items: number;
  stock: number;
  offers: number;
  salespeople: number;
  managers: number;
  gstSlabs: number;
  customers: number;
  held: number;
}

export interface TillSnapshot {
  storeCode: string;
  onlineAlpha: boolean;
  /** Alpha cached data is exposed only after this session receives its projection. */
  liveAccessVerified: boolean;
  onlinePending: PendingOnlineBill | null;
  devicePaired: boolean;
  legacyStorageQuarantined: boolean;
  /** The engine has read the local database at least once. Until then a screen
   *  knows nothing, which is not the same as knowing the till is empty. */
  ready: boolean;
  status: SyncStatus;
  pending: number;
  queue: QueuedBill[];
  datasetReady: boolean;
  syncedAt: string | null;
  online: boolean;
  halt: QueueHalt | null;
  register: RegisterPayload | null;
  counts: TillCounts;
  /** The carts this counter has parked (#185). In the snapshot rather than read
   *  per screen because two screens want them - the Billing hold list and the
   *  day-close prompt - and both have to see the same list at the same moment as
   *  the count on the sync panel. There are a handful of these at most. */
  held: HeldBill[];
  /** What the next bill will be numbered, for a screen to show. Advisory. */
  nextNumber: string;
  /** A sync or a drain is in flight. */
  busy: boolean;
  /** Flags the server raised on the bills that went up last - a hole, an offer
   *  mismatch. Shown on the Dashboard's action queue, never at the counter. */
  lastFlags: string[];
  /** Why the last attempt did not finish, if it did not. */
  lastError: string;
  /** Why this counter may not take a bill at all, or "" if it may (#189). The
   *  Billing screen refuses Save & Print on it; see `guard.billingBlock`. */
  blocked: string;
  /** The browser threw the local database away and nobody has recovered it. */
  storageLost: boolean;
  /** This tab owns the store's counter (as opposed to being the second one). */
  lockHeld: boolean;
  /** The last register handover done on this machine (#189) - the list of bills
   *  the old one never sent. Null until one happens, and again once the store
   *  has put the list away. */
  handover: HandoverState | null;
  /** Numbers this counter has keyed back in from their printed copies, this
   *  financial year. The screen's ticks, and the reason a receipt cannot go in
   *  twice; unlike `handover`, nothing on a screen can clear it. */
  paperEntered: number[];
  /** This device's identity as the store's counter, and the window it may bill
   *  offline in (OPS-09). Null until a dataset has told it, which is not the same
   *  as "no counter is registered" - see `TillIdentity.registered`. */
  device: TillDeviceState | null;
  /** How much of the 24-hour window is left, on the least generous reading
   *  available - see `authority.ts` on the clock. */
  authority: AuthorityState;
  /** The scan tones are off on this counter (#247, grill Q8). In the snapshot
   *  rather than read per screen because two screens want it at once - Billing
   *  plays the tone, Till & Sync draws the switch - and a switch that could
   *  disagree with what the counter is actually doing is worse than no switch. */
  muted: boolean;
  /** The counter's transfer pause, or null (change PRD §10.2). While it is
   *  set, `blocked` says so and no bill is finalised. */
  pause: TillPauseState | null;
  /** The allocation this counter holds, as the last sync said. */
  allocationVersion: number | null;
  /** The tax settings this counter holds (ticket 03), or null before a sync
   *  carried them. What it bills by with no network. */
  taxSettings: TillTaxSettings | null;
  /** The new invoice series this counter holds (ticket 04), or null before a
   *  sync carried it. */
  numbering: TillNumbering | null;
  /** Whether this counter refuses online-only work while offline (ticket 05).
   *  On until a sync says otherwise (B4). */
  onlineOnlyRefusals: boolean;
  /** Whether an override needs a manager other than the cashier (ticket 06).
   *  Off until a sync says otherwise (B3). */
  managerPinRules: boolean;
  /** Whether a line may be split between two salespeople (ticket 08). Off
   *  until a sync says otherwise (B3). */
  splitSale: boolean;
  /** Whether this counter offers a customer display (ticket 09). Off until a
   *  sync says otherwise (B3). */
  customerDisplay: boolean;
  /** Whether this counter asks for customer consent (ticket 15). Off until a
   *  sync says otherwise (B3). */
  customerConsent: boolean;
  /** Whether this counter offers "Reservation pickup" (ticket 20). Off until a
   *  sync says otherwise (B3). */
  customerReservation: boolean;
  /** Whether this counter shows the customer's saved sizes (ticket 18). Off
   *  until a sync says otherwise (B3). */
  savedSizes: boolean;
  /** Whether this counter offers special order collection (ticket 21). Off
   *  until a sync says otherwise (B3). */
  specialOrders: boolean;
  /** Whether this counter sells and takes gift vouchers (ticket 19). Off until
   *  a sync says otherwise (B3). */
  giftVouchers: boolean;
  /** The alteration charge line this counter offers (ticket 22), or null while
   *  the store's switch is off. Off until a sync says otherwise (B3). */
  alterationCharge: TillAlterationCharge | null;
  /** The consent wording this counter asks with (ticket 15). */
  consentWording: TillConsentWording;
  /** Consent answers on this device not yet at head office. */
  consentsPending: number;
  /** Consent answers head office refused for good, kept on this device. */
  consentsRefused: number;
}

const EMPTY_COUNTS: TillCounts = {
  items: 0,
  stock: 0,
  offers: 0,
  salespeople: 0,
  managers: 0,
  gstSlabs: 0,
  customers: 0,
  held: 0,
};

function initialSnapshot(storeCode: string): TillSnapshot {
  return {
    storeCode,
    onlineAlpha: false,
    liveAccessVerified: false,
    onlinePending: null,
    devicePaired: false,
    legacyStorageQuarantined: false,
    ready: false,
    status: { colour: "amber", label: "Starting", reason: "Opening the counter…" },
    pending: 0,
    queue: [],
    datasetReady: false,
    syncedAt: null,
    online: true,
    halt: null,
    register: null,
    counts: EMPTY_COUNTS,
    held: [],
    nextNumber: "",
    busy: false,
    lastFlags: [],
    lastError: "",
    blocked: "",
    storageLost: false,
    lockHeld: true,
    handover: null,
    paperEntered: [],
    device: null,
    authority: { known: false, remainingMs: 0, expired: false, until: null },
    muted: false,
    pause: null,
    allocationVersion: null,
    taxSettings: null,
    numbering: null,
    onlineOnlyRefusals: true,
    managerPinRules: false,
    splitSale: false,
    customerDisplay: false,
    customerConsent: false,
    customerReservation: false,
    savedSizes: false,
    specialOrders: false,
    giftVouchers: false,
    alterationCharge: null,
    consentWording: FIRST_WORDING,
    consentsPending: 0,
    consentsRefused: 0,
  };
}

export class TillEngine {
  readonly db: TillDb;
  private snapshot: TillSnapshot;
  private readonly listeners = new Set<() => void>();
  private timers: ReturnType<typeof setInterval>[] = [];
  private retry: ReturnType<typeof setTimeout> | null = null;
  private started = false;
  private storageLost = false;
  private readonly lock: CounterLock;
  /** Bumped by every start and every stop, so a boot sequence that was
   *  interrupted half way through stops touching the engine it no longer owns. */
  private generation = 0;
  readonly storeCode: string;
  readonly storageScope: string;
  private readonly transport: TillTransport;
  private readonly initialOnlineAlpha: boolean;
  private liveAccessVerified = false;

  constructor(
    storeCode: string,
    transport: TillTransport = httpTransport,
    lock?: CounterLock,
    storageScope = storeCode,
    onlineAlpha = false,
  ) {
    this.storeCode = storeCode;
    this.storageScope = storageScope;
    this.transport = transport;
    this.initialOnlineAlpha = onlineAlpha;
    this.db = tillDb(storageScope);
    this.lock = lock ?? new CounterLock(storageScope);
    this.snapshot = initialSnapshot(storeCode);
    this.snapshot.onlineAlpha = onlineAlpha;
  }

  // -- what React reads ------------------------------------------------------

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Cached and replaced whole, never mutated: `useSyncExternalStore` compares
   *  by identity and would loop for ever on a fresh object each call. */
  getSnapshot = (): TillSnapshot => this.snapshot;

  private publish(patch: Partial<TillSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) listener();
  }

  // -- lifecycle -------------------------------------------------------------

  /**
   * Open the counter.
   *
   * Three things happen here that do not happen anywhere else:
   *
   *   · **Persistent storage is requested.** Without it the browser may evict the
   *     database under pressure, and what it would be evicting is unsynced bills.
   *   · **The counter is claimed.** One store bills from one place; a second tab
   *     finds the lock taken and says so instead of billing beside the first
   *     (#189). It asks again on the queue's interval, so closing the first tab
   *     hands the counter over without a reload.
   *   · **A bootstrap is taken once a day.** The delta cursor laps a quarter of an
   *     hour behind the clock, which *bounds* a missed row rather than preventing
   *     one; a bootstrap cannot miss anything by construction, so taking one at
   *     store open bounds any hole to a single day (contract amendment, #179).
   *   · **The register is read.** The server says how far it has got, and a till
   *     that lost its local state moves its counter forward rather than re-issuing
   *     a number that is already on a posted bill.
   *
   * Start and stop are a matched pair that can run any number of times on one
   * engine, because React runs them that way: StrictMode mounts, unmounts and
   * remounts every effect in development, and a person walking off the Sell page
   * and back does the same in production. The timers are armed before the first
   * `await` for that reason - a `stop()` landing mid-start would otherwise clear
   * an empty list and leave two intervals firing at nobody.
   */
  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;
    const generation = (this.generation += 1);
    window.addEventListener("online", this.onOnline);
    window.addEventListener("offline", this.onOffline);
    this.timers = [
      setInterval(() => void this.pullDatasetAndRefresh(), DATASET_INTERVAL_MS),
      setInterval(() => void this.pushAndRefresh(), QUEUE_INTERVAL_MS),
    ];

    await askForPersistentStorage();
    if (generation !== this.generation) return;
    if (this.storageScope !== this.storeCode) {
      const legacyStorageQuarantined = await hasUnscopedDatabase(this.storeCode);
      if (generation !== this.generation) return;
      this.publish({ legacyStorageQuarantined });
    }
    await this.lock.acquire();
    if (generation !== this.generation) return;
    this.storageLost = await detectStorageLoss(this.db, this.storageScope);
    if (generation !== this.generation) return;
    await this.refresh();
    if (generation !== this.generation) return;
    await this.syncNow({ bootstrapIfNewDay: true });
  }

  /** Put the engine down without putting the *database* down.
   *
   *  The Dexie connection is a per-store singleton shared by whatever else the
   *  tab opens, and closing it here would leave a memoised engine holding a dead
   *  handle - which is exactly what a StrictMode remount does, and what a person
   *  walking off the Sell page and back does in production. It stays open for the
   *  life of the tab; `closeTillDb` exists for tests and for a sign-out that
   *  really is finished with this store.
   *
   *  Subscribers are left alone too: `useSyncExternalStore` unsubscribes itself,
   *  and dropping its listener here would freeze the screen on the last snapshot
   *  it happened to see.
   *
   *  The counter lock **is** given up, unlike the database: a person walking off
   *  the Sell pages is not using the counter, and holding it until the tab closes
   *  would make a stray tab left open on the Dashboard look like a second till. */
  stop(): void {
    this.started = false;
    this.generation += 1;
    window.removeEventListener("online", this.onOnline);
    window.removeEventListener("offline", this.onOffline);
    for (const timer of this.timers) clearInterval(timer);
    this.timers = [];
    if (this.retry) clearTimeout(this.retry);
    this.retry = null;
    this.lock.release();
  }

  private onOnline = (): void => {
    // Both directions, immediately: prices first so nothing is billed off a stale
    // copy, and the queue behind it because that is money waiting.
    void this.syncNow();
  };

  private onOffline = (): void => {
    void this.attempt(() => this.refresh());
  };

  // -- the work --------------------------------------------------------------

  /**
   * Down then up, in that order.
   *
   * Every step is independent and none of them throws. That is not tidiness: the
   * queue is money and the dataset is reference data, so a failed price pull must
   * never be a reason not to send a bill that is already paid for. Separate
   * attempts, worst case a reason from each, and the light tells the story.
   *
   * The register is read first because its whole job is to stop the till issuing
   * a number the server already holds, and that has to be settled before another
   * bill can be numbered. `push` reads it again if it landed anything - see there.
   */
  async syncNow(options: { bootstrapIfNewDay?: boolean } = {}): Promise<void> {
    this.publish({ busy: true, lastError: "" });
    const failures = [
      options.bootstrapIfNewDay ? await this.attempt(() => this.bootstrapIfNewDay()) : "",
      await this.attempt(() => reconcileRegister(this.db, this.transport)),
      await this.pullDataset(),
      await this.push(),
      // Ticket 15: consent answers waiting on this device. Their own attempt,
      // after the bills: an answer is never a reason a bill did not move.
      await this.attempt(() => drainConsents(this.db, this.transport)),
      // Last, and its own attempt: a mirror of what is parked is the least
      // important thing on this list, and it must never be the reason a bill or
      // a price list did not move.
      await this.attempt(() => pushHeld(this.db, this.transport)),
      await this.attempt(() => this.refresh()),
    ].filter(Boolean);
    this.publish({ busy: false, lastError: failures[0] ?? "" });
  }

  /** Clear the halt and offer the refused bill again. Called from a button with
   *  no `await` behind it, so it reports rather than throws. */
  async retryHalted(): Promise<void> {
    const failed = await this.attempt(() => clearHalt(this.db));
    if (failed) {
      this.publish({ lastError: failed });
      return;
    }
    await this.pushAndRefresh();
  }

  /** Number a bill, queue it and try to send it - the Save & Print path.
   *
   *  The commit is awaited and the send is not, deliberately: the bill is final
   *  the moment it is in the queue (grill Q2), and the counter must be ready for
   *  the next customer whether or not there is a network. */
  /** `originalBuyerGstin` is the buyer GSTIN of the bill an exchange is taken
   *  against, as the screen found it. The screen may have found that bill at
   *  head office rather than on this device, so the engine cannot always look
   *  it up itself (ticket 05). */
  async commit(
    draft: BillDraft,
    context: { originalBuyerGstin?: string } = {},
  ): Promise<QueuedBill> {
    this.refuseIfBlocked();
    await this.refuseIfOnlineOnly(draft, context.originalBuyerGstin ?? "");
    await this.refuseIfSplitOff(draft);
    if ((await readMeta(this.db, META.sellingMode, "historical")) === "online_alpha") {
      if (!navigator.onLine) throw new Error("Connect to the network before issuing a bill.");
      const pending = await prepareOnline(this.db, this.storeCode, draft);
      try {
        const bill = await submitOnline(this.db, this.transport, pending);
        // Accepted stock is authoritative; a failed refresh cannot undo issue.
        const refreshError = await this.pullDataset();
        if (refreshError) this.publish({ lastError: refreshError });
        await this.refresh();
        return bill;
      } catch (error) {
        this.closeOnAccessDenial(error);
        throw error;
      } finally {
        await this.refresh();
      }
    }
    const bill = await commitBill(this.db, this.storeCode, draft);
    await this.refresh();
    void this.pushAndRefresh();
    return bill;
  }

  /**
   * Key a printed bill from the old machine back in under its own number (#189).
   *
   * Goes through the same queue, the same shelf move and the same sync as any
   * other bill - it *is* an ordinary bill, only an old one - and differs in the
   * two ways `reenterPaperBill` enforces: it takes a number the counter has
   * already passed, and it takes it once.
   *
   * The record of what has been keyed in is written inside that same
   * transaction, so a screen's tick and the guard against a second entry are one
   * fact rather than two that can disagree.
   */
  async reenterFromPaper(draft: BillDraft, seq: number): Promise<QueuedBill> {
    this.refuseIfBlocked();
    if ((await readMeta(this.db, META.sellingMode, "historical")) === "online_alpha") {
      throw new Error(
        "This online store cannot issue or upload a new paper bill. Owner or Admin must review any historical recovery.",
      );
    }
    const bill = await reenterPaperBill(this.db, this.storeCode, draft, seq);
    await this.refresh();
    void this.pushAndRefresh();
    return bill;
  }

  /** The one gate in front of both commit paths.
   *
   *  The screens disable their buttons on the same string, so this is belt to
   *  their braces - but it is the belt that matters: a counter that does not know
   *  its own bill number must not be able to reach the commit through a stale
   *  render, a second tab left open, or anything else that got past the UI. */
  private refuseIfBlocked(): void {
    if (this.snapshot.blocked) throw new Error(this.snapshot.blocked);
  }

  /** Ticket 05: work that needs head office is refused on an offline counter,
   *  before the bill is made. The screen disables Save & Print on the same
   *  words; this is the belt for a line that dropped between the render and the
   *  press. Asked of the network *now*, not of the last refresh. A paper
   *  re-entry is not asked: that bill was made long ago, and refusing to record
   *  it would only lose it. */
  /** Ticket 08: off, the till makes no split bills. The screen refuses on the
   *  same words; this is the belt for a switch that went off between the render
   *  and the press. A paper re-entry is not asked, for the reason above. */
  private async refuseIfSplitOff(draft: BillDraft): Promise<void> {
    const allowed = (await readMeta<unknown>(this.db, META.splitSale, null)) === true;
    const refusal = whySplitIsOff(
      draft.lines.map((line) => ({
        line_no: line.line_no,
        split: line.shares?.length ? line.shares : null,
      })),
      allowed,
    );
    if (refusal) throw new Error(refusal);
  }

  private async refuseIfOnlineOnly(draft: BillDraft, originalBuyerGstin: string): Promise<void> {
    const original = draft.exchange?.original;
    const found =
      original && !originalBuyerGstin
        ? await findHeldBill(this.db, original.fy, original.till_seq)
        : null;
    const refusal = whyOfflineRefuses({
      online: navigator.onLine,
      refusalsOn: refusalsOnFrom(await readMeta<unknown>(this.db, META.onlineOnly, null)),
      buyerGstin: draft.customer?.gstin ?? "",
      originalBuyerGstin: originalBuyerGstin || found?.buyer_gstin || "",
      tenderModes: draft.tenders.map((tender) => tender.mode),
      reservation: Boolean(draft.reservation),
      specialOrder: Boolean(draft.special_order),
    });
    if (refusal) throw new Error(refusal);
  }

  /**
   * Recover a counter whose local data the browser threw away (#189).
   *
   * Deliberately a button rather than something the boot sync does on its own.
   * What has happened is that this device's bill counter went back to 1 while the
   * store's series carried on without it, and the fix - taking the whole dataset
   * again and asking the server how far it has got - moves the number the next
   * customer's bill will carry. That is not a thing to do silently behind
   * somebody's back (AC: "no silent counter reset"): a person asks for it, and is
   * told what the counter now stands at.
   *
   * Both halves must land before the block lifts. A bootstrap without the
   * register leaves the till pricing correctly and numbering from 1; the register
   * without a bootstrap leaves it numbering correctly with no price list. Neither
   * on its own is a counter.
   */
  async recover(): Promise<void> {
    this.publish({ busy: true, lastError: "" });
    try {
      await forceBootstrap(this.db);
      await syncDown(this.db, this.transport);
      this.liveAccessVerified = true;
      await reconcileRegister(this.db, this.transport);
      this.storageLost = false;
      markTillSeen(this.storageScope);
      await writeMeta(this.db, META.bootstrapDay, new Date().toISOString().slice(0, 10));
      await this.refresh();
    } finally {
      this.publish({ busy: false });
    }
  }

  /**
   * Move this store's counter onto this machine (#189, grill Q1).
   *
   * A manager's act, so it is not routed through `attempt` like the sync loop is:
   * a handover that quietly failed would leave somebody believing the counter had
   * moved when it had not, and the next bill would take a number the old machine
   * has already printed. The refusal goes back to the caller and onto the screen.
   *
   * Moving the counter is left to `reconcileRegister`, which is the only thing in
   * this layer that writes it from outside a commit - and which will not write it
   * at all when the server's financial year and the till's disagree. Doing it
   * again here from the handover's own `resume_from_seq` would step straight past
   * that guard: a shop-floor clock a day out on 1 April would take a counter
   * standing at 25-26 bill 305 and set it to 26-27 bill 1, and every bill after
   * that would collide with one already posted.
   */
  async handOver(reason: string): Promise<HandoverState> {
    this.publish({ busy: true, lastError: "" });
    try {
      const answer = await this.transport.handover(reason);
      await reconcileRegister(this.db, this.transport);
      const state: HandoverState = { ...answer, at: new Date().toISOString() };
      await writeMeta(this.db, META.handover, state);
      await this.refresh();
      return state;
    } finally {
      this.publish({ busy: false });
    }
  }

  /**
   * Ask head office for another 24 hours of offline billing (OPS-09, PRD §10.1).
   *
   * A person's button as well as the tail of every sync, and not routed through
   * `attempt` when it is pressed: somebody watching the Till & Sync panel asked a
   * question, and "nothing happened" is not an answer. The refusal goes back to
   * the screen.
   *
   * It needs a line by construction - that is what the window is evidence of - so
   * offline this fails the way every other call does, with the counter's own
   * "no connection to head office".
   */
  async renew(): Promise<void> {
    this.publish({ busy: true, lastError: "" });
    try {
      const renewed = await renewAuthority(this.db, this.transport);
      if (!renewed) throw new Error("Head office did not renew this counter's billing window.");
      await this.refresh();
    } finally {
      this.publish({ busy: false });
    }
  }

  /**
   * Pause billing and let the store's stock go, so a transfer out of the store
   * can be approved (change PRD §10.2, Anand, 25 September 2026).
   *
   * Syncs first, so every bill is up. Then the pause is written here - from that
   * instant nothing is finalised - and only then is head office asked to
   * release. A refusal takes the pause back off, because nothing was released;
   * no answer leaves it on, because something may have been. Pressing it again
   * on a counter left `pausing` asks for the release again.
   */
  async pauseForTransfer(reason: string): Promise<void> {
    if (!reason.trim()) throw new Error("Say why billing is being paused.");
    if (this.snapshot.storageLost || !this.snapshot.lockHeld)
      throw new Error(this.snapshot.blocked);
    this.publish({ busy: true, lastError: "" });
    try {
      const drained = await drainQueue(this.db, this.transport);
      if (drained.accepted) await reconcileRegister(this.db, this.transport);
      await syncDown(this.db, this.transport);
      const existing = await readPause(this.db);
      // The sync above may have learnt that an earlier request did land.
      if (existing?.stage === "paused") return;
      if (existing?.stage === "resuming") throw new Error("This counter is resuming.");
      const pause = existing ?? (await beginPause(this.db, reason.trim()));
      const version = await readMeta<number | null>(this.db, META.allocation, null);
      if (version === null) {
        await movePause(this.db, "pausing", null);
        throw new Error("This counter is not holding any stock to release.");
      }
      try {
        await this.transport.releaseForTransfer(version, pause.reason, pause.fy, pause.nextSeq);
      } catch (error) {
        if (isRefusal(error)) {
          // A refusal may be for a request whose first answer was lost and which
          // landed after all. Head office's word on the pause decides, not ours.
          const said = await this.transport.till().catch(() => null);
          if (said?.paused) {
            await movePause(this.db, "pausing", "paused");
            await writeMeta(this.db, META.allocation, null);
            return;
          }
          if (said === null) {
            throw new Error(
              "Head office refused, and could not say whether this counter is paused. Billing stays paused; press the button again, or Resume.",
            );
          }
          await movePause(this.db, "pausing", null);
        } else {
          throw new Error(
            "No answer from head office. Billing stays paused; press the button again, or Resume.",
          );
        }
        throw error;
      }
      await movePause(this.db, "pausing", "paused");
      await writeMeta(this.db, META.allocation, null);
    } finally {
      await this.refresh();
      this.publish({ busy: false });
    }
  }

  /**
   * End the pause, take a full fresh copy of the shelf, then bill again.
   *
   * Two steps, and billing stays shut until both have landed: head office ends
   * its pause, then a full dataset arrives - which is also the sync that protects
   * the new shelf - and `applyDataset` lifts the local pause in the same
   * transaction that writes it. If the copy does not arrive the counter stays
   * paused, and pressing Resume again finishes the job.
   */
  async resume(): Promise<void> {
    const pause = await readPause(this.db);
    if (!pause) return;
    this.publish({ busy: true, lastError: "" });
    try {
      const at = await positionNow(this.db);
      await this.transport.resumeTill(at.fy, at.nextSeq);
      await this.db.meta.put({ key: META.pause, value: { ...pause, stage: "resuming" } });
      await forceBootstrap(this.db);
      await syncDown(this.db, this.transport);
      if (await readPause(this.db)) {
        throw new Error("The fresh stock copy did not finish. Billing stays paused; try again.");
      }
    } finally {
      await this.refresh();
      this.publish({ busy: false });
    }
  }

  /**
   * Make this device the store's registered counter (OPS-09, PRD §10.1).
   *
   * A manager's act at `sell: approve`, like the handover, and the same shape: it
   * is not routed through `attempt`, because a registration that quietly failed
   * would leave somebody believing this machine owned the series when it did not.
   *
   * `replace` is passed through rather than inferred. A store that already has a
   * counter answers `TILL_TAKEN`, and what the person then has to decide is
   * whether this is a second machine (which must not happen) or a replacement for
   * one that is not coming back (which needs a reason and a fresh counter id).
   */
  async registerTill(options: { replace: boolean; reason: string }): Promise<void> {
    this.publish({ busy: true, lastError: "" });
    try {
      const identity = await this.transport.registerTill(options);
      await writeMeta(this.db, META.till, deviceStateFrom(identity));
      if (identity.device_token) await writeMeta(this.db, META.deviceToken, identity.device_token);
      // A brand-new counter starts with no window at all, so the registration is
      // followed straight away by the renewal that opens one. Two calls rather
      // than one because they answer two different questions - "which device is
      // this" and "is it talking to us now" - and only the second is the one the
      // window is evidence of.
      await renewAuthority(this.db, this.transport);
      // The new device resumes where the store's series actually stands, not at 1.
      await reconcileRegister(this.db, this.transport);
      await this.refresh();
    } finally {
      this.publish({ busy: false });
    }
  }

  /** Put this counter's paper re-entry list away - the store is finished with it.
   *
   *  Only the list. What has actually been keyed in stays where `numbering.ts`
   *  wrote it, because that is what stops a receipt going in twice and it must
   *  not be clearable from a screen. */
  async clearHandover(): Promise<void> {
    await writeMeta(this.db, META.handover, null);
    await this.refresh();
  }

  // -- holds (#185, grill Q13) -----------------------------------------------
  //
  // Three one-line methods over `held.ts`, all ending the same way: write, then
  // `heldChanged`. Parking a bill is a thing a cashier does with a customer
  // waiting, so it may not depend on a network - and a hold is not money, so
  // nothing is lost if the mirror is a minute stale.

  /** Park the cart. */
  async hold(hold: { held_uuid: string; label: string; payload: HeldPayload }): Promise<void> {
    await parkHold(this.db, hold);
    await this.heldChanged();
  }

  /** Take a hold off the list - resumed into a bill, or let go at day close.
   *
   *  One method for both because they are one act at this layer: the difference
   *  is entirely what the *screen* does with the cart, and a second method that
   *  did the same delete would be two names for one row disappearing. */
  async releaseHold(heldUuid: string): Promise<void> {
    await dropHold(this.db, heldUuid);
    await this.heldChanged();
  }

  /** The store's day-close answer: this cart carries to tomorrow. */
  async keepHold(heldUuid: string): Promise<void> {
    await keepHold(this.db, heldUuid);
    await this.heldChanged();
  }

  /** Show the new list, and offer it to head office without waiting for the
   *  answer: the screen is what the cashier is looking at, and the mirror is a
   *  count on somebody else's. */
  private async heldChanged(): Promise<void> {
    await this.refresh();
    void this.attempt(() => pushHeld(this.db, this.transport));
  }

  // -- customer consent (ticket 15) ---------------------------------------------

  /** Keep a consent answer on this device and send it at once if the line
   *  allows. Written before anything is sent, so a withdrawal takes effect on
   *  this counter the moment it is recorded, online or not. */
  async recordConsent(answer: QueuedConsent): Promise<void> {
    await queueConsent(this.db, answer);
    await this.refresh();
    void this.sendConsents();
  }

  /** Offer the waiting consent answers now, and show what is left. */
  async sendConsents(): Promise<void> {
    await this.attempt(() => drainConsents(this.db, this.transport));
    await this.attempt(() => this.refresh());
  }

  /**
   * What stands for a number: head office's answer when the counter can ask,
   * with this device's unsent answers laid over it. `known` is false when the
   * counter could not ask head office (offline, or it did not answer) - the
   * state is then only what this device itself has recorded.
   */
  async consentStanding(mobile: string): Promise<{ state: ConsentState; known: boolean }> {
    let server: ConsentState | null = null;
    if (navigator.onLine && this.transport.consentState) {
      try {
        server = await this.transport.consentState(mobile);
      } catch {
        server = null;
      }
    }
    const local = await unsentFor(this.db, mobile);
    return { state: standing(server ?? NOT_ASKED, local, mobile), known: server !== null };
  }

  /** Remember who sold the last piece, so the next line defaults to them.
   *
   *  In the database rather than in a component's state because the default has
   *  to survive a reload and a walk to another screen: the point of it is that a
   *  busy counter accepts the salesperson with no keystrokes at all (D10 §4), and a
   *  default that resets whenever React remounts is a popup again. */
  async rememberSalesperson(salespersonId: string): Promise<void> {
    await writeMeta(this.db, META.lastSalesperson, salespersonId);
  }

  /** Turn the scan tones off, or back on (#247, grill Q8).
   *
   *  Written to the counter's own database for the same reason the salesperson
   *  default is: it is a fact about this machine on this shop floor, so it has
   *  to survive a reload and be true for whoever is standing at it next - not a
   *  preference that resets every morning and has to be found again. */
  async setMuted(muted: boolean): Promise<void> {
    await writeMeta(this.db, META.muted, muted);
    await this.refresh();
  }

  private async bootstrapIfNewDay(): Promise<void> {
    const today = new Date().toISOString().slice(0, 10);
    if ((await readMeta(this.db, META.bootstrapDay, "")) === today) return;
    await forceBootstrap(this.db);
    await writeMeta(this.db, META.bootstrapDay, today);
  }

  /** Pull the dataset. Answers the reason it could not, rather than throwing. */
  private async pullDataset(): Promise<string> {
    return this.attempt(async () => {
      await syncDown(this.db, this.transport);
      this.liveAccessVerified = true;
      // A dataset that landed is proof the database is alive, and the marker is
      // what a later session compares against to notice it was thrown away.
      //
      // It does **not** clear a loss already noticed: that takes `recover`, on
      // purpose. A sync lifting the block on its own would move the counter
      // behind somebody's back, and the whole point of the red light is that a
      // person is told which number this till has jumped to.
      if (!this.storageLost) markTillSeen(this.storageScope);
    });
  }

  /**
   * Offer the queue, and schedule the next attempt if the line was down.
   *
   * Re-reads the register whenever the drain landed anything. What the server
   * had accepted was true before those bills went up, and a screen saying "head
   * office has bill 7" beside "nothing waiting", to a counter that has billed 9,
   * is two panels telling a store person different stories. One extra GET per
   * successful drain, and none at all on the retries that find nothing.
   */
  private async push(): Promise<string> {
    return this.attempt(async () => {
      const result = await drainQueue(this.db, this.transport);
      this.publish({ lastFlags: result.flags });
      if (this.retry) clearTimeout(this.retry);
      this.retry = result.retryAfterMs
        ? setTimeout(() => void this.pushAndRefresh(), result.retryAfterMs)
        : null;
      if (result.accepted) await reconcileRegister(this.db, this.transport);
    });
  }

  // Both of these are fired from a timer with `void`, so nothing is waiting to
  // catch them: an unhandled rejection here is a store's browser logging an
  // error nobody reads and a light that stops moving. Every step, the refresh
  // included, goes through `attempt`.
  private async pushAndRefresh(): Promise<void> {
    const failed = await this.push();
    await this.attempt(() => drainConsents(this.db, this.transport));
    // The refresh runs whether or not the push worked - a failed attempt has an
    // attempt count and a reason on it, and those are what the screen is for.
    const stale = await this.attempt(() => this.refresh());
    this.publish({ lastError: failed || stale });
  }

  private async pullDatasetAndRefresh(): Promise<void> {
    // Not over the top of a pause, a resume or a person's own sync: an answer
    // read before one of those lands after it and says something stale.
    if (this.snapshot.busy) return;
    const failed = await this.pullDataset();
    const stale = await this.attempt(() => this.refresh());
    this.publish({ lastError: failed || stale });
  }

  private async attempt(work: () => Promise<unknown>): Promise<string> {
    try {
      await work();
      return "";
    } catch (error) {
      this.closeOnAccessDenial(error);
      return messageOf(error);
    }
  }

  private closeOnAccessDenial(error: unknown): void {
    if (
      error instanceof TillHttpError &&
      (error.status === 401 ||
        (error.status === 403 &&
          [
            "AUTH_REQUIRED",
            "FIELD_DENIED",
            "ACTION_DENIED",
            "SCOPE_DENIED",
            "TILL_SCOPE",
            "HTTP_403",
          ].includes(error.code)))
    ) {
      this.liveAccessVerified = false;
      this.publish({ liveAccessVerified: false, lastError: messageOf(error) });
    }
  }

  async pairDevice(token: string): Promise<void> {
    if (!/^[a-f0-9]{32,64}$/.test(token.trim()))
      throw new Error("Enter the pairing code supplied by your administrator.");
    if (!this.transport.pairTill) throw new Error("Counter pairing is unavailable.");
    await this.transport.pairTill(token.trim());
    await writeMeta(this.db, META.deviceToken, token.trim());
    await this.refresh();
  }

  async retryOnline(): Promise<QueuedBill> {
    const pending = await unfinishedSubmission(this.db);
    if (!pending) throw new Error("No unresolved submission remains.");
    try {
      const bill = await submitOnline(this.db, this.transport, pending);
      const refreshError = await this.pullDataset();
      if (refreshError) this.publish({ lastError: refreshError });
      return bill;
    } catch (error) {
      this.closeOnAccessDenial(error);
      throw error;
    } finally {
      await this.refresh();
    }
  }

  async reviseOnline(): Promise<void> {
    await reviseRejected(this.db);
    const refusal = await this.pullDataset();
    await this.refresh();
    if (refusal) throw new Error(refusal);
  }

  /** Re-read everything a screen shows from the local database. Cheap - these
   *  are counts and one small table - and it is the only writer of the snapshot's
   *  facts, so nothing on screen can drift from what is on disk. */
  async refresh(): Promise<void> {
    const [items, stock, offers, salespeople, managers, gstSlabs, customers, heldBills] =
      await Promise.all([
        this.db.items.count(),
        this.db.stock.count(),
        this.db.offers.count(),
        this.db.salespeople.count(),
        this.db.managers.count(),
        this.db.gstSlabs.count(),
        this.db.customers.count(),
        listHolds(this.db),
      ]);
    const held = heldBills.length;
    const queue = await this.db.queue.orderBy("id").toArray();
    const halt = await readMeta<QueueHalt | null>(this.db, META.halt, null);
    const register = await readMeta<RegisterPayload | null>(this.db, META.register, null);
    const syncedAt = await readMeta<string | null>(this.db, META.syncedAt, null);
    const handover = await readMeta<HandoverState | null>(this.db, META.handover, null);
    const muted = await readMeta<boolean>(this.db, META.muted, false);
    const device = await readDevice(this.db);
    const pause = await readPause(this.db);
    const allocationVersion = await readMeta<number | null>(this.db, META.allocation, null);
    const taxSettings = await readMeta<TillTaxSettings | null>(this.db, META.taxSettings, null);
    const numbering = await readMeta<TillNumbering | null>(this.db, META.numbering, null);
    const onlineOnlyRefusals = refusalsOnFrom(
      await readMeta<unknown>(this.db, META.onlineOnly, null),
    );
    const managerPinRules = (await readMeta<unknown>(this.db, META.managerPinRules, null)) === true;
    const splitSale = (await readMeta<unknown>(this.db, META.splitSale, null)) === true;
    const customerDisplay = (await readMeta<unknown>(this.db, META.customerDisplay, null)) === true;
    const customerConsent = (await readMeta<unknown>(this.db, META.customerConsent, null)) === true;
    const customerReservation =
      (await readMeta<unknown>(this.db, META.customerReservation, null)) === true;
    const savedSizes = (await readMeta<unknown>(this.db, META.savedSizes, null)) === true;
    const specialOrders = (await readMeta<unknown>(this.db, META.specialOrders, null)) === true;
    const giftVouchers = (await readMeta<unknown>(this.db, META.giftVouchers, null)) === true;
    const alterationCharge = alterationChargeFrom(
      await readMeta<unknown>(this.db, META.alterationCharge, null),
    );
    const consentWording = await readMeta<TillConsentWording>(
      this.db,
      META.consentWording,
      FIRST_WORDING,
    );
    const consentRows = await this.db.consents.toArray();
    const consentsRefused = consentRows.filter((row) => row.refused).length;
    const consentsPending = consentRows.length - consentsRefused;
    // Read every refresh rather than cached on the engine: the window closes
    // while nobody is touching the screen, and a counter that only learned it had
    // expired when somebody pressed a button would print one more bill first.
    const authority = authorityState(device?.anchor ?? null);
    const paperEntered = await paperEntries(this.db, financialYear());
    const nextNumber = await previewNextNumber(this.db, this.storeCode);
    const online = navigator.onLine;
    const onlineAlpha =
      this.initialOnlineAlpha ||
      (await readMeta(this.db, META.sellingMode, "historical")) === "online_alpha";
    const onlinePending = (await unfinishedSubmission(this.db)) ?? null;
    const devicePaired = Boolean(await readMeta(this.db, META.deviceToken, ""));
    const datasetReady = Boolean(syncedAt);
    // Re-read rather than remembered: a second tab closing frees the counter, and
    // the ask is cheap. `acquire` is a no-op once this tab holds it.
    const lockHeld = this.lock.held() || (await this.lock.acquire());

    this.publish({
      ready: true,
      counts: { items, stock, offers, salespeople, managers, gstSlabs, customers, held },
      held: heldBills,
      queue,
      pending: queue.length,
      halt,
      register,
      handover,
      paperEntered,
      device,
      authority,
      muted,
      pause,
      allocationVersion,
      taxSettings,
      numbering,
      onlineOnlyRefusals,
      managerPinRules,
      splitSale,
      customerDisplay,
      customerConsent,
      customerReservation,
      savedSizes,
      specialOrders,
      giftVouchers,
      alterationCharge,
      consentWording,
      consentsPending,
      consentsRefused,
      syncedAt,
      nextNumber,
      online,
      onlineAlpha,
      liveAccessVerified: this.liveAccessVerified,
      onlinePending,
      devicePaired,
      datasetReady,
      storageLost: this.storageLost,
      lockHeld,
      blocked: billingBlock({
        storageLost: this.storageLost,
        lockHeld,
        authorityExpired: authority.expired,
        paused: pause !== null,
      }),
      status: deriveStatus({
        datasetReady,
        pending: queue.length,
        halt,
        register,
        storageLost: this.storageLost,
        lockHeld,
        authorityExpired: authority.expired,
        online,
      }),
    });
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
