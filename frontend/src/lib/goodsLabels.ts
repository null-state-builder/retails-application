// Labels and print jobs (ticket 11, E182-E184, GSA-T11). Wire shapes and the
// pure grouping/labelling rules `GoodsLabels.tsx` needs — nothing here
// recomputes a server decision (the rendered symbol, the layout fit check):
// what lives here is how a reader groups and labels what the server already
// decided.

export interface PrintLabel {
  official_line_id: string;
  alias_as_used: string;
  mrp_paise: string;
  copies: number;
  svg: string;
}

export interface PrintEventRow {
  id: string;
  outcome: PrintOutcomeKind;
  actor_id: string | null;
  recorded_at: string;
  usable_counts: { official_line_id: string; qty: number | null }[];
  reason_code: string | null;
  // The sample scan-back (E183 step 9), present only on a `scan_verified`
  // event. A recorded scan always matched: the server refuses a mismatch with
  // PRINT_VERIFY_FAILED rather than storing it.
  scanned_alias: string | null;
  matched_line_id: string | null;
}

export interface PrintJobData {
  pt_version_id: string;
  site_id: number;
  state: PrintJobStatus;
  template_version_id: string | null;
  reprint_of_id: string | null;
  reason_code: string | null;
  labels: PrintLabel[];
  events: PrintEventRow[];
}

export type PrintJobStatus = "prepared" | "attempted" | "confirmed" | "partial" | "failed" | "unknown";
export type PrintOutcomeKind =
  | "attempted"
  | "confirmed"
  | "partial"
  | "failed"
  | "unknown"
  | "scan_verified";

export const OUTCOME_OPTIONS: { value: PrintOutcomeKind; label: string }[] = [
  { value: "confirmed", label: "Confirmed — every copy printed" },
  { value: "partial", label: "Partial — only some copies printed" },
  { value: "failed", label: "Failed — nothing printed" },
  { value: "unknown", label: "Unknown — cannot tell yet" },
];

//: `confirmed`/`partial` are the two outcomes GSA-T11 requires a full per-line
//: physical count for; a dialog "attempted" alone never confirms a page printed.
const COUNT_REQUIRED_OUTCOMES = new Set<PrintOutcomeKind>(["confirmed", "partial"]);
//: `failed`/`unknown` may also carry counts — §5.8: "a known shortfall permits
//: partial/failed as appropriate". A printer that failed still leaves a
//: countable number of usable labels, and that count is what sizes the reprint.
const COUNT_OPTIONAL_OUTCOMES = new Set<PrintOutcomeKind>(["failed", "unknown"]);

export function needsUsableCounts(outcome: PrintOutcomeKind): boolean {
  return COUNT_REQUIRED_OUTCOMES.has(outcome);
}

/** Whether this outcome may carry a per-line count at all. Offering the count
 *  table for `failed` is what makes a failed job reprintable: without a counted
 *  zero there is no evidenced shortfall for a reprint to be limited to. */
export function allowsUsableCounts(outcome: PrintOutcomeKind): boolean {
  return COUNT_REQUIRED_OUTCOMES.has(outcome) || COUNT_OPTIONAL_OUTCOMES.has(outcome);
}

/** Every sample scan this job has had verified, oldest first. A scan proves
 *  one sample, never the requested copies, so these are listed beside the
 *  counted outcome rather than folded into it (GSA-T11, design §5.8). */
export function verifiedScans(data: PrintJobData): PrintEventRow[] {
  return data.events.filter((event) => event.outcome === "scan_verified");
}

/** The most recently recorded outcome, or `null` if the job has none yet.
 *  Trusts the server's own order (`print_job_data`: `.order_by("recorded_at",
 *  "pk")`, ascending) rather than re-sorting here - a client-side re-sort by
 *  `recorded_at` alone would only coincidentally match that same-instant
 *  tie-break, by relying on `Array.prototype.sort`'s stability to preserve
 *  the server's own `pk` order underneath it. */
export function latestEvent(job: PrintJobData): PrintEventRow | null {
  return job.events.at(-1) ?? null;
}

/** The newest event that actually counted something — §5.8's "reviewed prior
 *  outcome" that a missing-copy reprint pins, and the server's own rule in
 *  `goods_print_services._latest_counted_event`.
 *
 *  Recognised by shape, not by outcome name: an `attempted` (a browser dialog)
 *  and a `scan_verified` (one sample) carry no counts, so reading the plain
 *  latest event would turn an evidenced shortfall back into "unknown" and hide
 *  the reprint the operator just earned. An `unknown` does carry a full list of
 *  nulls, so a correction to "I cannot vouch for this" rightly supersedes an
 *  earlier `partial`. */
export function latestCountedEvent(job: PrintJobData): PrintEventRow | null {
  for (let i = job.events.length - 1; i >= 0; i -= 1) {
    const event = job.events[i];
    if (event.usable_counts.length > 0) return event;
  }
  return null;
}

/** Per line: requested copies minus the last known usable count, floored at
 *  zero. `null` means the usable count is unknown and must be checked before
 *  a reprint can be limited to it (GSA-T11). */
export function missingByLine(job: PrintJobData): Map<string, number | null> {
  const requested = new Map(job.labels.map((label) => [label.official_line_id, label.copies]));
  const missing = new Map<string, number | null>();
  const last = latestCountedEvent(job);
  for (const [lineId, copies] of requested) {
    const entry = last?.usable_counts.find((c) => c.official_line_id === lineId);
    if (!entry || entry.qty === null || entry.qty === undefined) {
      missing.set(lineId, null);
    } else {
      missing.set(lineId, Math.max(copies - entry.qty, 0));
    }
  }
  return missing;
}

/** Whether every requested line still has a strictly positive, evidenced
 *  missing quantity — the only case a reprint has anything left to reprint. */
export function hasReprintableShortfall(job: PrintJobData): boolean {
  return [...missingByLine(job).values()].some((qty) => qty !== null && qty > 0);
}

export function statusLabel(state: PrintJobStatus | PrintOutcomeKind): string {
  switch (state) {
    case "prepared":
      return "Rendered, not yet attempted";
    case "attempted":
      return "Sent to the printer";
    case "confirmed":
      return "Confirmed printed";
    case "partial":
      return "Partially printed";
    case "failed":
      return "Failed to print";
    case "unknown":
      return "Outcome unknown";
    case "scan_verified":
      return "A sample scanned back";
    default:
      return state;
  }
}
