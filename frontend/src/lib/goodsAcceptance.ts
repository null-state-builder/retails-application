// Acceptance and putaway (ticket 07): the wire shapes E139-E142 answer, and the
// pure rules the "Accept goods" screen needs — grouping scans for the running
// tally, and building the closed `AcceptanceScanInput` the server takes.
//
// Nothing here decides eligibility, resolves a tag to a line, or posts stock:
// those are the server's own answers (`stockledger/goods_acceptance.py`). What
// lives here is what the screen does with what it typed and what the server
// acknowledged.
import type { paths } from "./api-schema";

/** The four outcomes a scan may post (`stockledger.goods_acceptance.OUTCOMES`).
 *
 *  There is no fifth "extra" outcome and no fourth condition on the wire — the
 *  backend's `CONDITIONS` tuple is exactly `good`/`damaged`/`wrong` (GSA-T07).
 *  More pieces than the official line, or a tag the PT does not carry, is
 *  refused (`EXCEEDS_REMAINING` / `NOT_ON_PT`, see `extraPieces`) and the screen
 *  guides the person back to the goods receipt instead (ticket 07B). */
export type Outcome = "checked_good" | "accepted_good" | "damaged" | "wrong";
export type Condition = "good" | "damaged" | "wrong";

export const CONDITIONS: Condition[] = ["good", "damaged", "wrong"];

export const CONDITION_LABEL: Record<Condition, string> = {
  good: "Good",
  damaged: "Damaged",
  wrong: "Wrong tag",
};

export const CONDITION_HELP: Record<Condition, string> = {
  good: "Matches the official line. Checked, and put away when a location is chosen.",
  damaged: "Goes to quarantine under a damage hold. No location choice — the system decides.",
  wrong: "Not what this PT expects. Recorded as a discrepancy; no stock is created.",
};

/** `AcceptanceDTO.lines.items[]` (design §6.1, `goods_acceptance.line_progress`). */
export interface AcceptanceLineRow {
  official_line_id: string;
  line_key: string;
  alias_as_used: string | null;
  mrp_paise: string | null;
  expected_qty: number;
  checked_qty: number;
  accepted_qty: number;
  damaged_qty: number;
  remaining_qty: number;
  eligibility_reasons: string[];
  label_evidence_ids: string[];
}

/** `AcceptanceCorrectionRoute` (ticket 07B), read off the generated client so
 *  a field the server adds or drops cannot drift from what the screen shows:
 *  where extra pieces go back to, whether this person may start that
 *  correction, and the owned handoff if anyone made one. Null for a PT with no
 *  goods receipt (opening stock). */
type AcceptanceResource =
  paths["/api/goods-v1/stockledger/acceptance-sessions/{id}"]["get"]["responses"][200]["content"]["application/json"];
export type CorrectionRoute = NonNullable<NonNullable<AcceptanceResource["data"]>["correction"]>;

/** `AcceptanceDTO` (E139-E142). */
export interface AcceptanceData {
  session_id: string;
  source_version_id: string;
  state: "open" | "completed" | string;
  acknowledged_scan_keys: string[];
  lines: { items: AcceptanceLineRow[]; next_cursor: string | null; total: number };
  correction: CorrectionRoute | null;
}

/** `PendingAcceptanceDTO` (design §6.2, E248): one official version with
 *  acceptance work still open at a site this person may accept at.
 *
 *  This is the whole of what a receiver needs to find their own work, and
 *  deliberately the whole of what they are told: the official version to open,
 *  which PT it is, where, how much is left and when it last moved. No rows, no
 *  costs, no exception detail, and no authority over PTs at all (GSA-T07). */
export interface PendingAcceptanceRow {
  official_version_id: string;
  pt_id: string;
  pt_number: string | null;
  site_id: string;
  remaining_qty: number;
  updated_at: string;
}

export function remainingTotal(lines: AcceptanceLineRow[]): number {
  return lines.reduce((total, line) => total + line.remaining_qty, 0);
}

// ---------------------------------------------------------------------------
// Building a scan
// ---------------------------------------------------------------------------

/** `AcceptanceScanInput` (design §6.1). */
export interface ScanInput {
  scan_key: string;
  outcome: Outcome;
  condition: Condition;
  qty: number;
  alias_value: string;
  observed_ticket_mrp_paise: string | null;
  official_line_id: string | null;
  label_evidence_id: string | null;
  chosen_sku_id: string | null;
  location_id: string | null;
  actual_at: string;
}

