// The receiving slice's own wire shapes and the few rules its three screens
// share (ticket 05): bookings, arrivals and counting, and the GRN detail.
//
// Everything here is pure. Authority, eligibility and the invoice pairing are
// the server's answers and are never recomputed on this side — what lives here
// is what a *reader* is owed: an absent optional is "not given", an unknown
// amount is "Unknown", and a held piece can always say what is holding it.

import type { paths } from "./api-schema";

/** The four ways a piece can be counted (`inbound.goods_services.CONDITIONS`). */
export const CONDITIONS = ["good", "damaged", "wrong", "unidentified"] as const;
export type Condition = (typeof CONDITIONS)[number];

export const CONDITION_LABEL: Record<Condition, string> = {
  good: "Good",
  damaged: "Damaged",
  wrong: "Wrong goods",
  unidentified: "Cannot identify",
};

/** What each condition means for the pieces, in the words a receiver uses. */
export const CONDITION_HELP: Record<Condition, string> = {
  good: "Fit to sell. These are the only pieces an ordinary PT can cover.",
  damaged: "Arrived damaged. Goes to quarantine and stays held.",
  wrong: "Not what was ordered. Goes to quarantine and stays held.",
  unidentified: "No tag, or a tag nobody can resolve. Described in words, with no SKU.",
};

/** Conditions whose pieces are held and can never be prefilled into an ordinary
 *  PT (change PRD §14.3). Excess above the invoice is held too, but that is a
 *  quantity comparison rather than a condition — see `heldReasons`. */
export function isHeldCondition(condition: string): boolean {
  return condition !== "good";
}

/** The sites this person's own session names (`/auth/me`), which is the only
 *  site list a receiver, a buyer or a controller can read at all: the masters
 *  site list needs `org.site.manage`, which none of them holds. Naming a site
 *  from the session is both what they are entitled to and what they mean. */
export function siteName(
  sites: { id: string; code: string; name: string }[],
  id: string | null | undefined,
): string {
  if (!id) return NOT_GIVEN;
  const found = sites.find((site) => String(site.id) === String(id));
  return found ? `${found.name} (${found.code})` : `#${id}`;
}

// ---------------------------------------------------------------------------
// Bookings
// ---------------------------------------------------------------------------

export interface BookingLine {
  line_key: string;
  ordered_qty: number;
  received_qty: number;
  reversed_qty: number;
  outstanding_qty: number;
  style_code: string | null;
  description?: string | null;
  size_value_id: string | null;
  /** The governed size's words ("Medium"); null when no size was given. */
  size_label?: string | null;
  colour_value_id: string | null;
  colour_label?: string | null;
  mrp_paise: string | null;
  /** Indicative cost per piece. Absent (not null) without the `cost` field grant. */
  cost_paise?: string | null;
  destination_site_id: string | null;
}

export interface BookingHeader {
  vendor_id?: string;
  brand_id?: string;
  season_id?: string;
  entity_id?: string;
  destination_site_id?: string | null;
  commercial_label?: string | null;
  vendor_ref?: string | null;
  expected_date?: string | null;
  notes?: string | null;
}

export interface BookingProgress {
  booking_header: BookingHeader;
  /** The names of the header's vendor, brand, season and site. */
  names?: {
    vendor?: string | null;
    brand?: string | null;
    season?: string | null;
    site?: string | null;
  };
  lines: { items: BookingLine[]; next_cursor: string | null; total: number };
}

/** A booking draft's own lines, as POST/PATCH answer them (not the progress read). */
export interface BookingDraftLine {
  line_key: string;
  style_code: string;
  description?: string | null;
  /** The size as typed ("M", "Medium"); the server keeps the governed value. */
  size?: string | null;
  size_value_id?: string | null;
  colour_value_id?: string | null;
  qty: number;
  destination_site_id?: string | null;
  mrp_paise?: string | null;
  cost_paise?: string | null;
}

