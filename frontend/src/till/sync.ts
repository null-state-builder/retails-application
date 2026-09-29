// The two directions the counter talks in (#180, D10 step 3).
//
// **Down** is reference data - what may be sold, at what ticket price, at what
// tax and by whom. Losing a row here means selling at
// yesterday's price, which the daily check catches.
//
// **Up** is money. Every row in the queue is a bill that is already printed and
// already paid for, so the only two acceptable outcomes are "the server took it"
// and "a named human is looking at it". There is no third: nothing is dropped,
// nothing is skipped, and the queue does not reorder itself to get past an
// awkward bill.
//
// The asymmetry between the two failure modes is the whole design. A network
// failure says nothing about the bill, so it is retried for ever. A 4xx says the
// server has *decided* about this bill, and asking again will fetch the same
// decision - so the queue halts, names the bill and stops, and the counter keeps
// billing on the next number while somebody sorts it out (contract, step 3;
// design.md, "Error handling").

import { financialYear } from "../lib/fiscal";

import { META, readMeta, writeMeta } from "./db";
import type { TillDb } from "./db";
import { mirrorRow } from "./held";
import { refreshNumbering } from "./invoiceNumbers";
import { TillHttpError } from "./transport";
import type { TillTransport } from "./transport";
import { fastForwardTo } from "./numbering";
import { readPause, settlePause } from "./pause";
import { anchorFrom } from "./authority";
import type { AuthorityAnchor } from "./authority";
import type {
  CachedBill,
  DatasetPayload,
  QueueHalt,
  RegisterPayload,
  TillIdentity,
  TillPolicy,
} from "./types";

/** What the counter assumes before a server has told it otherwise: no keyed-in
 *  discount at all. The strict end of the dial, because a default that guessed
 *  generously would be a cap this file invented. */
export const DEFAULT_POLICY: TillPolicy = {
  manual_discount_cap_percent: "0.00",
  manual_discount_on_offer_lines: false,
  // An older server cannot establish that yesterday's bill is still in-window.
  // Zero fails closed: anything from an earlier local date asks a manager.
  return_window_days: 0,
  // How many days of bills the till holds for an offline exchange. Thirty is the
  // server's own default; the dial is head office's and rides down with the rest.
  cached_bill_days: 30,
};

/** Slowest a failing queue will retry, and the plain interval it drains on
 *  anyway. A minute is the contract's number: fast enough that a shop with a
 *  flaky line is never far behind, slow enough not to hammer a server that is
 *  down. */
export const MAX_BACKOFF_MS = 60_000;
const FIRST_BACKOFF_MS = 5_000;

/** How long to wait before offering a bill that failed `attempts` times again.
 *  Exponential from five seconds, capped at a minute. */
export function backoffMs(attempts: number): number {
  if (attempts < 1) return 0;
  return Math.min(MAX_BACKOFF_MS, FIRST_BACKOFF_MS * 2 ** (attempts - 1));
}

// ---------------------------------------------------------------- down -------

/**
 * Write a dataset response into the local copy.
 *
 * One transaction, so the counter never bills off half a sync. Three shapes of
 * section, each for a stated reason (contract, step 3, amendment):
 *
 *   · **Deltaed** - items, stock and customers arrive by watermark and are
 *     upserted by key. A bootstrap replaces them wholesale, because a bootstrap
 *     reports nothing as deleted and a row that was withdrawn while the till was
 *     off would otherwise stay on the shelf for ever. Items and stock also lose
 *     rows through `deleted`. The customer list has no such channel - it would
 *     have to name the customer who asked to be erased - so after an erasure
 *     it arrives whole, marked `customers_whole`, and is replaced (ticket 16).
 *   · **Sent whole every time** - store, tax slabs, salespeople and managers. The
 *     manager list is the one that matters: it is a set of override credentials,
 *     and a rung withdrawn at head office has to be withdrawn at the counter on
 *     the next response, so it is replaced rather than patched.
 *   · **Closed rather than removed** - offers die by date as well as by edit,
 *     so `deleted` carries them and the till stops applying them.
 */
