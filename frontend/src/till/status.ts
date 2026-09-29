// What colour the sync light is, and why (#180, D10 grill Q1).
//
// One function, pure, because the light is the only thing a busy counter person
// looks at and "amber" has to mean the same thing at every till on every day.
// The three colours are not severity - they are three different sentences:
//
//   · **green** - the server has everything this counter has done.
//   · **amber** - work is outstanding and the till is handling it. Nobody needs
//     to do anything; the count says how far behind we are.
//   · **red** - the till cannot be trusted to bill, or a bill needs a human. This
//     is the only colour that asks for an action, which is why nothing routine is
//     ever allowed to reach it.
//
// Note what is deliberately *not* red: being offline. The till is offline-first
// and selling with no network is the designed state, not a fault (grill Q5).

import { AUTHORITY_EXPIRED } from "./authority";
import type { QueueHalt, RegisterPayload } from "./types";

export type SyncColour = "green" | "amber" | "red";

export interface SyncStatus {
  colour: SyncColour;
  /** Two or three words, for the chip itself. */
  label: string;
  /** What the light means, in a sentence a store person can act on. */
  reason: string;
}

export interface StatusInput {
  /** Has a dataset ever landed? A till with no local copy cannot price a scan. */
  datasetReady: boolean;
  /** Bills waiting to go up. */
  pending: number;
  /** Set when the server refused a bill and the queue stopped. */
  halt: QueueHalt | null;
  /** What the server last told us about this counter, if we have asked. */
  register: RegisterPayload | null;
  /** Did the browser throw the local database away between sessions? */
  storageLost: boolean;
  /** Does this tab own the store's counter, or is it the second one open? */
  lockHeld: boolean;
  /** The 24-hour offline billing window has closed (OPS-09, PRD §10.1). */
  authorityExpired?: boolean;
  online: boolean;
}

export function deriveStatus(input: StatusInput): SyncStatus {
  // Ordered by what a person should do about it, worst first: a halted queue
  // matters more than a stale dataset, and both matter more than a count. The
  // chip's word and the sentence behind it are decided together, here and
  // nowhere else - two places deciding what the light says is two lights.
  if (input.storageLost) {
    return red(
      "Local data lost",
      "This device cleared the counter's local data. Recover the counter from Till & Sync so it knows which bill number it is on - it will not bill until you do.",
    );
  }
  if (!input.lockHeld) {
    return red(
      "Second window",
      "This counter is already open in another tab or window. One store bills from one place, so this one will not take a bill.",
    );
  }
  // Red, and above the halt: a counter that may not take the *next* bill is a
  // stronger sentence than one bill head office argued with. Deliberately not
  // alarming about what is already recorded, because nothing is lost - the queue
  // drains on reconnect and renewing puts the counter straight back to work.
  if (input.authorityExpired) {
    return red(
      "Authority expired",
      `${AUTHORITY_EXPIRED}. Bills already taken are safe and will sync; this counter cannot start a new one until it renews.`,
    );
  }
  if (input.halt) {
    return red(
      "Bill refused",
      `Bill ${input.halt.doc_number} was not accepted: ${input.halt.message}`,
    );
  }
  if (!input.datasetReady) {
    return red(
      "No price list",
      "No local copy of the price list yet. Connect and sync before billing.",
    );
  }
  if (input.register && !input.register.series_open) {
    return red(
      "No bill series",
      `Head office has not opened a bill series for ${input.register.fy}. Bills printed today will not sync.`,
    );
  }
  if (input.pending > 0) {
    const bills = input.pending === 1 ? "bill" : "bills";
    return {
      colour: "amber",
      label: `${input.pending} waiting`,
      reason: `${input.pending} ${bills} waiting to sync.`,
    };
  }
  if (!input.online) {
    return {
      colour: "amber",
      label: "Offline",
      reason: "Working offline. Bills will sync when the line is back.",
    };
  }
  return { colour: "green", label: "Synced", reason: "Everything is synced." };
}

function red(label: string, reason: string): SyncStatus {
  return { colour: "red", label, reason };
}
