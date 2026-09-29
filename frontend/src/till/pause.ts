// The counter's transfer pause (change PRD §10.2, Anand, 25 September 2026).
//
// Every sync protects the store's stock for the till, so nothing can be
// reserved away to a transfer while the counter might be selling it. To send
// stock out, the store's own person pauses the counter and lets the stock go -
// and the order of those two is the whole design:
//
//   1. **The counter pauses itself first**, in its own database, in the same
//      transaction that checks the queue is empty. From that moment no bill is
//      finalised: `numbering.ts` reads the pause inside the commit, so a reload,
//      a restart or a dead line changes nothing.
//   2. **Then it asks head office to release**, naming where it would bill next:
//      the year it is counting in and the number in that year. Head office
//      releases only if every number below that position has arrived,
//      and records its own half of the pause in the same step. A plain refusal
//      takes the local pause back off - nothing was released. No answer at all
//      leaves the counter paused, which is the safe side of not knowing.
//   3. **Resume** ends the server's pause and then takes a full, fresh dataset.
//      Only that dataset landing lifts the local pause (`settlePause`), so the
//      counter never bills again from the shelf it had before the transfer.

import { META, readMeta } from "./db";
import type { TillDb } from "./db";
import { financialYear } from "../lib/fiscal";
import { counterFor } from "./numbering";
import { TillHttpError } from "./transport";
import type { TillIdentity, TillPauseState } from "./types";

export async function readPause(db: TillDb): Promise<TillPauseState | null> {
  return readMeta<TillPauseState | null>(db, META.pause, null);
}

/**
 * What the pause should be once a dataset has landed. Pure, so `applyDataset`
 * can call it inside its own transaction.
 *
 *   · The server says paused and this device has no pause (it lost its data, or
 *     it is a new database) - take the server's.
 *   · The server says paused and this device is still `pausing` - confirmed.
 *   · The server says not paused, this device is `resuming`, and the dataset is
 *     a full one - the pause is over.
 *   · Anything else stays as it is. In particular a device that is `paused` or
 *     `pausing` is not un-paused by a server that says it is not: a person asked
 *     for the pause, and a person resumes it.
 */
export function settlePause(
  local: TillPauseState | null,
  till: TillIdentity | undefined,
  full: boolean,
): TillPauseState | null {
  if (!till) return local;
  if (till.paused) {
    // A `resuming` counter has already been told head office ended the pause, so
    // a "paused" here is an answer from before that; it stays shut either way,
    // and only a full dataset after the resume lets it bill.
    if (local?.stage === "resuming") return local;
    if (!local) {
      return {
        stage: "paused",
        reason: till.pause_reason ?? "",
        at: till.paused_at ?? new Date().toISOString(),
        fy: till.pause_fy ?? financialYear(),
        nextSeq: till.pause_next_seq ?? 1,
      };
    }
    return local.stage === "pausing" ? { ...local, stage: "paused" } : local;
  }
  if (local?.stage === "resuming" && full) return null;
  return local;
}

/**
 * Where this counter stands in its bill series: the year it is counting in and
 * the number it would give next in that year.
 *
 * One position, used for the release check, the pause and the resume alike, so
 * head office judges every one of them in the same year. That year is the
 * counter's own `META.fy` - the year of its last bill - and never today's date
 * or head office's: in the minutes the clocks straddle 1 April, a counter whose
 * last bill was 500 of the old year is still "next is 501 of the old year",
 * and everything it prints after that (bill 1 of the new year included) is
 * later in the series. A counter that has never billed starts at 1 of `now`'s
 * year.
 */
export async function positionNow(
  db: TillDb,
  now: Date = new Date(),
): Promise<{ fy: string; nextSeq: number }> {
  const fy = (await readMeta(db, META.fy, "")) || financialYear(now);
  return { fy, nextSeq: await counterFor(db, fy) };
}

/**
 * Write the local pause, all or nothing, and answer it.
 *
 * The queue and the halt are read in the same transaction as the write, so a
 * bill committed a moment ago cannot be left behind a pause that thinks the
 * queue was empty.
 */
export async function beginPause(
  db: TillDb,
  reason: string,
  now: Date = new Date(),
): Promise<TillPauseState> {
  return db.transaction("rw", [db.meta, db.queue], async () => {
    if (await readPause(db)) throw new Error("This counter is already paused.");
    if (await readMeta(db, META.halt, null)) {
      throw new Error("A bill is stuck. Sort it out before pausing.");
    }
    if ((await db.queue.count()) > 0) {
      throw new Error("Some bills have not reached head office yet. Sync first.");
    }
    const pause: TillPauseState = {
      stage: "pausing",
      reason,
      at: now.toISOString(),
      ...(await positionNow(db, now)),
    };
    await db.meta.put({ key: META.pause, value: pause });
    return pause;
  });
}

/** Move the pause to `stage`, if it is still at `from`. */
export async function movePause(
  db: TillDb,
  from: TillPauseState["stage"],
  stage: TillPauseState["stage"] | null,
): Promise<void> {
  await db.transaction("rw", db.meta, async () => {
    const pause = await readPause(db);
    if (pause?.stage !== from) return;
    if (stage === null) await db.meta.delete(META.pause);
    else await db.meta.put({ key: META.pause, value: { ...pause, stage } });
  });
}

/** A refusal from head office, as opposed to no answer at all. */
export function isRefusal(error: unknown): boolean {
  return error instanceof TillHttpError && error.status >= 400 && error.status < 500;
}
