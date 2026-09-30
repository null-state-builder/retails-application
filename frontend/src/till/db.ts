// The counter's own copy of the world (#180, D10 step 3).
//
// This is the till's source of truth, not a cache in front of one. A bill is
// final the moment Save & Print writes it here (grill Q2); the server hears about
// it afterwards, possibly days afterwards, and its only job is to accept what was
// already printed. Everything downstream of that follows:
//
//   · **One database per store.** The name carries the store code, so a device
//     that is signed into DEO can never bill against GAY's counter or its stock -
//     and a store login change opens a different database
//     rather than mixing two shops' rows in one.
//   · **The queue is a table, not a variable.** It has to survive a reload, a
//     crash and a flat battery, because what is in it is money that has already
//     changed hands.
//   · **`meta` holds the counter.** `nextSeq` is the till's half of the bill
//     series, and the whole reason `numbering.ts` exists is that it may only ever
//     be read and advanced inside the transaction that queues the bill.
//
// Everything here is quantity, price and identity. No cost, ever (H2): the
// dataset feed does not send it, and nothing in this module invents it.

import Dexie from "dexie";
import type { Table } from "dexie";

import type { QueuedConsent } from "./consent";
import type { DraftPayload } from "./draft";
import type { HeldPayload } from "./held";
import type {
  CachedBill,
  QueuedBill,
  TillGstSlab,
  TillItem,
  TillKnownCustomer,
  TillManager,
  TillOffer,
  TillSalesperson,
  TillSeason,
  TillStock,
} from "./types";

/** A bill the counter parked to serve the next customer. Mirrored to the server
 *  best-effort so the Dashboard can count them; the till stays authoritative.
 *
 *  `payload` is typed rather than left as loose JSON, even though the *server*
 *  keeps it opaque: the till writes it and the till reads it back, so a cart is
 *  the only thing it can hold, and calling it `Record<string, unknown>` here
 *  would buy a cast at every one of those reads. The import is type-only, so
 *  nothing circular survives the compiler. */
export interface HeldBill {
  held_uuid: string;
  label: string;
  held_at: string;
  /** `today` until the store says otherwise at day close; `kept` once it has. */
  expires_policy: "today" | "kept";
  payload: HeldPayload;
  /** The local day the store last answered "keep this" - and the one field here
   *  that is **not** mirrored up.
   *
   *  A hold parked before today is put to the store at day close, and keeping it
   *  has to be an answer about *that* close rather than for ever: a hold kept on
   *  Monday and still parked on Thursday is a cart nobody has thought about in
   *  three days, and a policy of `kept` alone would hide it for good. The server
   *  has no use for the answer - the mirror is a count on a Dashboard - and the
   *  contract's five columns are what `PUT /api/sell/held-bills` takes, so this
   *  stays where the decision was made. */
  reviewed_on?: string;
}

/** One row of `meta` - the till's small pile of state, keyed by name so a new
 *  fact does not need a schema version. */
export interface MetaRow {
  key: string;
  value: unknown;
}