export async function applyDataset(db: TillDb, payload: DatasetPayload): Promise<void> {
  await db.transaction(
    "rw",
    [
      db.items,
      db.stock,
      db.offers,
      db.salespeople,
      db.managers,
      db.gstSlabs,
      db.seasons,
      db.customers,
      db.bills,
      db.meta,
      db.queue,
    ],
    async () => {
      if (payload.full) {
        await Promise.all([
          db.items.clear(),
          db.stock.clear(),
          db.offers.clear(),
        ]);
      }
      await db.items.bulkPut(payload.items);
      await db.stock.bulkPut(payload.stock);
      await db.offers.bulkPut(payload.offers);

      // Whole-list sections. Cleared first so a row that vanished server-side
      // vanishes here: none of these has a `deleted` channel to say so.
      // The staff list's salespeople (ticket 07). Guarded like the seasons: a
      // server that predates the section says nothing, not "nobody sells here".
      if (payload.salespeople) {
        await db.salespeople.clear();
        await db.salespeople.bulkPut(payload.salespeople);
      }
      await db.managers.clear();
      await db.managers.bulkPut(payload.managers);
      await db.gstSlabs.clear();
      await db.gstSlabs.bulkPut(payload.gst_slabs);
      // Seasons and the policy arrived after the till spine did (#181), so a
      // server that predates them - a rolling deploy is exactly that, for a few
      // minutes - answers without the key. Absent means "this server has nothing
      // to say", not "the master is empty": wiping it would drop scan resolution
      // back to sorting names, where "FW25 before SS26" is true only by the
      // accident of the alphabet, and the till would write a season onto the
      // line that the server would never have chosen.
      if (payload.seasons) {
        await db.seasons.clear();
        await db.seasons.bulkPut(payload.seasons);
      }
      // Customers (#245): a watermark and an upsert by mobile, like the items.
      // Guarded for the same rolling-deploy minutes the seasons are - an older
      // server answers without the section, and that means "nothing to say", not
      // "the phone book is empty".
      // Ticket 16: after a customer is erased head office sends the list whole
      // (`customers_whole`), and the copy is replaced so the erased row goes too.
      if (payload.customers) {
        if (payload.full || payload.customers_whole) await db.customers.clear();
        await db.customers.bulkPut(payload.customers);
      }

      // A withdrawal names a barcode and takes every season of that piece with
      // it: a cohort is a record of a purchase and is never unmade, so the only
      // withdrawal that exists is the SKU being deactivated.
      for (const barcode of payload.deleted.items) {
        await db.items.where("barcode").equals(barcode).delete();
        await db.stock.delete(barcode);
      }
      await db.offers.bulkDelete(payload.deleted.offers);

      // The bills an exchange may be taken against offline (OPS-09, §10.3).
      // Replaced whole, never merged: a bill drops out of the window by ageing
      // rather than by being deleted at head office, so a merge would leave last
      // quarter's bills on the device for ever. Guarded like the seasons and the
      // phone book for the same rolling-deploy minutes - a server answering
      // without the section has nothing to say, and wiping the cache on that
      // would take offline exchange away mid-shift.
      if (payload.bills) {
        await db.bills.clear();
        await db.bills.bulkPut(payload.bills);
        await applyQueuedReturns(db);
      }

      await replayQueuedStock(db, payload);

      // The transfer pause, settled against what head office just said (change
      // PRD §10.2). In this transaction so that "a full, fresh shelf landed" and
      // "billing may resume" are one fact, never two a crash could split.
      const pause = await readPause(db);
      const settled = settlePause(pause, payload.till, payload.full);
      if (settled !== pause) {
        if (settled) await db.meta.put({ key: META.pause, value: settled });
        else await db.meta.delete(META.pause);
      }

      await db.meta.bulkPut([
        { key: META.cursor, value: payload.cursor },
        // Tax settings (ticket 03), whole every time. An older server answers
        // without them, which means "nothing to say", so what is held stays.
        ...(payload.tax_settings ? [{ key: META.taxSettings, value: payload.tax_settings }] : []),
        // Ticket 05's switch. Absent (an older server) keeps what is held.
        ...(typeof payload.online_only_refusals === "boolean"
          ? [{ key: META.onlineOnly, value: payload.online_only_refusals }]
          : []),
        // Ticket 06's switch, the same way.
        ...(typeof payload.manager_pin_rules === "boolean"
          ? [{ key: META.managerPinRules, value: payload.manager_pin_rules }]
          : []),
        // Ticket 08's switch, the same way.
        ...(typeof payload.split_sale === "boolean"
          ? [{ key: META.splitSale, value: payload.split_sale }]
          : []),
        // Ticket 09's switch, the same way.
        ...(typeof payload.customer_display === "boolean"
          ? [{ key: META.customerDisplay, value: payload.customer_display }]
          : []),
        // Ticket 15's switch and wording, the same way.
        ...(typeof payload.customer_consent === "boolean"
          ? [{ key: META.customerConsent, value: payload.customer_consent }]
          : []),
        ...(payload.consent_wording
          ? [{ key: META.consentWording, value: payload.consent_wording }]
          : []),
        // Ticket 20's switch, the same way.
        ...(typeof payload.customer_reservation === "boolean"
          ? [{ key: META.customerReservation, value: payload.customer_reservation }]
          : []),
        // Ticket 18's switch, the same way.
        ...(typeof payload.saved_sizes === "boolean"
          ? [{ key: META.savedSizes, value: payload.saved_sizes }]
          : []),
        // Ticket 21's switch, the same way.
        ...(typeof payload.special_orders === "boolean"
          ? [{ key: META.specialOrders, value: payload.special_orders }]
          : []),
        // Ticket 19's switch, the same way.
        ...(typeof payload.gift_vouchers === "boolean"
          ? [{ key: META.giftVouchers, value: payload.gift_vouchers }]
          : []),
        // Ticket 22: the alteration charge line, or null when the switch is off.
        ...(payload.alteration_charge !== undefined
          ? [{ key: META.alterationCharge, value: payload.alteration_charge }]
          : []),
        { key: META.store, value: payload.store },
        {
          key: META.policy,
          // The new stacking dial is absent during a rolling deploy. An absent
          // policy must close manual entry rather than reuse a cap from a
          // different server response, which could be wider than this chain's.
          value: {
            manual_discount_cap_percent:
              payload.policy?.manual_discount_cap_percent ?? DEFAULT_POLICY.manual_discount_cap_percent,
            manual_discount_on_offer_lines:
              payload.policy?.manual_discount_on_offer_lines ?? false,
            return_window_days:
              payload.policy?.return_window_days ?? DEFAULT_POLICY.return_window_days,
          },
        },
        { key: META.syncedAt, value: new Date().toISOString() },
        // This device's identity as the store's counter, and its window (OPS-09).
        // Written inside the same transaction as everything else: a till holding
        // a snapshot and no idea which allocation covers it is a till nobody can
        // release the stock for.
        ...(payload.till
          ? [
              { key: META.till, value: deviceStateFrom(payload.till) },
              { key: META.allocation, value: payload.till.allocation_version },
            ]
          : []),
      ]);
    },
  );
}

