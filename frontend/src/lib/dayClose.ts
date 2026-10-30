// Closing the store's business day (R-POS-011 / R-FIN-015).
//
// The server says which of the five steps are open as it sees them
// (`GET /api/sell/day-close`). The counter adds what only it knows: the bills
// still queued on this device and the carts parked on it. This module joins the
// two into the list the page draws - pure, so the rule "Close the day stays off
// while any step is open, and the open step says what to do" is one function
// with tests rather than a set of conditions spread through a screen.

import type { ApiSchemas } from "./api";
import { formatINR } from "./format";

export type DayCloseState = ApiSchemas["DayCloseState"];
export type StepKey = "synced" | "held" | "cash" | "summary" | "close";
/** done, the one to do now ("where you are"), or still to come. */
export type StepStatus = "done" | "now" | "open";

export interface StepLink {
  to: string;
  label: string;
}

export interface Step {
  key: StepKey;
  title: string;
  status: StepStatus;
  text: string;
  link: StepLink | null;
}

/** What this counter knows that head office does not, yet. */
export interface CounterView {
  /** The counter's local database has been read at least once. */
  ready: boolean;
  counterId: string;
  /** Bills queued on this device, not yet accepted by head office. */
  pending: number;
  /** The queue stopped on a bill head office refused. */
  halt: { doc_number: string; message: string } | null;
  /** An online bill whose outcome is not known yet. */
  onlinePending: boolean;
  /** Carts parked on this counter that the store has not kept for tomorrow. */
  heldOpen: number;
}

export const STEP_ORDER: StepKey[] = ["synced", "held", "cash", "summary", "close"];

export const STEP_TITLES: Record<StepKey, string> = {
  synced: "All bills synced",
  held: "Held bills cleared",
  cash: "Cash counted",
  summary: "Day summary checked",
  close: "Close the day",
};