/** An optional field that was never given. Not a zero, not a blank cell —
 *  a booking line with no MRP is a line whose MRP nobody stated. */
export const NOT_GIVEN = "Not given";

export function orNotGiven(value: string | null | undefined): string {
  return value === null || value === undefined || value === "" ? NOT_GIVEN : value;
}

export function outstandingTotal(lines: BookingLine[]): number {
  return lines.reduce((total, line) => total + line.outstanding_qty, 0);
}

// ---------------------------------------------------------------------------
// Arrivals and counting
// ---------------------------------------------------------------------------

export interface ArrivalData {
  site_id: string;
  vendor_id: string;
  brand_id: string;
  subbrand_key: string | null;
  actual_arrival_at: string;
  transporter_ref: string | null;
  booking_id: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  invoice_evidence_id: string | null;
  /** Present only when this arrival was recorded against a duplicate-invoice
   *  warning: the answer given to it. The matched records are not part of it —
   *  a reader of this arrival may reach fewer sites than the recorder did. */
  duplicate_acknowledgement: {
    warning_hash: string;
    reason: string;
    actor_id: string | null;
    recorded_at: string;
  } | null;
}

export interface ArrivalSummary {
  id: string;
  state: string;
  site_id: string;
  brand_id: string | null;
  created_at: string;
}

export interface CountSessionData {
  arrival_id: string;
  counter_id: string;
  entry_user_id: string;
  state: string;
  revision: number;
  grn_id: string | null;
  acknowledged_scan_keys: string[];
  /** The durable rows themselves. A row's `id` is what an E091 identity pick
   *  binds to when a scanned code matched several products (design E091). */
  observations: RecordedObservation[];
  /** Who has held this count, oldest first. `counter_id` and `entry_user_id`
   *  above are only the current owner; this is how it got to them (E240). */
  handovers: CountHandoverRow[];
}

/** One unfinished count passed to another authorised person at the same site. */
export interface CountHandoverRow {
  id: string;
  session_id: string;
  from_human_id: string;
  to_human_id: string;
  actor_id: string | null;
  recorded_at: string;
  reason_code: string;
}

/** One observation exactly as the server holds it. */
export interface RecordedObservation {
  id: string;
  scan_key: string;
  sku_id: string | null;
  alias_value: string | null;
  description: string;
  condition: Condition;
  qty: number;
  correction_of_id: string | null;
}

/** One row of the count, as this screen holds it before and after the server
 *  acknowledges its scan key (design §8.4: unacknowledged input is pending
 *  work, never "recorded"). */
export interface Observation {
  scan_key: string;
  sku_id: string | null;
  description: string;
  alias_value: string | null;
  condition: Condition;
  qty: number;
}

export function conditionTotals(rows: Observation[]): Record<Condition, number> {
  const totals: Record<Condition, number> = {
    good: 0,
    damaged: 0,
    wrong: 0,
    unidentified: 0,
  };
  for (const row of rows) totals[row.condition] += row.qty;
  return totals;
}

export function countedTotal(rows: Observation[]): number {
  return rows.reduce((total, row) => total + row.qty, 0);
}

/** Arrivals that share a transporter reference travel together and are shown
 *  together — and are still separate arrivals with their own count and GRN
 *  (GSA-T05). Grouping is presentation; nothing here merges anything.
 *  An arrival with no reference is its own group, never pooled with the others. */
export function groupByTransporter<T extends { id: string; transporter_ref?: string | null }>(
  arrivals: T[],
): { reference: string | null; arrivals: T[] }[] {
  const groups: { reference: string | null; arrivals: T[] }[] = [];
  const byReference = new Map<string, { reference: string | null; arrivals: T[] }>();
  for (const arrival of arrivals) {
    const reference = arrival.transporter_ref?.trim() || null;
    if (reference === null) {
      groups.push({ reference: null, arrivals: [arrival] });
      continue;
    }
    const existing = byReference.get(reference);
    if (existing) {
      existing.arrivals.push(arrival);
    } else {
      const group = { reference, arrivals: [arrival] };
      byReference.set(reference, group);
      groups.push(group);
    }
  }
  return groups;
}

