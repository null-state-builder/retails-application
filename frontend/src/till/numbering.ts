// The commit point (#180, D10 step 3, grill Q1 and Q2).
//
// Save & Print is where a sale becomes a fact: the number is assigned, the shelf
// goes down by one, the receipt prints, and none of it waits for a network. The
// till owns the bill counter for its store - one POS per store is a hard
// invariant, so there is exactly one writer on the series - and the server's job
// at sync is to accept each number exactly once, never to hand one out.
//
// That makes this module small and load-bearing in equal measure. The number and
// the queued bill **must** be written in one local transaction. Split them and
// each half is a way to lose money:
//
//   · number first, queue second, crash between - the number is spent on a bill
//     that does not exist. The server later sees a hole and the store spends the
//     evening looking for a bill nobody wrote.
//   · queue first, number second - two bills carry the same number, the second
//     one is refused for ever with `BILL_NO_TAKEN`, and two customers' purchases
//     sit under one Tally key.
//
// So there are exactly two ways in - `commitBill`, which takes the next number,
// and `reenterPaperBill` (#189), which fills a hole a dead machine left - and the
// function that reads the counter is not exported. Nothing outside this file can
// obtain a bill number.

import { financialYear } from "../lib/fiscal";

import { META, readMeta } from "./db";
import type { TillDb } from "./db";
import { TillPausedError } from "./guard";
import { takeInvoiceNumber } from "./invoiceNumbers";
import type { BillDraft, PaperEntered, QueuedBill } from "./types";
import { newUuid } from "./uuid";

/** The document type the till numbers. The kernel accepts external numbers on
 *  this series and no other (`core.documents.EXTERNAL_NUMBER_DOC_TYPES`). */
const SALE_DOC_TYPE = "SAL";

/** How a bill number reads on the customer's copy, and in Tally.
 *  The mirror of `VoucherSeries.render` with no configured affixes. */
export function renderBillNumber(fy: string, storeCode: string, seq: number): string {
  return `${fy}/${storeCode}/${SALE_DOC_TYPE}/${seq}`;
}

/**
 * How a registered till's bill reads on the shop floor: `DEO-T1-74` (R-POS-005).
 *
 * The **display** series, and deliberately beside the Tally key above rather than
 * instead of it. `doc_number` is the join key whose rendered form freezes the
 * moment a store's external series starts counting (`core.documents.VoucherSeries`
 * says so in as many words), so re-rendering it with a counter id would turn one
 * bill into two keys and two documents. A store with no registered counter has no
 * prefix and prints exactly what it always printed.
 *
 * The counter id is what makes replacement safe: a new device takes the store's
 * next unused counter id, so `DEO-T2-75` can never read the same as anything
 * `DEO-T1` printed - which is R-POS-005's "resets, cloning, replacement,
 * restoration and counter-ID reuse must not recreate prior bill identities".
 */
export function renderTillNumber(seriesPrefix: string, seq: number): string {
  return seriesPrefix ? `${seriesPrefix}-${seq}` : "";
}

/** The number the next bill will take, for a screen to show. Read-only, and
 *  advisory: by the time anything acts on it another bill may have taken it.
 *  Assigning a number happens in `commitBill` and nowhere else. */
export async function previewNextNumber(
  db: TillDb,
  storeCode: string,
  now: Date = new Date(),
): Promise<string> {
  const fy = financialYear(now);
  return renderBillNumber(fy, storeCode, await counterFor(db, fy));
}

/**
 * Move the counter up to `seq`, and never down.
 *
 * The one legitimate reason to write the counter from outside a commit: the
 * server has told us it already holds a bill at a number this till would hand
 * out again (see `sync.reconcileRegister`). It lives here, with the counter's
 * other two readers, so the rule about what "where are we up to" means is
 * written in one file rather than re-derived at each site.
 *
 * Forward only, and by a floor rather than an assignment: the ordinary state of
 * a busy counter is to be ahead of the server by whatever is still queued, and a
 * plain write would re-issue every one of those numbers.
 */