/** The keys `meta` holds, spelled once. */
export const META = {
  /** The dataset cursor to ask the next delta from. */
  cursor: "cursor",
  sellingMode: "sellingMode",
  commercialRevision: "commercialRevision",
  deviceToken: "deviceToken",
  /** The tax settings versions and this store's switch (ticket 03). Held here
   *  so a counter with no network taxes by the version it last received. */
  taxSettings: "taxSettings",
  /** The new invoice series (ticket 04): the start date, this store's prefix and
   *  the number blocks the counter holds, with how far into each it is - see
   *  `invoiceNumbers.ts`. Held so an offline counter numbers bills by itself. */
  numbering: "numbering",
  /** Whether this store refuses online-only work while offline (ticket 05). A
   *  boolean; absent means never heard, which is the default: on (B4). */
  onlineOnly: "onlineOnly",
  /** Whether an override needs a manager other than the cashier (ticket 06). A
   *  boolean; absent means never heard, which is the default: off (B3). */
  managerPinRules: "managerPinRules",
  /** Whether a line may be split between two salespeople (ticket 08). A
   *  boolean; absent means never heard, which is the default: off (B3). */
  splitSale: "splitSale",
  /** Whether this counter offers a customer display (ticket 09). A boolean;
   *  absent means never heard, which is the default: off (B3). */
  customerDisplay: "customerDisplay",
  /** Whether this counter asks for customer consent (ticket 15). A boolean;
   *  absent means never heard, which is the default: off (B3). */
  customerConsent: "customerConsent",
  /** Whether this counter offers "Reservation pickup" (ticket 20). A boolean;
   *  absent means never heard, which is the default: off (B3). */
  customerReservation: "customerReservation",
  /** Whether this counter shows the customer's saved sizes (ticket 18). A
   *  boolean; absent means never heard, which is the default: off (B3). */
  savedSizes: "savedSizes",
  /** Whether this counter offers special order collection (ticket 21). A
   *  boolean; absent means never heard, which is the default: off (B3). */
  specialOrders: "specialOrders",
  /** Whether this counter sells and takes gift vouchers (ticket 19). A boolean;
   *  absent means never heard, which is the default: off (B3). */
  giftVouchers: "giftVouchers",
  /** The alteration charge line this counter makes, or null (ticket 22). Absent
   *  means never heard, which is the default: off (B3). */
  alterationCharge: "alterationCharge",
  /** The consent wording this counter asks with, and its version (ticket 15).
   *  Held so an offline counter asks, and records the version, by itself. */
  consentWording: "consentWording",
  /** The financial year the counter is numbering in. */
  fy: "fy",
  /** The number the next bill will take. */
  nextSeq: "nextSeq",
  /** The store this database belongs to, and its GSTIN. */
  store: "store",
  /** When the dataset last landed. */
  syncedAt: "syncedAt",
  /** The day the till last took a full bootstrap, ISO. */
  bootstrapDay: "bootstrapDay",
  /** The bill the server refused, and why - see `QueueHalt`. */
  halt: "halt",
  /** What the server last said it had accepted from this counter. */
  register: "register",
  /** The shop floor's money dials - see `TillPolicy`. */
  policy: "policy",
  /** The salesperson the counter picked last, defaulted onto the next line. */
  lastSalesperson: "lastSalesperson",
  /** This counter has turned the scan tones off (#247, grill Q8). Here rather
   *  than on the user, and here rather than in `policy`: it is a property of the
   *  machine standing in the shop - one counter is beside the music, another is
   *  in a back office - and head office has no business ruling on it. */
  muted: "muted",
  /** The register handover this machine took over on, and the bills the old one
   *  never sent - see `HandoverState`. A list somebody is working through, and
   *  they may put it away when they are done. */
  handover: "handover",
  /** This device's own identity as the store's counter (OPS-09) - see
   *  `TillIdentity`. The counter id, the series prefix its bill numbers carry,
   *  the end of its 24-hour authority and the server clock that window was
   *  measured from. Written by every successful dataset sync.
   *
   *  In `meta` rather than in `localStorage` beside the storage sentinel: unlike
   *  the sentinel, this is only meaningful while the counter's database is there.
   *  A device that lost its queue is not a licensed till holding a window, it is a
   *  machine that has to be recovered. */
  till: "till",
  /** What this device took offline with it, as the server issued it - see
   *  `TillAllocation`. The version its shelf was read at, so a screen can name
   *  the allocation a manager is being asked to release. */
  allocation: "allocation",
  /** The counter's transfer pause - see `TillPauseState`. Here rather than
   *  anywhere a reload could lose it: it is what stops a bill being finalised
   *  while the store's stock is promised to a transfer, and it has to hold
   *  through a restart and with no network. */
  pause: "pause",
  /** Which numbers this counter has keyed back in from a printed copy, for the
   *  year it is counting in - see `PaperEntered`.
   *
   *  Deliberately **not** part of the handover row above, and the separation is
   *  the whole point: the handover list is a job that gets put away, and this is
   *  a fact about numbers that have been spent. A re-entry also leaves the queue
   *  the moment the server takes it, so neither the queue nor the tick list on a
   *  screen can be what stops the same receipt going in twice. */
  paperEntered: "paperEntered",
} as const;

export interface PendingOnlineBill {
  idempotency_uuid: string;
  bill: QueuedBill;
  state: "pending" | "rejected" | "accepted" | "revised";
  error?: string;
}

export class TillDb extends Dexie {
  onlineSubmissions!: Table<PendingOnlineBill, string>;
  items!: Table<TillItem, [string, string]>;
  stock!: Table<TillStock, string>;
  offers!: Table<TillOffer, number>;
  /** The staff list's salespeople at this store (ticket 07). */
  salespeople!: Table<TillSalesperson, string>;
  seasons!: Table<TillSeason, string>;
  managers!: Table<TillManager, number>;
  gstSlabs!: Table<TillGstSlab, [string, string]>;
  meta!: Table<MetaRow, string>;
  queue!: Table<QueuedBill, number>;
  held!: Table<HeldBill, string>;
  /** The in-progress bill, autosaved (#244). One row, at the fixed key
   *  `DRAFT_KEY` - an outbound key (`""` in the schema below), so the object on
   *  disk is exactly the payload and nothing has to strip a key field back off
   *  it on read. */
  draft!: Table<DraftPayload, string>;
  /** The counter's phone book (#245) - everybody KDPS has billed, keyed by the
   *  mobile the accept boundary collapsed them to. All-KDPS rather than this
   *  store's, so a regular is recognised wherever they walk in, and searched
   *  offline because there is no lookup endpoint by design. */
  customers!: Table<TillKnownCustomer, string>;
  /** The store's own recent bills, so an exchange can be taken with no network
   *  (OPS-09, PRD §10.3-10.4). Keyed by `doc_number`, which is the one thing a
   *  customer's copy and head office agree on.
   *
   *  A cache and *not* a source of truth, unlike everything else in this file: the
   *  queue owns bills this counter printed and has not sent, and these are bills
   *  head office already holds. What the till writes back onto a row here is only
   *  what it has itself given back (`returned_qty`/`returned_paise`), so the same
   *  piece cannot be marked twice on this device before the queue drains. */
  bills!: Table<CachedBill, string>;
  /** Consent answers not yet at head office (ticket 15), keyed by the till's own
   *  id for each. Money-free, but not a cache: an answer given offline lives
   *  only here until it is sent, so it is kept like the bill queue. */
  consents!: Table<QueuedConsent, string>;