/** The server's answer to "is this invoice already recorded here?" (E250).
 *
 *  Taken from the generated client rather than written again here, so a change
 *  to the contract is a type error rather than a screen quietly reading a field
 *  the server stopped sending.
 *
 *  The screen does not match invoice numbers for itself. The server searches
 *  the receiving legal entity, drops every match the reader may not open, and
 *  mints `warning_hash` over exactly the candidates it lists. That hash goes
 *  back with the reason when the arrival is recorded, which is what binds the
 *  reason to the warning it answers — a client-side list could do none of the
 *  three (GSA-T05, design §5.8). No candidates means a null hash and nothing
 *  to acknowledge. */
export type DuplicateWarning =
  paths["/api/goods-v1/inbound/arrivals/duplicate-warning"]["get"]["responses"][200]["content"]["application/json"];

/** One arrival already recorded for the same vendor and invoice number. */
export type DuplicateCandidate = DuplicateWarning["candidates"][number];

// ---------------------------------------------------------------------------
// GRN detail
// ---------------------------------------------------------------------------

export interface ObservedIdentity {
  sku_id: string | null;
  description: string;
  attributes: unknown[];
  raw_alias: string | null;
}

export interface GrnLine {
  line_key: string;
  counted_qty: number;
  covered_qty: number;
  /** Counted pieces no live PT covers yet. Not the same as held (GSA-T05). */
  uncovered_qty: number;
  /** Pieces under an actual hold. Good, uncovered goods are not held. */
  held_qty: number;
  /** Pieces no live PT covers and no hold is on (ticket 07B). */
  unheld_uncovered_qty: number;
  /** Of a wrong or unidentified line, the pieces a different person accepted once
   *  their identity was resolved that no PT covers and no hold is on (ticket 05D):
   *  what a primary or supplement PT may cover. Zero on every other line. */
  accepted_uncovered_qty: number;
  disposed_qty: number;
  /** Of `held_qty`, the pieces held because damage was reported on them — each
   *  under a report a different person decides (ticket 05C). */
  damage_held_qty: number;
  /** How many more pieces on this line damage may still be reported on (E254):
   *  the server's own limit, so the form never offers what it would refuse. */
  damage_reportable_qty: number;
  /** Goods ticket 13E: pieces of this line on the road on a pre-PT custody
   *  transfer, and (of `uncovered_qty`) those now standing at another site. */
  in_transit_qty?: number;
  elsewhere_qty?: number;
  identity: ObservedIdentity;
  condition: Condition;
  discrepancy_remark: string | null;
}

export interface ClaimLine {
  line_key: string;
  style_code: string | null;
  sku_id: string | null;
  size_value_id: string | null;
  description: string;
  claimed_qty: number;
  invoice_basic_paise: string | null;
  invoice_mrp_paise: string | null;
  remark: string | null;
}

export interface Comparison {
  claim_line_key: string;
  claimed_qty: number;
  counted_qty: number;
  difference: number;
  remaining_shortage_qty: number;
  line_keys: string[];
}

export interface CountHistoryEntry {
  document_id: string;
  kind: "grn" | "counter_grn";
  number: string | null;
  state: string;
  recorded_at: string;
  /** A counter-GRN's own head revision — the one its approval request quotes.
   *  Absent on the GRN's own entry, which has no approval of its own. */
  revision?: number;
  lines: Record<string, unknown>[];
}

export interface GrnCoverage {
  grn_header: Record<string, string | null>;
  lines: { items: GrnLine[]; next_cursor: string | null; total: number };
  invoice: {
    claim_revision_id: string;
    revision: number;
    invoice_number: string | null;
    invoice_date: string | null;
    evidence_id: string | null;
    lines: ClaimLine[];
  } | null;
  invoice_comparison: Comparison[];
  count_history: CountHistoryEntry[];
  dispositions: DispositionHistory[];
  damage_reports: GrnDamageReport[];
  /** Goods ticket 13E: the pre-PT custody transfers naming these goods. */
  custody_transfers?: GrnCustodyTransfer[];
}