export async function fastForwardTo(db: TillDb, fy: string, seq: number): Promise<number> {
  return db.transaction("rw", db.meta, async () => {
    const local = await counterFor(db, fy);
    const next = Math.max(local, seq);
    await db.meta.bulkPut([
      { key: META.fy, value: fy },
      { key: META.nextSeq, value: next },
    ]);
    return next;
  });
}

/** Where this till's counter stands for `fy` - 1 for a year it has not counted
 *  in yet, which is what makes a financial-year rollover a read rather than a
 *  scheduled job. */
export async function counterFor(db: TillDb, fy: string): Promise<number> {
  const countingFor = await readMeta(db, META.fy, "");
  return countingFor === fy ? await readMeta(db, META.nextSeq, 1) : 1;
}

/**
 * Number a bill and queue it, all or nothing.
 *
 * One Dexie read-write transaction covering `meta` (the counter), `queue` (the
 * bill) and `stock` (the shelf), which is the same set the design names for Save
 * & Print. If any part throws, IndexedDB rolls the whole thing back and the
 * counter is exactly where it was - the customer is told the bill did not save,
 * which is recoverable, rather than being handed a receipt whose number the till
 * has already given away.
 *
 * The financial year is the till's own clock (grill Q1): at midnight on 1 April
 * the counter restarts at 1 without anybody deploying anything, because the
 * server seeds next year's series row alongside this year's.
 */
export async function commitBill(
  db: TillDb,
  storeCode: string,
  draft: BillDraft,
  now: Date = new Date(),
): Promise<QueuedBill> {
  return writeBill(db, storeCode, draft, financialYear(now), null);
}

/** This device's series prefix, or "" for a counter nobody has registered.
 *
 *  Read inside the commit transaction rather than passed in, so the number a bill
 *  carries and the counter it was numbered on cannot be decided at two different
 *  moments by two different pieces of code. */
async function seriesPrefixOf(db: TillDb): Promise<string> {
  const device = await readMeta<{ identity?: { series_prefix?: string } } | null>(
    db,
    META.till,
    null,
  );
  return device?.identity?.series_prefix ?? "";
}

/**
 * Key a printed bill back in under the number it already carries (#189).
 *
 * The other half of a register handover. A machine that died holding six unsynced
 * bills left six receipts in a drawer and six holes in the store's series; this is
 * how those receipts become rows, on a machine that never issued their numbers.
 *
 * Three things make it different from an ordinary commit, and all three are the
 * same worry from different sides - that this becomes a way to mint a number:
 *
 *   · **The number is given, not taken.** The counter does not move, because this
 *     bill is *behind* the counter by definition.
 *   · **It must be a hole.** A sequence at or past where the till is up to is not
 *     a bill from a dead machine, it is a fresh one, and re-entering it here
 *     would spend a number twice.
 *   · **Once, and once for good.** Two people working through the same drawer -
 *     or one person reloading a page whose address still names the bill - would
 *     otherwise queue the same receipt twice. The record of what has been keyed
 *     in is `META.paperEntered`, which outlives both the queue (a bill leaves it
 *     the moment the server takes it) and the handover list (a store puts that
 *     away when it is done). The second attempt is refused here rather than left
 *     for the server, whose only answer is `BILL_NO_TAKEN` - terminal, which
 *     stops the store's whole queue behind a bill nobody meant to send twice.
 *
 * The stock still moves. The pieces went out of the door on the old machine, but
 * this counter's shelf was rebuilt from the server's figures - which never heard
 * about these bills - so the local count is holding them and has to let go.
 */
export async function reenterPaperBill(
  db: TillDb,
  storeCode: string,
  draft: BillDraft,
  seq: number,
  now: Date = new Date(),
): Promise<QueuedBill> {
  return writeBill(db, storeCode, { ...draft, origin: "paper" }, financialYear(now), seq);
}

