// Non-trading stock counts (goods ticket 17) - the wire shapes the Counts
// screens share, as the generated client states them, and the few pure reading
// rules over them.
//
// Nothing here decides anything. Whether a site may be counted, what a pass
// covers, whether a count matches the book and whether it may close are the
// server's answers (`outbound/goods_counts.py`). This file only names them for a
// reader - and never turns "differences pending" into anything that reads as a
// finished count.

import type { operations } from "./api-schema";

type Json<T> = T extends { responses: { 200: { content: { "application/json": infer B } } } }
  ? B
  : never;

/** One count as a list row (E102). */
export type CountSummary = Json<operations["goods_v1_outbound_stocktakes_list"]>["items"][number];
/** One count (E103). */
export type CountDetail = Json<operations["goods_v1_outbound_stocktakes_detail"]>;
export type CountPassRow = CountDetail["passes"][number];
/** One blind pass (the counter's own, or a reviewer's read). */
export type CountPass = Json<operations["goods_v1_outbound_count_sessions_detail"]>;
export type CountObservation = CountPass["observations"][number];
/** The reviewer's quantities-only variance (E163). */
export type CountVariance = Json<operations["goods_v1_outbound_stocktakes_variance"]>;
export type CountVarianceLine = CountVariance["lines"]["items"][number];
/** The blind lookup's answer (E211): identity only. */
export type CountLookup = Json<operations["goods_v1_outbound_count_lookup"]>;

type Body<T> = T extends { requestBody?: { content: { "application/json": infer B } } } ? B : never;
export type CountStartBody = Body<operations["goods_v1_outbound_stocktakes_start"]>;
export type CountScanBody = Body<operations["goods_v1_outbound_count_sessions_scan"]>;
export type CountScanInput = CountScanBody["observations"][number];
export type CountSubmitBody = Body<operations["goods_v1_outbound_count_sessions_submit"]>;
export type CountRecountBody = Body<operations["goods_v1_outbound_stocktakes_recount"]>;
export type CountCloseBody = Body<operations["goods_v1_outbound_stocktakes_close"]>;
export type CountCancelBody = Body<operations["goods_v1_outbound_stocktakes_cancel"]>;

export type CountCondition = CountScanInput["condition"];
export const CONDITIONS: CountCondition[] = ["good", "damaged", "wrong", "unidentified"];
export const CONDITION_LABEL: Record<CountCondition, string> = {
  good: "Good",
  damaged: "Damaged",
  wrong: "Wrong item",
  unidentified: "Cannot identify",
};

export const STATE_LABEL: Record<string, string> = {
  requested: "Requested",
  open: "Open - site frozen",
  review: "In review",
  closed: "Closed",
  cancelled: "Cancelled",
};

export const PASS_STATE_LABEL: Record<string, string> = {
  open: "Counting",
  submitted: "Submitted",
  selected: "Used for the result",
  superseded: "Not used",
};

/** What the screen says about where a count stands. A count whose counted
 *  quantities differ from the book is *pending*, never "done": the differences
 *  wait for review and the Owner's approval, and the site stays frozen. */
export function progressWords(progress: CountDetail["progress"]): { title: string; body: string } {
  switch (progress) {
    case "counting":
      return {
        title: "Counting",
        body:
          "The site is frozen: no stock moves, is sent, received or put away until this count ends. " +
          "Damage can still be reported, and goes to quarantine at once.",
      };
    case "awaiting_review":
      return {
        title: "Waiting for review",
        body: "Every pass so far is submitted. A reviewer checks the counted quantities against the book.",
      };
    case "incomplete":
      return {
        title: "Not every location is counted once",
        body: "Some locations have no submitted pass, or two passes cover the same place. Count them, or choose one pass per place.",
      };
    case "differences_pending":
      return {
        title: "Differences found - not complete",
        body:
          "What was counted differs from the book. The count is not finished: the differences wait for " +
          "review and the Owner's approval, the site stays frozen, and no stock has changed.",
      };
    case "matches_book":
      return {
        title: "Counted matches the book",
        body: "It can be closed. Closing changes no stock and lifts the freeze.",
      };
    case "closed_adjusted":
      return {
        title: "Closed after independent review",
        body: "The approved original-cost shortages were posted once. The store freeze is lifted; resume the counter when ready.",
      };
    case "closed_matching":
      return {
        title: "Closed - counted matched the book",
        body: "Nothing was posted. The freeze is lifted.",
      };
    case "cancelled":
      return {
        title: "Cancelled",
        body: "Everything counted is kept. No stock changed. The freeze is lifted.",
      };
  }
}

export function scopeWords(scope: CountSummary["scope"]): string {
  const kind = scope.count_kind === "full" ? "Full count" : "Cycle count";
  if (scope.kind === "location") return `${kind} of ${scope.location_name ?? "one location"}`;
  if (scope.kind === "brand") return `${kind} of ${scope.brand_name ?? "one brand"}`;
  return `${kind} of the whole site`;
}

/** A readable name for a counted piece: its item, else what the counter wrote. */
export function itemWords(row: {
  item: string;
  description: string;
  sku_id: string | null;
}): string {
  if (row.item) return row.item;
  if (row.description) return row.description;
  return row.sku_id ?? "Unknown item";
}

/** A lookup's candidate, in words. */
export function candidateWords(candidate: CountLookup["candidates"][number]): string {
  return [candidate.brand, candidate.style, candidate.size, candidate.colour, candidate.grade]
    .filter(Boolean)
    .join(" · ");
}

/** Signed difference, as a person reads it: "+2", "-3", "0", or "not counted". */
export function deltaWords(delta: number | null): string {
  if (delta === null) return "not counted";
  if (delta > 0) return `+${delta}`;
  return String(delta);
}

/** The lines a reviewer has ticked for a recount, all from one pass - the server
 *  refuses a recount that spans passes, so the screen says so first. */
export function recountSource(
  lines: CountVarianceLine[],
  ticked: Set<string>,
): {
  passId: string | null;
  mixed: boolean;
} {
  const sources = new Set(
    lines.filter((line) => ticked.has(line.line_key)).map((line) => line.pass_id ?? ""),
  );
  if (sources.size === 0) return { passId: null, mixed: false };
  if (sources.size > 1 || sources.has("")) return { passId: null, mixed: true };
  return { passId: [...sources][0] ?? null, mixed: false };
}