type GrnCoverageWire = NonNullable<
  paths["/api/goods-v1/inbound/grns/{id}"]["get"]["responses"][200]["content"]["application/json"]["data"]
>;

/** One damage report over this receipt's goods (ticket 05C), read off the
 *  generated client. The receipt only names it and the lines it covers; the
 *  decision itself is the common review's (the Movements screen). */
export type GrnDamageReport = NonNullable<GrnCoverageWire["damage_reports"]>[number];
/** One pre-PT custody transfer that moved some of this receipt's damaged goods
 *  (goods ticket 13E): where to, in which state, how many. */
export type GrnCustodyTransfer = NonNullable<GrnCoverageWire["custody_transfers"]>[number];

export interface DispositionHistory {
  id: string;
  approval_request_id: string | null;
  kind: string;
  state: string;
  source_line_key: string;
  lot_id: string | null;
  qty: number;
  reason_code: string;
  damage_description: string | null;
  damage_evidence_ids: string[];
  resolved_sku_id: string | null;
  source_value_evidence: {
    evidence_id: string | null;
    cost_paise: string | null;
    mrp_paise: string | null;
  };
  tax_basis_evidence_id: string | null;
  requested_value_paise: string | null;
  frozen_value_paise: string | null;
  maker_id: string;
}

/** One owned exception, as E185 answers it. */
export interface ExceptionRow {
  id: string;
  kind: string;
  subject_id: string;
  site_id: string;
  owner_role: string | null;
  owner_human_id: string | null;
  opened_at: string;
  /** Null when this kind's SLA is measured in working days and the business has
   *  approved no working calendar (GSA-T08): no deadline, never an inferred one. */
  due_at: string | null;
  overdue: boolean;
  state: string;
  reason_code: string;
  allowed_resolution_actions: string[];
}

/** The exceptions raised against this GRN, newest cause first. Nothing here
 *  raises one: these are the records the receiving commands already opened
 *  (design §5.7), which is where a held quantity's owner and due date come from. */
export function exceptionsFor(rows: ExceptionRow[], grnId: string): ExceptionRow[] {
  return rows.filter((row) => row.subject_id === `grn:${grnId}` && row.state === "open");
}

/** Why this line's pieces are not free to move on, in the reader's words.
 *
 *  A non-good condition holds the whole line. Good pieces are held when they
 *  are counted above what the invoice claimed and nothing has decided them yet.
 *  "Not yet covered by a PT" is the last of these on purpose: it is the ordinary
 *  state of good goods waiting for their PT, and GSA-T05 forbids calling it a
 *  hold — the server answers it as `uncovered_qty`, never as `held_qty`. */
export function heldReasons(line: GrnLine, excess: number): string[] {
  const reasons: string[] = [];
  if (isHeldCondition(line.condition)) reasons.push(CONDITION_LABEL[line.condition]);
  // Ticket 05C: good pieces somebody reported damaged before their PT are held
  // under that report, whatever the count said about the line.
  if (line.condition === "good" && (line.damage_held_qty ?? 0) > 0) {
    reasons.push("Reported damaged, waiting for review");
  }
  if (excess > 0) reasons.push("More than the invoice claimed");
  if (reasons.length === 0 && line.uncovered_qty > 0) reasons.push("Not yet covered by a PT");
  return reasons;
}

/** Good pieces counted above the invoice's claim, per GRN line.
 *
 *  Derived from the server's own pairing (`invoice_comparison`), never from a
 *  second pairing of our own: a claim group's surplus lands on the lines the
 *  server matched to it. With no invoice there is no claim to exceed. */