/**
 * Put this till's own unsent returns back onto the bills the server just restated.
 *
 * The cached bills come down with the returned quantities *head office* knows
 * about, which is short by every exchange this counter has rung up and not yet
 * synced. Without this, a bill whose last piece was given back an hour ago would
 * offer that piece again the moment the dataset landed - the same hazard
 * `replayQueuedStock` exists for, on the other side of the same queue.
 */
async function applyQueuedReturns(db: TillDb): Promise<void> {
  const pending = await db.queue.toArray();
  for (const bill of pending) {
    const exchange = bill.exchange;
    if (!exchange) continue;
    const cached = await db.bills
      .filter(
        (row) =>
          row.fy === exchange.original.fy && row.till_seq === exchange.original.till_seq,
      )
      .first();
    if (!cached) continue;
    await db.bills.put(markReturnedOn(cached, exchange.lines));
  }
}

/** A cached bill with `qty` more pieces and `refund_paise` more money given back
 *  off each named line. Capped at what the line sold, so a double count cannot
 *  make a line look more than fully returned. */
export function markReturnedOn(
  bill: CachedBill,
  legs: { original_line: number; qty: number; refund_paise: number }[],
): CachedBill {
  const given = new Map<number, { qty: number; paise: number }>();
  for (const leg of legs) {
    const seen = given.get(leg.original_line) ?? { qty: 0, paise: 0 };
    given.set(leg.original_line, {
      qty: seen.qty + leg.qty,
      paise: seen.paise + leg.refund_paise,
    });
  }
  return {
    ...bill,
    lines: bill.lines.map((line) => {
      const back = given.get(line.line_no);
      if (!back) return line;
      return {
        ...line,
        returned_qty: Math.min(line.qty, line.returned_qty + back.qty),
        returned_paise: Math.min(line.net_paise, line.returned_paise + back.paise),
      };
    }),
  };
}