export const STEP_LINKS: Record<StepKey, StepLink | null> = {
  synced: { to: "/sell/till", label: "Till & sync" },
  held: { to: "/sell", label: "Billing" },
  cash: { to: "/sell/cash-count", label: "Cash count" },
  summary: { to: "/reports/day-summary", label: "Day summary" },
  close: null,
};

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** A business day in words, as the day it is in India: "Thu 29 Oct 2026". */
export function dayWords(ymd: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  }).formatToParts(new Date(`${ymd}T00:00:00+05:30`));
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("weekday")} ${part("day")} ${part("month")} ${part("year")}`;
}

/** The time of an instant in India, on the 24-hour clock: "21:31". */
export function timeWords(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Holds that still need an answer: everything the store has not kept. */
export function openHolds(holds: { expires_policy: string }[]): number {
  return holds.filter((hold) => hold.expires_policy !== "kept").length;
}

function serverStep(state: DayCloseState, key: StepKey) {
  return state.steps.find((step) => step.key === key);
}

function syncedText(state: DayCloseState, counter: CounterView): [boolean, string] {
  const id = counter.counterId || state.counter_id;
  const name = id ? `Counter ${id}` : "This counter";
  if (!state.closed) {
    if (!counter.ready) return [false, "Reading this counter's queue…"];
    if (counter.halt) {
      return [
        false,
        `Bill ${counter.halt.doc_number} was not accepted: ${counter.halt.message} Sort it out on Till & sync, then come back.`,
      ];
    }
    if (counter.pending > 0) {
      return [
        false,
        `${name} has ${plural(counter.pending, "bill", "bills")} waiting. Sync ${id || "it"}, then come back.`,
      ];
    }
    if (counter.onlinePending) {
      return [
        false,
        "A bill is still being finalised with head office. Finish it on Billing, then come back.",
      ];
    }
  }
  const server = serverStep(state, "synced");
  if (server && !server.done) return [false, server.message];
  const upTo = state.last_doc_number ? `, up to ${state.last_doc_number}` : "";
  return [true, `0 waiting on ${name}. Head office accepted every bill${upTo}.`];
}

function heldText(state: DayCloseState, counter: CounterView): [boolean, string] {
  const kept = state.held_kept
    ? ` ${plural(state.held_kept, "bill", "bills")} kept for tomorrow ${state.held_kept === 1 ? "carries" : "carry"} over.`
    : "";
  if (state.closed) return [true, `No held bill was open at the close.${kept}`];
  const open = Math.max(state.held_open, counter.ready ? counter.heldOpen : 0);
  if (open > 0) {
    return [
      false,
      `${plural(open, "bill is", "bills are")} on hold. Resume or let go of each on Billing, then come back.`,
    ];
  }
  return [true, `No held bill on the counter.${kept}`];
}

function cashText(state: DayCloseState): [boolean, string] {
  const server = serverStep(state, "cash");
  const count = state.count;
  if (!count) return [false, server?.message || "The drawer is not counted yet."];
  const who = count.counted_by_name ? ` by ${count.counted_by_name}` : "";
  const variance = count.variance_paise;
  const match =
    variance === 0
      ? "Matches the expected cash."
      : `${variance < 0 ? "Short" : "Over"} by ${formatINR(Math.abs(variance))}, confirmed by ${count.approved_by_name || "a manager"}.`;
  const later = state.bills_after_count
    ? ` ${plural(state.bills_after_count, "bill", "bills")} after the count ${state.bills_after_count === 1 ? "goes" : "go"} into the next count.`
    : "";
  return [
    true,
    `${formatINR(count.counted_paise)} counted at ${timeWords(count.counted_at)}${who}. ${match}${later}`,
  ];
}

function summaryText(state: DayCloseState): [boolean, string] {
  const server = serverStep(state, "summary");
  const record = state.record;
  if (server && !server.done) return [false, server.message];
  if (!record?.summary_checked_at) return [false, "Read the Day summary and tick it as checked."];
  const who = record.summary_checked_by_name ? ` by ${record.summary_checked_by_name}` : "";
  return [
    true,
    `Sales, returns and tenders read through. Ticked${who} at ${timeWords(record.summary_checked_at)}.`,
  ];
}

function closeText(state: DayCloseState): string {
  const next = dayWords(state.next_business_day);
  const record = state.record;
  if (state.closed && record?.closed_at) {
    const by = record.closed_by_name ? ` by ${record.closed_by_name}` : "";
    const confirmed = record.confirmed_by_name ? `, confirmed by ${record.confirmed_by_name}` : "";
    return `Closed at ${timeWords(record.closed_at)}${by}${confirmed}. Counters now bill into ${next}; this day's count and summary are final.`;
  }
  return `A manager confirms with their own PIN. After this, every counter bills into ${next}, and today's count and summary are final.`;
}

/**
 * The five steps as the page draws them.
 *
 * A step is done only when both sides agree it is: the server for what it holds,
 * the counter for its own queue and parked carts. The first step not done is
 * the one to do now; Close the day is "now" only once the four above it are done.
 */
export function deriveSteps(state: DayCloseState, counter: CounterView): Step[] {
  const answers: Record<Exclude<StepKey, "close">, [boolean, string]> = {
    synced: syncedText(state, counter),
    held: heldText(state, counter),
    cash: cashText(state),
    summary: summaryText(state),
  };
  let found = false;
  return STEP_ORDER.map((key) => {
    const [done, text] = key === "close" ? [state.closed, closeText(state)] : answers[key];
    let status: StepStatus = "open";
    if (done) status = "done";
    else if (!found) {
      status = "now";
      found = true;
    }
    return { key, title: STEP_TITLES[key], status, text, link: STEP_LINKS[key] };
  });
}

/** Close the day is offered only with every step above it done, online. */
export function canClose(steps: Step[], state: DayCloseState, online: boolean): boolean {
  return (
    online &&
    !state.closed &&
    steps.filter((step) => step.key !== "close").every((step) => step.status === "done")
  );
}

/** The sentence beside a disabled Close the day: the first open step's. */
export function whyNotYet(steps: Step[], state: DayCloseState, online: boolean): string {
  if (state.closed) return "";
  const open = steps.find((step) => step.key !== "close" && step.status !== "done");
  if (open) return open.text;
  if (!online)
    return "You are offline. The day closes at head office, so this waits for the connection.";
  return "";
}