export function outcomeFor(condition: Condition, putaway: boolean): Outcome {
  if (condition === "damaged") return "damaged";
  if (condition === "wrong") return "wrong";
  return putaway ? "accepted_good" : "checked_good";
}

/** One scan, held client-side from the moment it is queued to the moment the
 *  server's `acknowledged_scan_keys` names it (design §8.4: unacknowledged
 *  input is pending work, never "recorded"). `error` carries a business
 *  refusal (e.g. `TAG_MISMATCH`); a network failure leaves the scan `pending`
 *  and unexplained, which is what the offline banner reads. */
export interface QueuedScan {
  key: string;
  input: ScanInput;
  lineLabel: string;
  status: "pending" | "recorded" | "error";
  error?: string;
}

/** The running tally groups only rows sharing official line, tag, MRP,
 *  condition and location (GSA-T07) — never a looser match that would hide a
 *  real difference between two scans. */
export interface ScanGroup {
  key: string;
  lineLabel: string;
  alias: string;
  mrp: string | null;
  condition: Condition;
  outcome: Outcome;
  locationId: string | null;
  qty: number;
  recordedQty: number;
  pendingQty: number;
  errorQty: number;
}

export function groupScans(scans: QueuedScan[]): ScanGroup[] {
  const groups = new Map<string, ScanGroup>();
  for (const scan of scans) {
    const key = [
      scan.input.official_line_id ?? "",
      scan.input.alias_value,
      scan.input.observed_ticket_mrp_paise ?? "",
      scan.input.condition,
      scan.input.location_id ?? "",
    ].join("|");
    const existing = groups.get(key);
    const group: ScanGroup = existing ?? {
      key,
      lineLabel: scan.lineLabel,
      alias: scan.input.alias_value,
      mrp: scan.input.observed_ticket_mrp_paise,
      condition: scan.input.condition,
      outcome: scan.input.outcome,
      locationId: scan.input.location_id,
      qty: 0,
      recordedQty: 0,
      pendingQty: 0,
      errorQty: 0,
    };
    group.qty += scan.input.qty;
    if (scan.status === "recorded") group.recordedQty += scan.input.qty;
    else if (scan.status === "pending") group.pendingQty += scan.input.qty;
    else group.errorQty += scan.input.qty;
    groups.set(key, group);
  }
  return [...groups.values()];
}

// ---------------------------------------------------------------------------
// Refusal issues (`core.refusals.issue`)
// ---------------------------------------------------------------------------

export interface ApiIssue {
  code: string;
  message: string;
  field?: string;
  line_key?: string;
  quantity?: number;
}

export function apiErrorIssues(e: unknown): ApiIssue[] {
  const details = (e as { response?: { data?: { details?: { issues?: ApiIssue[] } } } })?.response
    ?.data?.details;
  return details?.issues ?? [];
}

/** The refusal codes that mean "more pieces than this PT covers" (ticket 07B):
 *  more than a line has left, or a tag no line of this PT carries. Neither is
 *  accepted here; both go back to the goods receipt's count. */
export const EXTRA_ISSUES = ["EXCEEDS_REMAINING", "NOT_ON_PT"] as const;

/** Which scans of a refused batch were extra pieces, by their place in the batch. */
export function extraPieces(issues: ApiIssue[]): Map<number, ApiIssue> {
  const out = new Map<number, ApiIssue>();
  for (const found of issues) {
    if (!(EXTRA_ISSUES as readonly string[]).includes(found.code)) continue;
    const m = /^observations\[(\d+)\]/.exec(found.field ?? "");
    if (m) out.set(Number(m[1]), found);
  }
  return out;
}

/** What the extra-pieces panel offers this person: the way back itself, a
 *  handoff to whoever owns it, the handoff already open, or nothing to correct
 *  (a PT with no goods receipt). Decided by the server's `can_correct` and
 *  `handoff`, never by the screen's own reading of a grant. A handoff whose
 *  extra was since decided is history: new extra pieces are handed over again. */
export type ExtraRoute = "correct" | "hand_over" | "handed_over" | "none";

export function extraRoute(correction: CorrectionRoute | null): ExtraRoute {
  if (!correction) return "none";
  if (correction.can_correct) return "correct";
  return correction.handoff?.state === "open" ? "handed_over" : "hand_over";
}