/**
 * Take the queue's sales back off the shelf the dataset just re-stated.
 *
 * The server's `stock` counts only the bills it has *received*, so every row it
 * sends is short by whatever this till has sold and not yet synced. Writing it
 * straight over the local count puts yesterday's unsynced sales back on the
 * shelf - and the store-open bootstrap replaces the whole table, so the daily
 * routine would undo the very decrement `commitBill` made at Save & Print.
 *
 * Only rows this payload actually wrote are adjusted. A delta that did not
 * mention a barcode left its local row alone, and that row already carries the
 * decrement; adjusting it again would take the piece off twice.
 */
async function replayQueuedStock(db: TillDb, payload: DatasetPayload): Promise<void> {
  const pending = await db.queue.toArray();
  if (!pending.length) return;
  const restated = payload.full ? null : new Set(payload.stock.map((row) => row.barcode));

  const net = new Map<string, number>();
  for (const bill of pending) {
    for (const line of bill.lines) {
      if (restated && !restated.has(line.barcode)) continue;
      const delta = line.direction === "return" ? line.qty : -line.qty;
      net.set(line.barcode, (net.get(line.barcode) ?? 0) + delta);
    }
  }
  for (const [barcode, delta] of net) {
    if (!delta) continue;
    const row = await db.stock.get(barcode);
    // No row means the server does not stock this piece here - a sold-before-
    // inward line. `commitBill` declined to invent a count for it, and so does
    // this.
    if (row) await db.stock.put({ barcode, qty: row.qty + delta });
  }
}

/** What this device is, and the two clocks its billing window is measured
 *  between. One `meta` row, because a counter that knew its series but not how
 *  long it may bill for is a counter that cannot answer the only question the
 *  Save & Print row asks. */
export interface TillDeviceState {
  identity: TillIdentity;
  anchor: AuthorityAnchor;
}

/** A device state from a `till` block the server just answered with. */
export function deviceStateFrom(identity: TillIdentity): TillDeviceState {
  return { identity, anchor: anchorFrom(identity) };
}

/** This device's counter identity and window, as the last sync left them. */
export async function readDevice(db: TillDb): Promise<TillDeviceState | null> {
  return readMeta<TillDeviceState | null>(db, META.till, null);
}

/**
 * Pull whatever has changed since the last cursor (everything, the first time),
 * and renew the counter's window while the line is up.
 *
 * Renewal rides on the sync rather than on a timer of its own, and the reason is
 * that they are the same event: a dataset that landed *is* the evidence the
 * window stands on, so a till that has just resynced has by definition been
 * talking to head office within the day. A separate timer would be a second
 * thing to go wrong and a second thing to forget.
 *
 * It is attempted after the data has landed, and its failure is not the sync's:
 * a server that answered the dataset and refused the renewal has told the till
 * something worth knowing, but nothing about the rows it just wrote.
 */
export async function syncDown(db: TillDb, transport: TillTransport): Promise<DatasetPayload> {
  const since = await readMeta(db, META.cursor, "");
  const payload = await transport.dataset(since);
  await applyDataset(db, payload);
  if (payload.till?.registered) {
    await renewAuthority(db, transport);
    // Ticket 04: top up the invoice number blocks while the line is up. Its
    // failure is not the sync's, for the same reason the renewal's is not.
    await refreshNumbering(db, transport);
  }
  return payload;
}

/** Ask for another 24 hours, and write down the two clocks it was measured
 *  between. Answers null when the server would not (an unregistered counter,
 *  a line that went down between the two calls). */
export async function renewAuthority(
  db: TillDb,
  transport: TillTransport,
): Promise<TillDeviceState | null> {
  let identity: TillIdentity;
  try {
    identity = await transport.renewTill();
  } catch {
    // A renewal that did not happen leaves the previous window exactly where it
    // was. It is not a reason to stop billing, and it is certainly not a reason
    // to throw away rows the dataset just wrote.
    return null;
  }
  const state = deviceStateFrom(identity);
  await writeMeta(db, META.till, state);
  await writeMeta(db, META.allocation, identity.allocation_version);
  return state;
}