export function excessByLine(coverage: GrnCoverage): Record<string, number> {
  const out: Record<string, number> = {};
  const byKey = new Map(coverage.lines.items.map((line) => [line.line_key, line]));
  const matched = new Set<string>();
  for (const comparison of coverage.invoice_comparison) {
    for (const key of comparison.line_keys) matched.add(key);
    if (comparison.difference <= 0) continue;
    let surplus = comparison.difference;
    const good = comparison.line_keys
      .map((key) => byKey.get(key))
      .filter((line): line is GrnLine => Boolean(line) && line!.condition === "good")
      .reverse();
    for (const line of good) {
      if (surplus <= 0) break;
      const take = Math.min(surplus, line.counted_qty - (out[line.line_key] ?? 0));
      out[line.line_key] = (out[line.line_key] ?? 0) + take;
      surplus -= take;
    }
  }
  if (coverage.invoice) {
    for (const line of coverage.lines.items) {
      if (line.condition === "good" && !matched.has(line.line_key)) {
        out[line.line_key] = (out[line.line_key] ?? 0) + line.counted_qty;
      }
    }
  }
  return Object.fromEntries(Object.entries(out).filter(([, qty]) => qty > 0));
}

/** Pieces an ordinary PT may cover: good, counted, not already covered, not
 *  disposed, and not part of an undecided excess. A held quantity is never
 *  offered here — that is the whole point of change PRD §14.3, and the
 *  screen must not be the place it leaks.
 *
 *  `line.uncovered_qty` (`inbound.goods_services.grn_coverage`) is *every*
 *  physical piece this GRN counted that no live PT covers and no disposition
 *  has consumed — "Not on a PT" in the table header above, not a separate
 *  excess-only figure, and not a hold. A good, matched line's entire uncovered
 *  remainder sits there until something covers or disposes it, so it cannot be
 *  subtracted a second time on top of `counted_qty - covered_qty -
 *  disposed_qty`; that arithmetic (ticket 05's original form) zeroes every
 *  coverable line before its first PT ever exists. What a good line's uncovered
 *  quantity must still give up before an ordinary PT is only its own undecided
 *  excess (`excessByLine`, ticket 05 §"Pieces not on a PT") — the pieces
 *  GSA-T06's own GRN picker needs this function to name honestly. */
export function coverableQty(line: GrnLine, excess: number): number {
  if (isHeldCondition(line.condition)) return acceptedWrongQty(line);
  return Math.max(0, line.uncovered_qty - excess);
}

/** Wrong or unidentified pieces a PT may cover: only those a different person
 *  accepted after their identity was resolved (ticket 05D, overall PRD §15.2.1
 *  rule 2) — naming them alone is not enough, and damaged pieces never count.
 *  The server's own figure; a primary and a supplement may both take them. */
export function acceptedWrongQty(line: GrnLine): number {
  if (line.condition !== "wrong" && line.condition !== "unidentified") return 0;
  return line.accepted_uncovered_qty ?? 0;
}

/** What a *supplement* PT may still take from one line (ticket 07B): good pieces
 *  no live PT covers and no hold is on - accepted excess included, which an
 *  ordinary PT never takes (`coverableQty`). The server's own figure, so a
 *  hold on pieces a PT already covers is never subtracted a second time. */
export function supplementCoverableQty(line: GrnLine): number {
  if (isHeldCondition(line.condition)) return acceptedWrongQty(line);
  return line.unheld_uncovered_qty;
}

/** E119's request body, read off the generated client. */
export type DispositionRequest = NonNullable<
  paths["/api/goods-v1/inbound/grns/{id}/dispositions"]["post"]["requestBody"]
>["content"]["application/json"];

/** The disposition kinds this screen offers, in the words of the decision.
 *  `site` marks a kind the site itself may ask for with the common
 *  `movement.draft` grant (GSA-R01) - asking decides nothing; a different
 *  person approves it. Every other kind is the decision grant's. */