async function writeBill(
  db: TillDb,
  storeCode: string,
  draft: BillDraft,
  fy: string,
  atSeq: number | null,
): Promise<QueuedBill> {
  // Generated outside the transaction because it is not state: the key exists to
  // make the *server* side idempotent, so a bill that rolls back here and is
  // retried by the cashier is a genuinely different bill and wants a new one.
  const idempotencyUuid = newUuid();

  // `items` is in scope because the shelf move asks it whether the piece is one
  // the counter has ever heard of. Read-only in practice, but a Dexie
  // transaction has to declare every table it will touch, and a commit that
  // reached outside its own scope would throw at the worst possible moment.
  // `bills` joins the transaction for OPS-09: an exchange has to mark the pieces
  // it gave back on the cached original in the same commit as the bill that gave
  // them back, or a crash between the two would leave the same piece returnable
  // twice on this device.
  return db.transaction("rw", [db.meta, db.queue, db.stock, db.items, db.bills], async () => {
    // The transfer pause is read here, inside the transaction that would take
    // the number, rather than trusted from a screen (change PRD §10.2). A
    // paused counter's shelf may already be promised to a transfer, and this is
    // the one place every bill - a sale, an exchange, a paper re-entry - has
    // to pass through.
    if (await readMeta(db, META.pause, null)) throw new TillPausedError();
    const seq = atSeq === null ? await nextBillNumber(db, fy) : await claimHole(db, fy, atSeq);
    if (atSeq !== null) await recordPaperEntry(db, fy, seq);
    const prefix = await seriesPrefixOf(db);
    // Ticket 04: the number in the new invoice series, taken in this same
    // transaction so a bill that rolls back gives it back. A bill keyed in from
    // paper already carries whatever its printed copy says, so it takes none.
    const invoice = atSeq === null ? await takeInvoiceNumber(db, new Date(draft.billed_at)) : null;
    const bill: QueuedBill = {
      ...draft,
      idempotency_uuid: idempotencyUuid,
      store: storeCode,
      fy,
      till_seq: seq,
      origin: draft.origin ?? (navigator.onLine ? "online" : "offline"),
      doc_number: renderBillNumber(fy, storeCode, seq),
      till_number: renderTillNumber(prefix, seq),
      ...(invoice ? { tax_invoice_number: invoice } : {}),
      attempts: 0,
    };
    await db.queue.add(bill);
    await moveStock(db, bill);
    await markReturnedPieces(db, bill);
    return bill;
  });
}

/**
 * Write this bill's returned pieces onto the cached original, in the same commit.
 *
 * PRD §10.4: a returned piece is recorded against its original bill line so the
 * same piece cannot be returned twice - "on this till or after sync on another".
 * The server's half is `ALREADY_RETURNED` against the original line's own count;
 * this is the device's half, and it has to be in the commit rather than after it.
 * A crash in between would leave the customer holding a receipt for an exchange
 * the till had forgotten, and the next scan would offer the same piece again.
 *
 * A bill the cache does not hold is left alone rather than invented: the exchange
 * path refuses an uncached original offline (`original.ts`), so an exchange
 * reaching here against a bill that is not cached came from the *server* while
 * online, and the server's own count is the ceiling for that one.
 */
async function markReturnedPieces(db: TillDb, bill: QueuedBill): Promise<void> {
  const exchange = bill.exchange;
  if (!exchange) return;
  const cached = await db.bills
    .filter((row) => row.fy === exchange.original.fy && row.till_seq === exchange.original.till_seq)
    .first();
  if (!cached) return;
  const given = new Map<number, { qty: number; paise: number }>();
  for (const leg of exchange.lines) {
    const seen = given.get(leg.original_line) ?? { qty: 0, paise: 0 };
    given.set(leg.original_line, {
      qty: seen.qty + leg.qty,
      paise: seen.paise + leg.refund_paise,
    });
  }
  await db.bills.put({
    ...cached,
    lines: cached.lines.map((line) => {
      const back = given.get(line.line_no);
      if (!back) return line;
      return {
        ...line,
        returned_qty: Math.min(line.qty, line.returned_qty + back.qty),
        returned_paise: Math.min(line.net_paise, line.returned_paise + back.paise),
      };
    }),
  });
}