/** Throw away the cursor so the next sync is a full bootstrap.
 *
 *  The contract is explicit that a bootstrap cannot miss anything by
 *  construction, while the delta's cursor laps a quarter of an hour behind the
 *  clock and is a *bound* on the hole rather than a guarantee against one. So the
 *  till takes one at store open, and this is how. */
export async function forceBootstrap(db: TillDb): Promise<void> {
  await writeMeta(db, META.cursor, "");
}

// ------------------------------------------------------------ register -------

/**
 * Reconcile the local counter with what the server has accepted.
 *
 * The one thing a till may never do is issue a number that is already on a posted
 * bill: two customers' purchases would land under one Tally key, and the second
 * one can never sync. A till that was reinstalled, or whose browser storage was
 * evicted, has a counter of 1 and a store that is on bill 300 - so at boot it
 * asks, and moves its counter forward if the server is ahead.
 *
 * It only ever moves **forward**. The ordinary state of a busy counter is to be
 * ahead of the server by however many bills are still in the queue, and dragging
 * the counter back to meet the server would re-issue every one of them.
 *
 * The financial years must agree first. The two clocks straddle 1 April for a few
 * minutes each year, and a till that reconciled a brand-new counter against last
 * year's frontier would jump to bill 5,001 and stay there.
 */
export async function reconcileRegister(
  db: TillDb,
  transport: TillTransport,
  now: Date = new Date(),
): Promise<RegisterPayload> {
  const register = await transport.register();
  await writeMeta(db, META.register, register);
  if (register.fy !== financialYear(now)) return register;

  // `fastForwardTo` owns the "never backwards" half, in the same file as the two
  // other readers of the counter.
  await fastForwardTo(db, register.fy, register.last_accepted_seq + 1);
  return register;
}

// ------------------------------------------------------------------ up -------

export interface DrainResult {
  /** Bills the server took (201) or recognised as replays (200). */
  accepted: number;
  /** Flags the server raised on them - a hole, an offer mismatch, a bill that
   *  arrived before the goods did. They belong on the Dashboard's action queue,
   *  never at the counter mid-sale. */
  flags: string[];
  /** Still waiting, including the halted one. */
  pending: number;
  /** Set when the queue stopped because the server refused a bill. */
  halt: QueueHalt | null;
  /** Set when the queue stopped because the network did. */
  retryAfterMs: number | null;
}

// Single-flight, per database. The engine drains on an interval, on the online
// event and on a person pressing the button, and two drains at once would offer
// the same bill twice: the server's idempotency makes that safe money-wise, but
// the two would then race to delete the row and to write the halt.
const inFlight = new Map<string, Promise<DrainResult>>();

interface QueueReadGate {
  readers: number;
  idle: Promise<void>;
  releaseIdle: () => void;
}

/** Reads that must see the server and this queue at one logical instant. A
 * return lookup is the important one: if a queued exchange drains between its
 * server detail response and the local overlay, the same piece briefly appears
 * returnable twice. */
const stableReads = new Map<string, QueueReadGate>();

function holdQueueStable(db: TillDb): () => void {
  let gate = stableReads.get(db.name);
  if (!gate) {
    let releaseIdle = () => undefined as void;
    const idle = new Promise<void>((resolve) => {
      releaseIdle = resolve;
    });
    gate = { readers: 0, idle, releaseIdle };
    stableReads.set(db.name, gate);
  }
  gate.readers += 1;
  return () => {
    if (!gate) return;
    gate.readers -= 1;
    if (gate.readers > 0) return;
    stableReads.delete(db.name);
    gate.releaseIdle();
  };
}

/** Run a read after the current drain and hold later drains until it has used
 * both the server answer and the local queue. The hold is per till database,
 * just like the drain single-flight. */
export async function withStableQueue<T>(db: TillDb, read: () => Promise<T>): Promise<T> {
  const running = inFlight.get(db.name);
  if (running) await running;
  const release = holdQueueStable(db);
  try {
    return await read();
  } finally {
    release();
  }
}

/**
 * Offer queued bills to the server, oldest first, until one will not go.
 *
 * FIFO and strictly in order. The queue is not reordered to get past a bill the
 * server refuses, because the bills behind it are the ones whose numbers follow
 * it: syncing them first would report the refused bill's number as a hole and
 * send a store person looking for a bill that is sitting right here.
 */