export const DISPOSITION_KINDS: readonly {
  kind: DispositionRequest["kind"];
  label: string;
  help: string;
  checker: boolean;
  creates: string | null;
  site?: boolean;
}[] = [
  {
    kind: "hold_excess",
    label: "Hold it",
    help: "Leave the pieces held while someone finds out what they are. Nothing moves.",
    checker: false,
    creates: null,
  },
  {
    kind: "accept_excess",
    label: "Accept the extra",
    help: "Keep the extra pieces. They become eligible for a supplement PT, never an ordinary one.",
    checker: true,
    creates: "This needs the pieces resolved to a SKU and approved cost and tax evidence first.",
  },
  {
    kind: "return",
    label: "Send it back",
    help: "Return the pieces to the vendor against this GRN. Not for damaged pieces: they stay in quarantine under their damage report.",
    checker: true,
    creates: "No SKU, cost or stock layer is created. The pieces leave held custody unvalued.",
  },
  {
    kind: "dispose",
    label: "Dispose of it",
    help: "Destroy or write the pieces off the floor against this GRN. Not for damaged pieces: they stay in quarantine under their damage report.",
    checker: true,
    creates: "No SKU, cost or stock layer is created. The pieces leave held custody unvalued.",
  },
  {
    kind: "value_damage",
    label: "Keep and value the damage",
    help: "Keep reported damaged pieces in quarantine and freeze their evidenced ticket MRP as memo value. Optional: the damage needs no value to stay held or be reviewed.",
    checker: true,
    creates:
      "Quantity and damage hold stay unchanged. ATS stays zero and no books entry is created.",
  },
  {
    kind: "resolve_identity",
    label: "Name the goods",
    help: "Record which SKU wrong or unidentified pieces really are. They keep how they were counted and stay held: naming them alone never puts them on a PT.",
    checker: false,
    creates: "Nothing is valued and nothing leaves quarantine.",
  },
  {
    kind: "accept_wrong",
    label: "Keep the wrong goods",
    help: "Keep wrong or unidentified pieces already named to a SKU. Once someone else approves, they can go on a PT like good goods. The receipt still says how they arrived.",
    checker: true,
    creates:
      "This needs the pieces named first, and approved cost and tax evidence. Damaged pieces are never kept this way, and no hold on the pieces is lifted.",
    site: true,
  },
];

export type DispositionKind = DispositionRequest["kind"];

/** The lines holding wrong or unidentified pieces no PT covers yet: the only
 *  pieces keeping wrong goods (`accept_wrong`) can ever take. */
export function wrongGoodsLines(lines: GrnLine[]): GrnLine[] {
  return lines.filter(
    (line) =>
      (line.condition === "wrong" || line.condition === "unidentified") && line.uncovered_qty > 0,
  );
}

/** The lines a decision form offers: a person who may ask only for the site's
 *  own kinds gets only the wrong or unidentified lines; anyone else every line
 *  with pieces no PT covers (`uncovered_qty`, never `held_qty` - GSA-T05). */
export function dispositionLines(
  kinds: readonly { site?: boolean }[],
  lines: GrnLine[],
): GrnLine[] {
  const siteOnly = kinds.length > 0 && kinds.every((entry) => entry.site);
  return siteOnly ? wrongGoodsLines(lines) : lines.filter((line) => line.uncovered_qty > 0);
}

/** The kinds this person may ask for on this receipt: every kind with the
 *  decision grant; with `movement.draft` alone, only the site's own kinds
 *  (the server's own rule), and only when the receipt has wrong or
 *  unidentified pieces for them - never a form that could only be refused. */
export function dispositionKindsFor(canDecide: boolean, canPrepare: boolean, lines: GrnLine[]) {
  if (canDecide) return DISPOSITION_KINDS;
  if (!canPrepare || wrongGoodsLines(lines).length === 0) return [];
  return DISPOSITION_KINDS.filter((entry) => entry.site);
}

// ---------------------------------------------------------------------------
// The receiving inbox (OPS-04, store and warehouse operations PRD §5.1)
// ---------------------------------------------------------------------------