/** Take a number the counter has already given out, and refuse anything else.
 *
 *  Inside `writeBill`'s transaction, so the check and the write of both the bill
 *  and the "this one is done" record cannot be separated by a second re-entry of
 *  the same receipt. */
async function claimHole(db: TillDb, fy: string, seq: number): Promise<number> {
  if (!Number.isInteger(seq) || seq < 1) {
    throw new Error(`${seq} is not a bill number.`);
  }
  const counter = await counterFor(db, fy);
  if (seq >= counter) {
    throw new Error(
      `Bill ${seq} has not been printed yet - this counter is on ${counter}. ` +
        "Only a bill from the old machine is re-entered from paper.",
    );
  }
  const entered = await paperEntries(db, fy);
  if (entered.includes(seq)) {
    throw new Error(`Bill ${seq} has already been entered from its printed copy.`);
  }
  const queued = await db.queue.filter((bill) => bill.fy === fy && bill.till_seq === seq).count();
  if (queued) {
    throw new Error(`Bill ${seq} has already been re-entered and is waiting to sync.`);
  }
  return seq;
}

/** Which numbers this counter has keyed in from paper this year.
 *
 *  A year that is not the stored one reads as empty rather than being cleared:
 *  the read happens on the commit path, and a rollover is not the moment to be
 *  writing. `recordPaperEntry` replaces the row when it next writes. */
export async function paperEntries(db: TillDb, fy: string): Promise<number[]> {
  const entered = await readMeta<PaperEntered | null>(db, META.paperEntered, null);
  return entered && entered.fy === fy ? entered.seqs : [];
}

async function recordPaperEntry(db: TillDb, fy: string, seq: number): Promise<void> {
  const seqs = await paperEntries(db, fy);
  await db.meta.put({
    key: META.paperEntered,
    value: { fy, seqs: [...seqs, seq].sort((a, b) => a - b) } satisfies PaperEntered,
  });
}

/**
 * Take the next sequence for `fy` and advance the counter.
 *
 * Deliberately not exported. It MUST run inside `commitBill`'s transaction - on
 * its own it is a way to spend a number on nothing.
 */
async function nextBillNumber(db: TillDb, fy: string): Promise<number> {
  // A new financial year is a new series, starting at 1. Reading the stored year
  // rather than comparing dates means a till that was switched off across 1 April
  // rolls over when it is switched on, not when somebody remembers.
  const seq = await counterFor(db, fy);
  await db.meta.bulkPut([
    { key: META.fy, value: fy },
    { key: META.nextSeq, value: seq + 1 },
  ]);
  return seq;
}

/**
 * Move the counter's own copy of the shelf, in the same transaction as the bill.
 *
 * The local count is what the next scan reads, so it has to move at Save & Print
 * rather than when the server hears about the sale - otherwise a busy counter
 * offline sells the same last piece all afternoon.
 *
 * A count that goes negative is allowed and is not an error (grill Q6): the piece
 * was on the shelf, so the count was wrong, and the next stock count reconciles
 * it. A barcode the till has never heard of is a sold-before-inward line and gets
 * no stock row - inventing one would put a piece the books do not know about into
 * the counter's own stock figures.
 */
async function moveStock(db: TillDb, bill: QueuedBill): Promise<void> {
  const net = new Map<string, number>();
  for (const line of bill.lines) {
    const delta = line.direction === "return" ? line.qty : -line.qty;
    net.set(line.barcode, (net.get(line.barcode) ?? 0) + delta);
  }
  for (const [barcode, delta] of net) {
    if (!delta) continue;
    const row = await db.stock.get(barcode);
    if (row) {
      await db.stock.put({ barcode, qty: row.qty + delta });
      continue;
    }
    const known = await db.items.where("barcode").equals(barcode).count();
    if (known) await db.stock.put({ barcode, qty: delta });
  }
}