export function drainQueue(db: TillDb, transport: TillTransport): Promise<DrainResult> {
  const running = inFlight.get(db.name);
  if (running) return running;
  const stable = stableReads.get(db.name)?.idle;
  const attempt = (stable ? stable.then(() => drainOnce(db, transport)) : drainOnce(db, transport))
    .finally(() => inFlight.delete(db.name));
  inFlight.set(db.name, attempt);
  return attempt;
}

async function drainOnce(db: TillDb, transport: TillTransport): Promise<DrainResult> {
  const result: DrainResult = {
    accepted: 0,
    flags: [],
    pending: 0,
    halt: await readMeta<QueueHalt | null>(db, META.halt, null),
    retryAfterMs: null,
  };
  // A halted queue stays halted until a human clears it. Draining past the bill
  // the server refused is exactly the "silently dropped" this must not do.
  if (result.halt) {
    result.pending = await db.queue.count();
    return result;
  }

  for (;;) {
    const bill = await db.queue.orderBy("id").first();
    if (!bill) break;
    let accepted;
    try {
      accepted = await transport.postSale(bill);
    } catch (error) {
      // Only the *network* call is guarded here. A local failure - a full disk,
      // a closed database - is not a refusal of the bill and must not be dressed
      // up as one: it would read as "no connection" for ever while the real
      // reason went unsaid. It escapes to the engine, which records it.
      const refusal = asRefusal(error);
      const attempts = bill.attempts + 1;
      await db.queue.update(bill.id as number, {
        attempts,
        last_error: refusal.message,
      });
      if (refusal.terminal) {
        result.halt = {
          doc_number: bill.doc_number,
          idempotency_uuid: bill.idempotency_uuid,
          code: refusal.code,
          message: refusal.message,
          at: new Date().toISOString(),
        };
        await writeMeta(db, META.halt, result.halt);
      } else {
        result.retryAfterMs = backoffMs(attempts);
      }
      break;
    }
    // Outside the guard on purpose. The server has taken the bill; if the local
    // delete now fails, the loop must not treat that as the *server* refusing
    // one - the bill would sit in the queue being re-offered every minute with
    // "no connection" written beside it, which is a lie about a bill that is on
    // the books. Delete by primary key rather than by object: the row may have
    // been rewritten with a new attempt count since it was read.
    await db.queue.delete(bill.id as number);
    result.accepted += 1;
    result.flags.push(...accepted.flags);
  }
  result.pending = await db.queue.count();
  return result;
}

/** What the server said, or - for anything the transport did not shape - the
 *  fact that we never heard back. A transport is free to throw a plain `Error`
 *  (a fake in a test, an interceptor that rejected before the request went out),
 *  and "we do not know what happened to this bill" is always retry, never halt. */
function asRefusal(error: unknown): TillHttpError {
  if (error instanceof TillHttpError) return error;
  return new TillHttpError(0, "NETWORK", messageOf(error) || "No connection to head office.");
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

// --------------------------------------------------------------- holds -------

/**
 * Tell head office what is parked at this counter (#185, grill Q13).
 *
 * The whole list every time, because the till is authoritative and there is no
 * per-hold delete to replay: a hold resumed at the counter disappears from the
 * store's Dashboard by not being in the next push.
 *
 * Deliberately **not** part of the queue. Nothing here is money - a hold moves no
 * stock, no number and no value - so a failure has nothing to halt over and no
 * bill to name. It is offered again on the next sync, and in the meantime the
 * only thing that is wrong is a count on somebody else's screen.
 */
export async function pushHeld(db: TillDb, transport: TillTransport): Promise<number> {
  const rows = await db.held.toArray();
  const answer = await transport.putHeld(rows.map(mirrorRow));
  return answer.count;
}

/**
 * Let the queue try the refused bill again.
 *
 * Deliberately the only way out, and deliberately not a way to *discard* the
 * bill: the receipt is in a customer's hand and the money is in the drawer, so a
 * till that could throw the record away would be a till that can lose a sale. A
 * refusal the server will keep giving - `BILL_NO_TAKEN` after two machines
 * numbered the same series - is resolved by the register handover and re-entry
 * from the printed copy (contract, step 3), not here.
 */
export async function clearHalt(db: TillDb): Promise<void> {
  await writeMeta(db, META.halt, null);
}