/** The steps a delivery walks, in the order it walks them. The server decides
 *  which one a delivery is on; this is the same list, so the screen can draw
 *  the rail and say which steps are behind it. */
export const RECEIVING_STEPS = [
  "arrival",
  "count",
  "grn",
  "discrepancies",
  "pt_prepare",
  "pt_approve",
  "labels",
  "accept",
  "done",
] as const;

export type ReceivingStep = (typeof RECEIVING_STEPS)[number];

/** What each step is called, and what it actually asks somebody to do. Plain
 *  words: this is read at a dock by whoever is standing in front of the goods. */
export const STEP_LABEL: Record<ReceivingStep, string> = {
  arrival: "Arrival",
  count: "Count",
  grn: "GRN",
  discrepancies: "Discrepancies",
  pt_prepare: "PT",
  pt_approve: "PT approval",
  labels: "Labels",
  accept: "Accept & put away",
  done: "Done",
};

export const STEP_HELP: Record<ReceivingStep, string> = {
  arrival: "What came, from whom, with which invoice, and whether it was booked.",
  count: "Count what physically arrived, piece by piece, exactly as it is.",
  grn: "Issue the goods receipt from the count. Quantity only — no cost, no books.",
  discrepancies:
    "Short, excess, damaged, wrong and unidentified pieces, held and waiting for a decision. None of them goes on the ordinary PT.",
  pt_prepare: "Prepare the PT for the clean good quantity and send it for approval.",
  pt_approve: "A second person approves the PT. The preparer cannot approve their own.",
  labels: "Print the tags from the approved PT's frozen values.",
  accept: "Scan the goods in against the tags and put them away.",
  done: "Accepted and put away. Nothing is waiting.",
};

/** What each role code is called where a person reads it. */
export const OWNER_ROLE_LABEL: Record<string, string> = {
  "C-BUY": "Buyer",
  "C-INV": "Inventory controller",
  "C-OWN": "Owner",
  "C-WHO": "Warehouse",
  "M-STR": "Store",
  "M-CSH": "Store counter",
};

export function ownerRoleName(code: string | null): string {
  if (!code) return "Nobody — it is finished";
  return OWNER_ROLE_LABEL[code] ?? code;
}

/** One row of the receiving inbox: a vendor delivery or an incoming transfer
 *  dispatch, the step it waits on, and the records it has reached so far.
 *
 *  Mirrors `inbound.goods_views.INBOX_ITEM`. The links are what the delivery
 *  workflow opens; each is null until its record exists, which is exactly how
 *  the workflow knows which steps are behind it. */
export interface InboxItem {
  id: string;
  kind: "vendor_delivery" | "transfer_dispatch" | "customer_return";
  site_id: string;
  brand_id: string | null;
  reference: string;
  arrived_at: string;
  updated_at: string;
  next_step: ReceivingStep;
  next_step_owner_role: string | null;
  in_receiving_queue: boolean;
  arrival_id: string | null;
  grn_id: string | null;
  grn_number: string | null;
  /** The PT this delivery is working on now: the primary, then each supplement
   *  in turn. Null before any PT, and while a new supplement is still to be
   *  started (ticket 07B). */
  pt_id: string | null;
  pt_number: string | null;
  /** Every receipt PT this delivery has had, primary first (ticket 07B). */
  pt_ids?: string[];
  official_version_id: string | null;
  acceptance_session_id: string | null;
  /** Canonical transfer/dispatch identities projected by the scoped receiving inbox. */
  transfer_id?: string | null;
  dispatch_id?: string | null;
  /** The bill line a customer return came back on (OPS-09). Null on the other
   *  two kinds, so all three rows are one shape. */
  sale_line_id?: string | null;
  /** The booking a vendor delivery was received against, and how much of it has
   *  been received so far (OPS-17). Worked out by the server from the receipt
   *  links on every read; null when there is no booking this reader may read. */
  booking_id?: string | null;
  booking_number?: string | null;
  booking_booked_qty?: number | null;
  booking_received_qty?: number | null;
}

