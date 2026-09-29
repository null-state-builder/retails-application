// How long this counter may keep billing without a network (OPS-09, PRD §10.1).
//
// A registered till bills offline under a 24-hour window that only an online
// renewal can extend. Past the window it refuses to finalise a **new** bill and
// does nothing else: no queued bill is deleted, no protected quantity is
// released, no local row is hidden, and the queue keeps draining the moment the
// line comes back (§10.1, and the invariant in PRD §13).
//
// ## The clock problem, and what is actually done about it
//
// The window is the *server's*, measured on the server's clock. The device's own
// clock is a setting on a machine in a shop, and a counter that has run out of
// authority is exactly the counter whose operator has a reason to move it.
//
// So the till never asks "what does this device think the time is". At every
// renewal it writes down two numbers together: the server's clock at that moment
// (`server_time`) and the browser's monotonic reading (`performance.now()`),
// which counts forward from page load and cannot be set by anybody. How much of
// the window is left is then the server's own arithmetic plus however much
// monotonic time has passed - a figure a shop floor cannot move.
//
// **The limit, stated rather than papered over.** `performance.now()` restarts at
// nought on every page load, so it can only measure elapsed time *within one
// session*. Across a reload, a browser restart or a machine that was switched off
// overnight there is no monotonic clock to read, and the till falls back to the
// device's wall clock - which is the honest position, not a hidden one. The fall
// back is deliberately one-sided: where the two readings disagree, the till takes
// whichever says **less** time is left. A counter wrongly asking to reconnect is a
// minute's inconvenience; a counter wrongly believing it still has authority is
// billing nobody licensed.
//
// R-POS-004 asks that guarantees disclose their limits: none of this is
// tamper-proof, `meta` is ordinary browser storage, and the real protection is
// that the server refuses to renew a window for a device it is not talking to.

import type { TillIdentity } from "./types";

/** What the till wrote down the last time the server renewed its window. */
export interface AuthorityAnchor {
  /** End of the window, as the server stated it (ISO). */
  authority_until: string | null;
  /** The server's clock at the moment it said so (ISO). */
  server_time: string;
  /** `performance.now()` at that same moment, if this browser has one. Null
   *  makes the reading wall-clock-only, which is what it is after a reload. */
  monotonic_ms: number | null;
  /** The device's own clock then, so a reading after a reload still has two
   *  points to subtract. */
  device_time: string;
}

/** Take the anchor from a `till` block the server just answered with. */
export function anchorFrom(
  identity: TillIdentity,
  now: Date = new Date(),
  monotonic: number | null = monotonicNow(),
): AuthorityAnchor {
  return {
    authority_until: identity.authority_until,
    server_time: identity.server_time,
    monotonic_ms: monotonic,
    device_time: now.toISOString(),
  };
}

/** `performance.now()` where the browser offers one, else null. */
export function monotonicNow(): number | null {
  const clock = globalThis.performance;
  return clock && typeof clock.now === "function" ? clock.now() : null;
}

export interface AuthorityState {
  /** This device is a registered counter with a window recorded. */
  known: boolean;
  /** Milliseconds left, floored at nought. Nought means expired. */
  remainingMs: number;
  expired: boolean;
  /** The window's end as the server stated it, for a screen to print. */
  until: string | null;
}

/**
 * How much of the window is left, on the least generous reading available.
 *
 * Two readings are taken and the smaller wins - see the module note on why the
 * bias is one-sided:
 *
 *   · **monotonic** - the server's own remaining figure at the anchor, less the
 *     browser's monotonic elapsed time. Unaffected by anything a person can do to
 *     the machine, and available only inside the session that took the anchor.
 *   · **wall clock** - the same server figure, less how far the device's own
 *     clock has moved since the anchor. Available always; trusted only as far as
 *     the clock is.
 *
 * A device clock dragged *backwards* makes the wall-clock reading generous, and
 * the monotonic one then caps it. A clock dragged forwards makes it mean, and the
 * till asks to reconnect sooner than it strictly had to - which is the side of
 * the line to be wrong on.
 */
export function authorityState(
  anchor: AuthorityAnchor | null,
  now: Date = new Date(),
  monotonic: number | null = monotonicNow(),
): AuthorityState {
  if (!anchor || !anchor.authority_until) {
    return { known: false, remainingMs: 0, expired: false, until: anchor?.authority_until ?? null };
  }
  const until = Date.parse(anchor.authority_until);
  const anchoredAt = Date.parse(anchor.server_time);
  const deviceAt = Date.parse(anchor.device_time);
  if (!Number.isFinite(until) || !Number.isFinite(anchoredAt)) {
    return { known: false, remainingMs: 0, expired: false, until: anchor.authority_until };
  }
  // What the server said was left, at the instant it said it.
  const granted = until - anchoredAt;
  const byWallClock = Number.isFinite(deviceAt) ? granted - (now.getTime() - deviceAt) : granted;
  const byMonotonic =
    anchor.monotonic_ms !== null && monotonic !== null
      ? granted - (monotonic - anchor.monotonic_ms)
      : Number.POSITIVE_INFINITY;
  const remaining = Math.min(byWallClock, byMonotonic);
  return {
    known: true,
    remainingMs: Math.max(0, remaining),
    expired: remaining <= 0,
    until: anchor.authority_until,
  };
}

/** What a counter is told when its window has closed.
 *
 *  One sentence, spelled once, because three screens say it: the Save & Print
 *  row, the Till & Sync panel and the sync light. It says what happened and what
 *  to do, and it deliberately does **not** say anything is lost - because nothing
 *  is (§10.1). */
export const AUTHORITY_EXPIRED = "Till authority expired — reconnect to renew";

/** Why this counter may not finalise a new bill on authority grounds, or "".
 *
 *  Only ever a reason about *new* bills. Everything already recorded stays where
 *  it is, and the queue drains on reconnect whether or not the window is open. */
export function authorityBlock(state: AuthorityState): string {
  return state.expired ? AUTHORITY_EXPIRED : "";
}

/** The window in words, for the Till & Sync panel. */
export function describeAuthority(state: AuthorityState): string {
  if (!state.known) return "This counter has no billing window yet. Connect and renew.";
  if (state.expired) return AUTHORITY_EXPIRED;
  const hours = Math.floor(state.remainingMs / 3_600_000);
  if (hours >= 1) return `Billing window open for about ${hours} more hour${hours === 1 ? "" : "s"}.`;
  const minutes = Math.max(1, Math.round(state.remainingMs / 60_000));
  return `Billing window closes in about ${minutes} minute${minutes === 1 ? "" : "s"}.`;
}