  constructor(storeCode: string) {
    super(databaseName(storeCode));
    this.version(1).stores({
      // A piece is a barcode *in a season*: the same barcode bought twice is two
      // lots at two ticket prices, and the counter has to be able to tell them
      // apart. `barcode` is indexed on its own because a withdrawal arrives as a
      // bare barcode and takes every season of that piece with it.
      items: "[barcode+season], barcode, brand",
      stock: "barcode",
      offers: "id",
      creditNotes: "number",
      salesmen: "id",
      managers: "user_id",
      // Slabs are replaced whole on every sync, so their key only has to be
      // stable enough to upsert on: one prefix has one rate from one date.
      gstSlabs: "[hsn_prefix+effective_from]",
      meta: "key",
      // `++id` is the FIFO. Dexie hands out ascending keys, so "the oldest bill
      // still unsent" is the first row in key order - the queue does not need a
      // sequence number of its own, and cannot disagree with one.
      queue: "++id, idempotency_uuid",
      held: "held_uuid",
    });
    // The season master's ordering, for resolving a scan that names no season
    // (#181). A version of its own rather than an edit to version 1: a counter
    // that has been billing since #180 has a database on disk with unsynced
    // money in it, and Dexie upgrades that one in place instead of asking the
    // browser to throw it away and start again.
    this.version(2).stores({ seasons: "code" });
    // The autosaved draft (#244) - additive again, for the same reason version
    // 2 was: a counter mid-shift has unsynced money in its database and Dexie
    // must upgrade that one in place. This version adds `draft` only; the
    // `customers` table design.md plans alongside it lands with #245 on the
    // next free version, not folded in here.
    this.version(3).stores({ draft: "" });
    // The synced customer list (#245). A version of its own rather than an edit
    // to version 3, and for the third time the reason is the same: a till that
    // has been billing since #244 has unsynced money on disk, and rewriting a
    // shipped version's schema is what makes Dexie throw that database away
    // instead of upgrading it. Only the new table is named - a `stores` call
    // lists what *changes*, so every table above is carried forward untouched.
    this.version(4).stores({ customers: "mobile" });
    // Credit notes no longer enter or leave at the counter. A new version is
    // required to remove the shipped cache without disturbing queued bills.
    this.version(5).stores({ creditNotes: null });
    // The cached original bills an offline exchange works against (OPS-09). A
    // version of its own for the fifth time, and for the fifth time the reason is
    // the same: a counter mid-shift has unsynced money on disk, and editing a
    // shipped version's schema is what makes Dexie throw that database away
    // instead of upgrading it in place. Only the new table is named.
    this.version(6).stores({ bills: "doc_number, till_seq, customer_mobile" });
    // Ticket 07: the salesperson picker reads the staff list, whose ids are the
    // staff records' UUIDs. A new table rather than new rows in the old one, and
    // a version of its own for the same reason as every version above; the old
    // table (numeric ids of the retired salesman list) is removed.
    this.version(7).stores({ salespeople: "id", salesmen: null });
    // Ticket 15: consent answers waiting to reach head office. A version of its
    // own for the same reason as every version above; only the new table.
    this.version(8).stores({ consents: "id, mobile" });
    this.version(9).stores({ onlineSubmissions: "idempotency_uuid, state" });
  }
}

/** The IndexedDB name for a store's till. */
export function databaseName(storeCode: string): string {
  return `kdps-till-${storeCode}`;
}

/** Locate an old label-only database without opening or reading its contents.
 * Its tenant/device ownership cannot be inferred, so automatic delivery must
 * leave it preserved for an authorised reconciliation. */
export async function hasUnscopedDatabase(storeCode: string): Promise<boolean> {
  if (typeof indexedDB === "undefined" || typeof indexedDB.databases !== "function") return false;
  try {
    const databases = await indexedDB.databases();
    return databases.some((entry) => entry.name === databaseName(storeCode));
  } catch {
    return false;
  }
}

// One database object per store for the life of the tab. Dexie connections are
// expensive to open and a second one to the same name blocks the first during a
// version change, so a screen asks for the till rather than constructing one.
const open = new Map<string, TillDb>();

export function tillDb(storeCode: string): TillDb {
  let db = open.get(storeCode);
  if (!db) {
    db = new TillDb(storeCode);
    open.set(storeCode, db);
  }
  return db;
}

/** Forget the cached connection (tests, and a sign-out that changes store). */
export function closeTillDb(storeCode: string): void {
  open.get(storeCode)?.close();
  open.delete(storeCode);
}

export async function readMeta<T>(db: TillDb, key: string, fallback: T): Promise<T> {
  const row = await db.meta.get(key);
  return row === undefined ? fallback : (row.value as T);
}

export async function writeMeta(db: TillDb, key: string, value: unknown): Promise<void> {
  await db.meta.put({ key, value });
}