/** Receiving opens the retained transfer workflow using its stable server identity.
 * A legacy dispatch bookmark still resolves through the scoped inbox first. */
export function transferReceivingPath(
  item: Pick<InboxItem, "kind" | "transfer_id">,
): string | null {
  if (item.kind !== "transfer_dispatch" || !item.transfer_id) return null;
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(item.transfer_id))
    return null;
  return `/goods/transfers/${encodeURIComponent(item.transfer_id)}`;
}

/** "Booking B-104: 80 of 100 received" - the one sentence the Pending row and the
 *  booking page both say (store and warehouse operations PRD §5.1). The figures
 *  are the server's; this only words them. A booking with no number yet still
 *  says it is a booking rather than printing an id. */
export function receivedOfBooked(
  number: string | null | undefined,
  received: number,
  booked: number,
): string {
  return `Booking ${number || "without a number"}: ${Math.max(0, received)} of ${booked} received`;
}

/** The inbox row's booking sentence, or null for a delivery nobody booked. */
export function bookingProgress(item: InboxItem): string | null {
  if (!item.booking_id || item.booking_booked_qty == null) return null;
  return receivedOfBooked(
    item.booking_number,
    item.booking_received_qty ?? 0,
    item.booking_booked_qty,
  );
}

/** How a record outside the inbox names the delivery it belongs to (OPS-17): by
 *  the arrival, the GRN or the PT. The delivery screen reads the inbox narrowed
 *  to that one record, so a link from an alert, an exception or another screen
 *  opens the delivery itself rather than a separate screen of its own. */
export type DeliveryKey = "delivery" | "grn" | "pt";

/** A delivery's address, opened at `step` when one is named. */
export function deliveryStepPath(key: DeliveryKey, id: string, step?: ReceivingStep): string {
  const base = `/goods/receive/${key}/${encodeURIComponent(id)}`;
  return step ? `${base}?step=${step}` : base;
}

/** Is `value` one of the steps a delivery walks? A `?step=` from a URL is only
 *  honoured when it names one. */
export function isReceivingStep(value: string | null | undefined): value is ReceivingStep {
  return Boolean(value) && (RECEIVING_STEPS as readonly string[]).includes(value as string);
}

/** Is `step` already behind this delivery? Used to tick the rail: a step is
 *  done when the delivery has moved past it, never because a record happens to
 *  exist - a reversed PT puts the delivery back at preparation, and the rail
 *  has to say so. */
export function stepIsDone(item: InboxItem, step: ReceivingStep): boolean {
  const at = RECEIVING_STEPS.indexOf(item.next_step);
  const index = RECEIVING_STEPS.indexOf(step);
  return index >= 0 && at >= 0 && index < at;
}

/** Steps the server lets a site do while the delivery still waits on the step
 *  before them, so the rail opens them too:
 *
 *   · Count, while an unbooked delivery waits at Arrival on the buyer's booking
 *     question - the delivery moves on the moment counting begins
 *     (`inbound.goods_services._delivery_step`);
 *   · Accept, while an official PT waits at Labels - goods that already carry a
 *     readable tag (the vendor's barcode) are put away without a print job.
 *
 *  Since OPS-17 these steps are the only place a delivery is counted or put
 *  away, so shutting them would take away work the separate screens allowed. */
const OPEN_EARLY: Partial<Record<ReceivingStep, ReceivingStep>> = {
  arrival: "count",
  labels: "accept",
};

/** Which steps a delivery can actually be opened at: everything up to and
 *  including the one it waits on, and the early ones above. A step further on
 *  has no record yet. */
export function stepIsReachable(item: InboxItem, step: ReceivingStep): boolean {
  const at = RECEIVING_STEPS.indexOf(item.next_step);
  const index = RECEIVING_STEPS.indexOf(step);
  if (item.kind === "vendor_delivery" && OPEN_EARLY[item.next_step] === step) return true;
  return index >= 0 && at >= 0 && index <= at;
}
