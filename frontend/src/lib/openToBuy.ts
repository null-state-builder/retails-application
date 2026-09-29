/** Open-to-buy (store operations ticket 39, ST-BUY-1).
 *
 *  A buying budget at cost per brand, season and site, or for the whole company.
 *  Open-to-buy is the budget less open bookings less goods received; the server
 *  works out every figure. A booking over it is confirmed only after the Owner
 *  approves it in the approvals inbox. This file turns what the Owner types into
 *  the request, and says in words where things stand. */
import type { ApiRead, ApiSchemas } from "./api";
import { rupeesToPaise } from "./format";

export type OtbList = ApiRead<ApiSchemas["OtbList"]>;
export type OtbBudget = ApiRead<ApiSchemas["OtbBudget"]>;
export type OtbAsk = ApiRead<ApiSchemas["OtbAsk"]>;
export type OtbCheck = ApiRead<ApiSchemas["OtbBookingCheck"]>;
export type OtbOver = ApiRead<ApiSchemas["OtbOver"]>;

export const STAGE_LABEL: Record<string, string> = {
  not_asked: "Not asked yet",
  waiting: "Waiting for the Owner",
  approved: "Approved by the Owner",
  rejected: "Turned down by the Owner",
  stale: "The draft changed after the Owner was asked",
};

/** The chip class beside the words: never colour alone. */
export const STAGE_CHIP: Record<string, string> = {
  not_asked: "chip-amber",
  waiting: "chip-amber",
  approved: "chip-green",
  rejected: "chip-red",
  stale: "chip-amber",
};

/** What the buyer does next, in one sentence. */
export function nextStep(check: Pick<OtbCheck, "over" | "stage" | "applies">): string {
  if (!check.applies) return "Open-to-buy does not apply to this booking.";
  if (!check.over) return "It fits within open-to-buy. Confirm it.";
  switch (check.stage) {
    case "waiting":
      return "The Owner approves or turns it down in the approvals inbox.";
    case "approved":
      return "The Owner approved this draft. Confirm it now.";
    case "rejected":
      return "The Owner turned it down. Change the booking, or ask again.";
    case "stale":
      return "The draft changed after the Owner decided. Ask the Owner again.";
    default:
      return "It goes over open-to-buy. Ask the Owner to approve it, then confirm it.";
  }
}

/** Where a budget is for, in words. */
export function scopeName(budget: Pick<OtbBudget, "site">): string {
  return budget.site ? `${budget.site.code} · ${budget.site.name}` : "Whole company";
}

/** Whether what is left has gone below zero. Read as text, never through a float. */
export function isOver(openToBuyPaise: string): boolean {
  return openToBuyPaise.trim().startsWith("-");
}

/** The Owner's typed budget in rupees -> whole paise as text; null when it is not money. */
export function budgetPaise(typed: string): string | null {
  const paise = rupeesToPaise(typed);
  return paise === null ? null : String(paise);
}

/** One press of Save: which budget, at which revision, with what amount. */
export interface Attempt {
  key: string;
  revision: number | null;
  paise: string;
}
export interface Pending extends Attempt {
  commandId: string;
}

/** The command identity for `attempt`. Pressing the very same Save again after a
 *  dropped connection replays the command it was sent under, so the server never
 *  sets the budget twice. Anything else is a new command. */
export function commandIdFor(
  pending: Pending | null,
  attempt: Attempt,
  fresh: () => string,
): string {
  const same =
    pending !== null &&
    pending.key === attempt.key &&
    pending.revision === attempt.revision &&
    pending.paise === attempt.paise;
  return same ? pending.commandId : fresh();
}

/** The key naming one budget: brand, season and site (or company). */
export function budgetKey(brandId: string, seasonId: string, siteId: string): string {
  return `${brandId}:${seasonId}:${siteId || "company"}`;
}
