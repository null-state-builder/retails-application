// Transfers between a store and a warehouse (OPS-06) — the wire shapes the
// Transfers list, the transfer record and the Requests screen share, and the
// few pure reading rules over them.
//
// Nothing here decides anything. Which pieces may move, who may approve, what
// is still reserved and whether a movement may close are the server's own
// answers (`outbound/transfers.py`); this file names them for a reader and
// works out which chip a row belongs under.

import type { operations } from "./api-schema";

/** A new transfer's body and one of its lines, as the generated client states them. */
export type TransferDraftBody = NonNullable<
  operations["goods_v1_outbound_transfers_create"]["requestBody"]
>["content"]["application/json"];
export type TransferDraftLine = TransferDraftBody["lines"][number];

/** Goods ticket 13B: the shipment being scanned (E243), and the bodies of the
 *  steps around it, as the generated client states them. */
export type DispatchPreparation =
  operations["goods_v1_outbound_dispatch_sessions_detail"]["responses"][200]["content"]["application/json"];
export type DispatchPreparationLine = DispatchPreparation["lines"][number];
export type DispatchScanBody = NonNullable<
  operations["goods_v1_outbound_dispatch_sessions_scan"]["requestBody"]
>["content"]["application/json"];
export type DispatchBody = NonNullable<
  operations["goods_v1_outbound_transfers_dispatch"]["requestBody"]
>["content"]["application/json"];
export type ArrivalBody = NonNullable<
  operations["goods_v1_outbound_transfers_arrival"]["requestBody"]
>["content"]["application/json"];
/** Store operations ticket 36: the delivery challan or tax invoice one shipment
 *  left with, as printed, and the no-money summary each shipment carries. */
export type TransferDocument =
  operations["goods_v1_outbound_transfers_dispatch_document"]["responses"][200]["content"]["application/json"];
export type TransferDocumentLine = TransferDocument["lines"][number];
export type ShipmentDocument = NonNullable<TransferDispatchRow["document"]>;
export type EwayBody = NonNullable<
  operations["goods_v1_outbound_transfers_eway"]["requestBody"]
>["content"]["application/json"];
/** Goods ticket 13C: one return receipt at the source, and putting returned
 *  goods away there, as the generated client states them. */
export type ReturnBody = NonNullable<
  operations["goods_v1_outbound_transfers_return_to_source"]["requestBody"]
>["content"]["application/json"];
export type AcceptReturnedBody = NonNullable<
  operations["goods_v1_outbound_transfers_accept_returned"]["requestBody"]
>["content"]["application/json"];
/** One return receipt recorded at the source. Never rewritten. */
export type ReturnReceipt = NonNullable<
  NonNullable<
    operations["goods_v1_outbound_transfers_detail"]["responses"][200]["content"]["application/json"]["dispatches"]
  >[number]["returns"]
>[number];
/** Goods ticket 14: counting a whole shipment, putting its good pieces away,
 *  proposing a shortage and deciding one, as the generated client states them. */
export type CountBody = NonNullable<
  operations["goods_v1_outbound_transfers_count"]["requestBody"]
>["content"]["application/json"];
export type AcceptBody = NonNullable<
  operations["goods_v1_outbound_transfers_accept"]["requestBody"]
>["content"]["application/json"];
export type ShortageBody = NonNullable<
  operations["goods_v1_outbound_transfers_shortage_propose"]["requestBody"]
>["content"]["application/json"];
export type ShortageDecisionBody = NonNullable<
  operations["goods_v1_outbound_transfers_shortage_decide"]["requestBody"]
>["content"]["application/json"];
/** Goods ticket 16: proposing a corrective transfer for observed excess, and
 *  confirming one, as the generated client states them. */
export type CorrectiveProposalBody = NonNullable<
  operations["goods_v1_outbound_transfers_excess_corrective"]["requestBody"]
>["content"]["application/json"];
export type CorrectiveConfirmationBody = NonNullable<
  operations["goods_v1_outbound_transfers_corrective_confirmation"]["requestBody"]
>["content"]["application/json"];
/** One entry of a count's `excess`: goods nobody sent, or what came in place of
 *  a line's wrong pieces (`in_place_of_line_key`). */
export type CountExcessEntry = NonNullable<CountBody["excess"]>[number];
type DetailDispatch = NonNullable<
  operations["goods_v1_outbound_transfers_detail"]["responses"][200]["content"]["application/json"]["dispatches"]
>[number];
/** Goods ticket 16: one excess observation a count recorded, and its decisions. */
export type ExcessObservation = DetailDispatch["excess_observations"][number];
/** One excess-observed decision: a corrective transfer proposed for exact pieces. */
export type ExcessDecision = ExcessObservation["decisions"][number];
/** A shipment's shortage: found missing, resolved, waiting, still in transit, unnamed. */
export type ShipmentShortage = DetailDispatch["shortage"];
/** One proposed transit-shortage correction and its decision. */
export type ShortageResolution = DetailDispatch["shortage_resolutions"][number];
/** A shipment's own conservation: good + held + returned + resolved + in transit = sent. */
export type ShipmentReconciliation = DetailDispatch["reconciliation"];
/** The movement's undispatched balance: approved = sent + still reserved + cancelled. */
export type MovementReconciliation =
  operations["goods_v1_outbound_transfers_detail"]["responses"][200]["content"]["application/json"]["reconciliation"];
/** One shipment's e-way evidence: three facts, never merged into one status. */
export type ShipmentEway = NonNullable<
  operations["goods_v1_outbound_transfers_detail"]["responses"][200]["content"]["application/json"]["dispatches"]
>[number]["eway"];

/** Goods ticket 13D: which source pool a movement takes from. `quarantine` is
 *  the controlled custody transfer of held, recorded quarantined stock. */
export type TransferCustody =
  operations["goods_v1_outbound_transfers_detail"]["responses"][200]["content"]["application/json"]["custody"];
/** One row of what a quarantine transfer from a site could take. */
export type QuarantineStockRow =
  operations["goods_v1_outbound_transfer_quarantine_stock"]["responses"][200]["content"]["application/json"]["items"][number];
/** Goods ticket 13E: one GRN line of damaged pre-PT custody a pre-PT transfer
 *  from a site could take. */
export type PrePtCustodyRow =
  operations["goods_v1_outbound_transfer_pre_pt_custody"]["responses"][200]["content"]["application/json"]["items"][number];

export const CUSTODY_LABEL: Record<TransferCustody, string> = {
  ordinary: "Ordinary transfer",
  quarantine: "Quarantine transfer",
  pre_pt: "Damaged goods not yet on a PT",
};

export const CUSTODY_HELP: Record<TransferCustody, string> = {
  ordinary: "Accepted, good, unheld stock that can be sold or sent once it is put away.",
  quarantine:
    "Held goods moving between sites. They stay in quarantine at both ends, keep every hold and never become sellable or sendable by moving.",
  pre_pt:
    "Damaged goods a GRN counted that no PT covers yet, moving between a store and a warehouse. They are known only by their GRN, their value stays unknown, no PT is made for them, and they stay in quarantine at both ends under every hold.",
};

/** Goods tickets 13D and 13E: held goods that travel held, arrive into
 *  quarantine and are never put away or made available by moving. */
export function isHeldCustody(custody: TransferCustody | undefined): boolean {
  return custody === "quarantine" || custody === "pre_pt";
}

/** How a pre-PT custody line names its goods: the GRN and what it counted. */
export function prePtSource(line: {
  grn_number?: string | null;
  grn_id?: string;
  counted_condition?: string | null;
}): string {
  const grn = line.grn_number || (line.grn_id ? `GRN ${line.grn_id.slice(0, 8)}` : "GRN");
  return `${grn} · counted ${line.counted_condition ?? "damaged"} · value unknown`;
}

/** The movement's own lifecycle (`outbound.goods_models.GoodsTransfer.State`). */
export const TRANSFER_STATES = [
  "draft",
  "submitted",
  "approved",
  "dispatching",
  "completed",
  "cancelled",
] as const;
export type TransferState = (typeof TRANSFER_STATES)[number];

export const TRANSFER_STATE_LABEL: Record<string, string> = {
  draft: "Draft",
  submitted: "Waiting for approval",
  approved: "Approved",
  dispatching: "In transit",
  completed: "Completed",
  cancelled: "Cancelled",
};

/** What each state actually means for the goods, in the words a person uses. */
export const TRANSFER_STATE_HELP: Record<string, string> = {
  draft: "Proposed. Nothing is reserved and nothing has moved.",
  submitted: "Waiting for a different person to approve it. Still nothing reserved.",
  approved: "The pieces are reserved at the sending site and cannot be sold or sent elsewhere.",
  dispatching: "At least one shipment is on the road.",
  completed: "Every dispatched piece is accounted for and nothing is still reserved.",
  cancelled: "Closed without sending anything.",
};

/** One shipment's own state (`TransferDispatch.State`). */
export const DISPATCH_STATE_LABEL: Record<string, string> = {
  in_transit: "On the road",
  counted: "Counted at the destination",
  accepted: "Accepted and put away",
  partly_returned: "Partly back at the sender",
  returned_to_source: "Came back to the sender",
};

export const DISPATCH_STATE_HELP: Record<string, string> = {
  in_transit: "Dispatched. Electronic visibility is not arrival and not available stock.",
  counted: "Counted whole. The good pieces are held at the destination until they are put away.",
  accepted: "Put away at the destination, and only now sellable there.",
  partly_returned:
    "Never delivered. Some pieces are back at the sender; the rest are still unaccounted for and stay open until they come back.",
  returned_to_source:
    "Never delivered. Every piece is back at the sender, where the good ones are put away again.",
};

/** A shipment's state in words for this movement's pool. A quarantine
 *  shipment that is accounted for was received into quarantine, not put away
 *  (goods ticket 13D): nothing of it is sellable there. */
export function dispatchStateWords(
  state: string,
  custody: TransferCustody | undefined,
): { label: string; help: string } {
  if (isHeldCustody(custody) && state === "accepted") {
    return {
      label: "Received into quarantine",
      help: "Counted whole. Every piece that arrived is in quarantine here, still under every hold, and none of it is sellable or sendable.",
    };
  }
  return {
    label: DISPATCH_STATE_LABEL[state] ?? state,
    help: DISPATCH_STATE_HELP[state] ?? "",
  };
}

/** The conditions a whole-shipment count reports, beside what was missing. */
export const COUNT_CONDITIONS = ["good", "damaged", "wrong", "unidentified"] as const;
export type CountCondition = (typeof COUNT_CONDITIONS)[number];

export const COUNT_CONDITION_LABEL: Record<CountCondition, string> = {
  good: "Good",
  damaged: "Damaged",
  wrong: "Wrong goods",
  unidentified: "Cannot identify",
};

export const COUNT_CONDITION_HELP: Record<CountCondition, string> = {
  good: "Fit to sell once somebody puts them away here.",
  damaged: "Goes to quarantine, held, and opens a report for a second person to decide.",
  wrong: "Something else came in place of these. They stay short; say what came instead.",
  unidentified: "No tag, or a tag nobody can resolve. Held.",
};

/** A quarantine shipment's count (goods ticket 13D): "good" is "arrived as it
 *  left", and everything counted stays in quarantine. */
export const QUARANTINE_COUNT_LABEL: Record<CountCondition, string> = {
  ...COUNT_CONDITION_LABEL,
  good: "Arrived as sent",
};

export const QUARANTINE_COUNT_HELP: Record<CountCondition, string> = {
  ...COUNT_CONDITION_HELP,
  good: "Arrived in the condition it left in. Goes to quarantine here, still under every hold.",
  damaged: "Newly damaged on the way. Goes to quarantine and opens a report for a second person.",
};

/** The labels a count uses for this movement's pool. */
export function countLabels(custody: TransferCustody | undefined): {
  label: Record<CountCondition, string>;
  help: Record<CountCondition, string>;
} {
  return isHeldCustody(custody)
    ? { label: QUARANTINE_COUNT_LABEL, help: QUARANTINE_COUNT_HELP }
    : { label: COUNT_CONDITION_LABEL, help: COUNT_CONDITION_HELP };
}

/** One SKU a quarantine transfer could send: how many pieces, from which
 *  origins, and the conditions and hold kinds standing over them. */
export interface QuarantineChoice {
  sku_id: string;
  description: string;
  available: number;
  origins: OriginShare[];
  conditions: string[];
  hold_kinds: string[];
}

/** Group the quarantine pool by SKU, keeping each origin separate. The server
 *  groups by SKU, origin and condition; a draft line names a SKU, so the rows
 *  are summed per SKU and the origins listed for a person to choose from. */
export function quarantineChoices(rows: QuarantineStockRow[]): QuarantineChoice[] {
  const bySku = new Map<string, QuarantineChoice>();
  for (const row of rows) {
    if (row.qty <= 0) continue;
    const choice: QuarantineChoice = bySku.get(row.sku_id) ?? {
      sku_id: row.sku_id,
      description: row.description,
      available: 0,
      origins: [],
      conditions: [],
      hold_kinds: [],
    };
    choice.available += row.qty;
    if (row.origin_id) {
      const share = choice.origins.find((o) => o.origin_id === row.origin_id);
      if (share) share.qty += row.qty;
      else choice.origins.push({ origin_id: row.origin_id, qty: row.qty });
    }
    if (!choice.conditions.includes(row.condition)) choice.conditions.push(row.condition);
    for (const kind of row.hold_kinds) {
      if (!choice.hold_kinds.includes(kind)) choice.hold_kinds.push(kind);
    }
    bySku.set(row.sku_id, choice);
  }
  return [...bySku.values()];
}

/** One end of a transfer, by name and code only (OPS-11): somebody who can read
 *  the transfer may not be able to read the site at the other end. */
export interface TransferSiteRef {
  id: string;
  code: string;
  name: string;
}

export interface TransferSummary {
  id: string;
  number: string | null;
  state: TransferState;
  source_site_id: string;
  destination_site_id: string;
  source_site: TransferSiteRef;
  destination_site: TransferSiteRef;
  /** Goods ticket 13D: ordinary stock, or held goods that stay held. */
  custody: TransferCustody;
  created_at: string;
  dispatched_at: string | null;
  arrived_at: string | null;
  dispatch_count: number;
  /** Everything still on the road, a counted shipment's unresolved shortage included. */
  in_transit_qty: number;
  short_qty: number;
  /** Goods ticket 14: of `short_qty`, what approved corrections resolved. */
  shortage_resolved_qty: number;
  /** Goods ticket 16: on a corrective transfer, the movement whose excess it corrects. */
  corrective_for_id: string | null;
}

export interface TransferLine {
  line_key: string;
  /** Null only on a pre-PT line whose count gave the goods no SKU (13E). */
  sku_id: string | null;
  qty: number;
  /** Goods ticket 13E: a pre-PT line names its GRN instead of origins, and its
   *  value is always unknown. */
  grn_id?: string;
  grn_number?: string | null;
  grn_line_key?: string;
  counted_condition?: string | null;
  value?: "unknown";
  damage_report_ids?: string[];
  description?: string;
  note?: string | null;
  /** Origins the drafter chose by hand. Empty means the oldest eligible ones. */
  origin_ids?: string[];
  /** Present once the plan has been frozen: the exact pieces that will go.
   *  `unit_cost_paise` and `mrp_paise` are there only for a reader holding the
   *  cost field grant; without it they are absent, never zero. */
  portions?: {
    lot_id: string;
    lower: number;
    upper: number;
    origin_id: string | null;
    unit_cost_paise?: string | null;
    mrp_paise?: string | null;
  }[];
}

/** How many pieces of a line come from one origin, and - for a reader allowed
 *  to see value - the unit cost and MRP that origin was frozen at. */
export interface OriginShare {
  origin_id: string | null;
  qty: number;
  unit_cost_paise?: string | null;
  mrp_paise?: string | null;
}

/** The frozen plan of one line, one row per origin, in the order the plan
 *  names them. Separate origins stay separate rows - a transfer never merges
 *  two costs into one (transfers PRD §4) - and pieces with no origin are
 *  their own row rather than folded into another. */
export function originsOf(line: TransferLine): OriginShare[] {
  const shares = new Map<string, OriginShare>();
  for (const piece of line.portions ?? []) {
    const key = piece.origin_id ?? "";
    const share: OriginShare = shares.get(key) ?? { origin_id: piece.origin_id, qty: 0 };
    share.qty += piece.upper - piece.lower;
    if (piece.unit_cost_paise !== undefined) share.unit_cost_paise = piece.unit_cost_paise;
    if (piece.mrp_paise !== undefined) share.mrp_paise = piece.mrp_paise;
    shares.set(key, share);
  }
  return [...shares.values()];
}

/** One stock-search row, as far as choosing origins needs it. The two value
 *  properties are present only on a value basis, which needs the cost grant. */
export interface OriginStockRow {
  sku_id: string | null;
  origin_id: string | null;
  transferable_qty: number;
  physical_qty?: number;
  cost_value_paise?: string | null;
  ticket_value_paise?: string | null;
}

/** One unit's value out of a row's total value. Every piece of a stock row
 *  shares one origin, so the division is exact; anything else is not a price
 *  and is not shown. */
function perUnit(total: string | null | undefined, pieces: number | undefined): string | undefined {
  if (total === null || total === undefined || !pieces) return undefined;
  const value = BigInt(total);
  const count = BigInt(pieces);
  return value % count === 0n ? (value / count).toString() : undefined;
}

/** What each SKU can be sent from: its eligible origins and how many pieces
 *  each can give. A row with nothing transferable, or no origin to name, is
 *  not something a person can choose. */
export function eligibleOrigins(rows: OriginStockRow[]): Map<string, OriginShare[]> {
  const bySku = new Map<string, Map<string, OriginShare>>();
  for (const row of rows) {
    if (!row.sku_id || !row.origin_id || row.transferable_qty <= 0) continue;
    const origins = bySku.get(row.sku_id) ?? new Map<string, OriginShare>();
    const share: OriginShare = origins.get(row.origin_id) ?? { origin_id: row.origin_id, qty: 0 };
    share.qty += row.transferable_qty;
    const cost = perUnit(row.cost_value_paise, row.physical_qty);
    const mrp = perUnit(row.ticket_value_paise, row.physical_qty);
    if (cost !== undefined) share.unit_cost_paise = cost;
    if (mrp !== undefined) share.mrp_paise = mrp;
    origins.set(row.origin_id, share);
    bySku.set(row.sku_id, origins);
  }
  return new Map([...bySku].map(([sku, origins]) => [sku, [...origins.values()]]));
}

/** Whether a session may ask stock search for a value basis. Cost and ticket
 *  (MRP) value both need the `cost` field grant (Anand, 15 September 2026);
 *  the server still decides per site and brand. */
export function seesStockValue(fieldGrants: readonly string[] | undefined): boolean {
  return Boolean(fieldGrants?.includes("cost"));
}

/** A short, stable name for an origin a person can compare across screens. */
export function originLabel(originId: string | null): string {
  return originId ? `Origin ${originId.slice(0, 8)}` : "No recorded origin";
}

export interface DispatchCountLine {
  line_key: string;
  dispatched: number;
  good: number;
  damaged: number;
  wrong: number;
  unidentified: number;
  short: number;
}

export interface DispatchCount {
  counted_at: string;
  note: string | null;
  lines: DispatchCountLine[];
  good_total: number;
  held_total: number;
  short_total: number;
  /** Goods ticket 16: expected pieces other goods came in place of. */
  wrong_total?: number;
  excess: {
    lot_id: string;
    qty: number;
    description: string;
    in_place_of_line_key?: string | null;
    pairing_key?: string | null;
  }[];
  dispatch_sequence_no: number;
}

export interface TransferDispatchRow {
  id: string;
  sequence_no: number;
  state: "in_transit" | "counted" | "accepted" | "partly_returned" | "returned_to_source";
  source_site_id: string;
  destination_site_id: string;
  dispatched_at: string;
  recorded_by: { id: string; name: string };
  transport: Record<string, unknown>;
  /** The preparation whose scans this shipment carried (goods ticket 13B). */
  preparation_id: string | null;
  /** When it physically arrived. Arrival releases nothing: until the count the
   *  pieces are still in transit. */
  arrived_at: string | null;
  arrival_recorded_by: { id: string; name: string } | null;
  counted_at: string | null;
  accepted_at: string | null;
  /** When the last piece was back at the sender. */
  returned_at: string | null;
  /** Why the delivery failed, as the first return receipt said it. */
  return_reason: string | null;
  quantity: number;
  /** What of this shipment is on the road now - after a failed delivery,
   *  what has not come back and is still unaccounted for. */
  in_transit_qty: number;
  /** Goods ticket 13C: what the return receipts have brought back so far. */
  returned_qty: number;
  returns: ReturnReceipt[];
  lines: {
    line_key: string;
    sku_id: string;
    qty: number;
    origins: { origin_id: string | null; qty: number }[];
    /** Pieces of this line recorded back at the sender. */
    returned_qty: number;
    /** Returned good pieces waiting, unaccepted, in the sender's receiving. */
    returned_awaiting_putaway: number;
    /** Good pieces counted at the destination and not yet put away there. */
    awaiting_putaway: number;
  }[];
  count: DispatchCount | null;
  eway: ShipmentEway;
  /** Store operations ticket 36: the challan or tax invoice it left with, or
   *  null - the sending site's switch was off. No money here. */
  document: {
    id: string;
    kind: "delivery_challan" | "tax_invoice";
    number: string;
    issued_on: string;
  } | null;
  /** Goods ticket 16: what the count found that nobody sent, and its route. */
  excess_observations: ExcessObservation[];
  shortage: ShipmentShortage;
  shortage_resolutions: ShortageResolution[];
  reconciliation: ShipmentReconciliation;
}

export interface TransferEventRow {
  id: string;
  kind: string;
  site_id: string;
  actual_at: string;
  recorded_at: string;
  details: Record<string, unknown>;
}

export interface TransferDetail extends TransferSummary {
  approval?: {
    id: string;
    revision: number;
    reviewed_hash: string;
    policy_version_id: string | null;
    completed_steps: number;
    total_steps: number;
    current_label: string;
  } | null;
  pt_id: string | null;
  pt_number: string | null;
  pt_state: string | null;
  drafted_by: { id: string; name: string };
  approved_by: { id: string; name: string } | null;
  lines: TransferLine[];
  reserved_qty: number;
  /** Goods ticket 13B: the movement in numbers - approved, sent over every
   *  shipment, and released by an explicit cancellation. */
  approved_qty: number;
  dispatched_qty: number;
  cancelled_qty: number;
  /** Goods ticket 13C: what failed deliveries brought back to the sender. */
  returned_qty: number;
  /** Goods ticket 14: the undispatched balance, reconciled on its own. */
  reconciliation: MovementReconciliation;
  /** Goods ticket 16: goods observed that nobody sent, beside what was sent. */
  excess_qty: number;
  /** Goods ticket 16: on a corrective transfer, what it corrects and the
   *  excess decision it serves. */
  corrective_for: { id: string; number: string | null } | null;
  corrective_decision: ExcessDecision | null;
  /** The shipment being scanned at the sending site now, if any. */
  dispatch_preparation: {
    id: string;
    revision: number;
    scanned_qty: number;
    opened_at: string;
    last_activity_at: string;
  } | null;
  dispatches: TransferDispatchRow[];
  events: TransferEventRow[];
  /** Exactly what this person may do to this record right now. The commands
   *  re-check all of it themselves; a button that is not drawn is a courtesy,
   *  never the boundary. */
  allowed_actions: string[];
}

export interface TransferRequestRow {
  id: string;
  source_site_id: string;
  destination_site_id: string;
  requested_by: { id: string; name: string };
  requested_at: string;
  state: "open" | "drafted" | "closed";
  note: string | null;
  lines: { line_key: string; sku_id: string; qty: number; note: string | null }[];
  transfer_id: string | null;
}

export const REQUEST_STATE_LABEL: Record<string, string> = {
  open: "Waiting for the sending site",
  drafted: "A transfer has been drafted for it",
  closed: "Closed",
};

// ---------------------------------------------------------------------------
// The list's filters
// ---------------------------------------------------------------------------

/** The chips on the Transfers list. *In transit* is one of these and not a
 *  screen of its own, exactly as the store and warehouse operations PRD §7
 *  says. "Drafts" covers a movement that has been sent for approval as well as
 *  one nobody has sent yet, because neither has reserved anything - which is
 *  the thing a person is actually asking about. */
export const TRANSFER_FILTERS = [
  { key: "all", label: "All", states: TRANSFER_STATES },
  { key: "drafts", label: "Drafts", states: ["draft", "submitted"] },
  { key: "approved", label: "Approved", states: ["approved"] },
  { key: "in_transit", label: "In transit", states: ["dispatching"] },
  { key: "completed", label: "Completed", states: ["completed", "cancelled"] },
] as const;

export type TransferFilterKey = (typeof TRANSFER_FILTERS)[number]["key"];

export function matchesFilter(row: TransferSummary, key: TransferFilterKey): boolean {
  const filter = TRANSFER_FILTERS.find((f) => f.key === key) ?? TRANSFER_FILTERS[0];
  return (filter.states as readonly string[]).includes(row.state);
}

/** Which way this transfer runs relative to the site being looked at. */
export function directionOf(row: TransferSummary, siteId: string): "in" | "out" | "other" {
  if (row.source_site_id === siteId) return "out";
  if (row.destination_site_id === siteId) return "in";
  return "other";
}

// ---------------------------------------------------------------------------
// Reading one record
// ---------------------------------------------------------------------------

/** How many pieces of a line are still reserved and undispatched.
 *
 *  Approved minus everything already sent on any shipment - including a
 *  shipment that came back, because those pieces departed and their
 *  reservation was consumed when they did. Cancelling the balance is what
 *  releases what is left, and nothing else does. */
export function outstandingOf(detail: TransferDetail, lineKey: string): number {
  // Nothing reserved means nothing outstanding: not yet approved, or the
  // balance was cancelled (a cancellation releases everything still reserved,
  // and nothing can be dispatched after it).
  if (detail.reserved_qty === 0) return 0;
  const approved = detail.lines.find((l) => l.line_key === lineKey)?.qty ?? 0;
  const sent = detail.dispatches.reduce(
    (total, record) => total + (record.lines.find((l) => l.line_key === lineKey)?.qty ?? 0),
    0,
  );
  return Math.max(0, approved - sent);
}

/** The pieces of one shipment's line that the destination counted good and has
 *  not yet put away. A count says what arrived; only acceptance puts it away,
 *  and it may take several goes, so this is what an Accept form may still
 *  offer - the server's own reading of what is waiting. */
export function acceptableOf(record: TransferDispatchRow, lineKey: string): number {
  if (record.state !== "counted") return 0;
  return record.lines.find((l) => l.line_key === lineKey)?.awaiting_putaway ?? 0;
}

/** Why putting `qty` pieces of a line away would be refused, or "". */
export function acceptProblem(qty: number, waiting: number): string {
  if (!Number.isInteger(qty) || qty < 0) return "Put away a whole number of pieces.";
  if (qty > waiting)
    return `Only ${waiting} good piece(s) of this line are waiting to be put away.`;
  return "";
}

/** What a shipment still owes the movement, in one sentence or none. */
export function outstandingNote(record: TransferDispatchRow): string | null {
  if (record.state === "returned_to_source") return null;
  if (record.state === "partly_returned") {
    return `${record.in_transit_qty} piece(s) have not come back and are still unaccounted for. This shipment stays open until they do.`;
  }
  const unresolved = record.shortage?.unresolved ?? 0;
  if (unresolved > 0) {
    const waiting = record.shortage?.pending_approval ?? 0;
    return (
      `${unresolved} piece(s) never arrived and are still in transit. This shipment stays open ` +
      `until an approved shortage correction resolves them` +
      (waiting > 0 ? ` (${waiting} waiting for a decision).` : ".")
    );
  }
  if (record.state === "in_transit") return "Nobody has counted this shipment yet.";
  if (record.state === "counted") return "Counted. The good pieces are waiting to be put away.";
  return null;
}

/** Does a count add up to something the server will accept? A shipment is
 *  counted whole, so the answer may be "fewer than were sent" (that is a
 *  shortage) but never "more" - goods nobody sent are excess, recorded
 *  separately, and never more of what was sent. */
export function countProblem(found: Record<CountCondition, number>, dispatched: number): string {
  const total = COUNT_CONDITIONS.reduce((sum, name) => sum + (found[name] || 0), 0);
  if (COUNT_CONDITIONS.some((name) => (found[name] || 0) < 0)) {
    return "A count cannot be a negative number of pieces.";
  }
  if (total > dispatched) {
    return `This shipment carried ${dispatched} piece(s). Goods that were not sent are excess, not more of what was.`;
  }
  return "";
}

// ---------------------------------------------------------------------------
// Preparing and following one shipment (goods ticket 13B)
// ---------------------------------------------------------------------------

/** The shipment a preparation has scanned, line by line: exactly what a
 *  dispatch of it must carry - no more, no less. */
export function scannedShipment(
  preparation: Pick<DispatchPreparation, "lines">,
): { line_key: string; qty: number }[] {
  return preparation.lines
    .filter((line) => line.scanned_qty > 0)
    .map((line) => ({ line_key: line.line_key, qty: line.scanned_qty }));
}

/** Why a scan of `qty` more pieces on this line would be refused, or "". The
 *  server checks the same and has the last word. */
export function scanProblem(line: DispatchPreparationLine | undefined, qty: number): string {
  if (!line) return "Choose the line the piece belongs to.";
  if (!Number.isInteger(qty) || qty < 1) return "Scan at least one piece.";
  const room = line.reserved_qty - line.scanned_qty;
  if (qty > room) {
    return `Only ${room} more of this line can go: ${line.reserved_qty} are still reserved and ${line.scanned_qty} are already scanned. More needs a fresh approval.`;
  }
  return "";
}

/** A shipment's e-way evidence in words: whether it left with the goods, what
 *  is on file now, and whether that was verified - three separate facts. */
export function ewayWords(eway: ShipmentEway): string {
  const atDispatch =
    eway.at_dispatch === "present"
      ? "Present at dispatch"
      : eway.at_dispatch === "not_present"
        ? "Not present at dispatch"
        : "Not recorded at dispatch";
  const onFile = eway.reference
    ? eway.at_dispatch === "present" && !eway.attached_at
      ? ` · reference ${eway.reference}`
      : ` · reference ${eway.reference} attached later`
    : " · no reference on file";
  const checked = eway.reference ? (eway.verified ? " · verified" : " · not verified") : "";
  return `${atDispatch}${onFile}${checked}`;
}

/** A transfer document's name, as it prints (store operations ticket 36). */
export function documentLabel(kind: ShipmentDocument["kind"]): string {
  return kind === "tax_invoice" ? "Tax invoice" : "Delivery challan";
}

/** Why this document and not the other one, in words - from the two GSTINs. */
export function documentReason(document: TransferDocument): string {
  if (document.kind === "delivery_challan") {
    return "Both sites are under the same GSTIN, so this is a stock transfer within one registration, not a sale. No tax is charged.";
  }
  const tax = document.tax_kind === "igst" ? "IGST" : "CGST and SGST";
  return `The two sites are under different GSTINs, so this is a supply from one to the other. ${tax} is charged.`;
}

/** Where a shipment's document prints. */
export function documentPath(transferId: string, dispatchId: string): string {
  return `/goods/transfers/${transferId}/shipments/${dispatchId}/document`;
}

// ---------------------------------------------------------------------------
// A failed delivery coming back (goods ticket 13C)
// ---------------------------------------------------------------------------

/** How many pieces of one shipped line are still unaccounted for after a
 *  failed delivery: shipped, less what the return receipts brought back. */
export function unreturnedOf(record: TransferDispatchRow, lineKey: string): number {
  const line = record.lines.find((l) => l.line_key === lineKey);
  if (!line) return 0;
  return Math.max(0, line.qty - (line.returned_qty ?? 0));
}

/** Why a return receipt for one line would be refused, or "". Only what is
 *  physically back is recorded, so it may be fewer than are missing but
 *  never more. The server checks the same and has the last word. */
export function returnProblem(good: number, damaged: number, unreturned: number): string {
  if (good < 0 || damaged < 0) return "A return cannot be a negative number of pieces.";
  if (good + damaged > unreturned) {
    return `Only ${unreturned} piece(s) of this line are still unaccounted for. Record only what is physically back.`;
  }
  return "";
}

/** One return receipt in words: how many came back, in what condition. */
export function returnWords(receipt: ReturnReceipt): string {
  const good = receipt.lines.reduce((total, line) => total + (line.good ?? 0), 0);
  const damaged = receipt.lines.reduce((total, line) => total + (line.damaged ?? 0), 0);
  return `${receipt.quantity} back (${good} good, ${damaged} damaged)`;
}

// ---------------------------------------------------------------------------
// A counted shipment's shortage (goods ticket 14)
// ---------------------------------------------------------------------------

/** How many missing pieces of one line no proposal names yet: the count's
 *  shortage on that line, less what open or approved proposals already name. */
export function unclaimedOf(record: TransferDispatchRow, lineKey: string): number {
  const short = record.count?.lines.find((l) => l.line_key === lineKey)?.short ?? 0;
  const named = record.shortage_resolutions
    .filter((gap) => gap.state !== "rejected")
    .reduce(
      (total, gap) =>
        total + gap.lines.filter((l) => l.line_key === lineKey).reduce((n, l) => n + l.qty, 0),
      0,
    );
  return Math.max(0, short - named);
}

/** Why a shortage proposal for one line would be refused, or "". */
export function shortageProblem(qty: number, unclaimed: number): string {
  if (!Number.isInteger(qty) || qty < 0) return "Name a whole number of missing pieces.";
  if (qty > unclaimed) {
    return `Only ${unclaimed} missing piece(s) of this line are not already named by a proposal.`;
  }
  return "";
}

export const SHORTAGE_STATE_LABEL: Record<string, string> = {
  pending: "Waiting for a decision - still in transit",
  approved: "Approved - resolved as a transit shortage",
  rejected: "Rejected - still in transit",
};

/** A shipment's conservation in one sentence: every piece that left, accounted for. */
export function reconciliationWords(r: ShipmentReconciliation): string {
  const parts = [
    `${r.received_good} arrived good`,
    `${r.received_held} arrived held`,
    ...(r.returned > 0 ? [`${r.returned} came back`] : []),
    ...(r.shortage_resolved > 0 ? [`${r.shortage_resolved} resolved as shortage`] : []),
    `${r.in_transit} still in transit`,
  ];
  return `${r.dispatched} sent = ${parts.join(" + ")}${r.excess > 0 ? `; ${r.excess} excess held beside it` : ""}`;
}

// ---------------------------------------------------------------------------
// Goods ticket 16: excess and wrong goods, and corrective transfers
// ---------------------------------------------------------------------------

/** Why a count's wrong goods cannot be sent yet, or "". Every wrong piece has
 *  to say what came in its place; the server refuses it otherwise. */
export function wrongProblem(wrong: number, cameInstead: string): string {
  if (wrong > 0 && !cameInstead.trim()) {
    return "Say what came instead of the wrong pieces - a description of the goods that arrived.";
  }
  return "";
}

/** Where one excess observation has got to, in one sentence. */
export function excessWords(observation: ExcessObservation): string {
  const what = observation.in_place_of_line_key ? "came instead of what was sent" : "nobody sent";
  if (observation.unresolved_qty === 0) {
    return `${observation.qty} ${what} - matched to a corrective transfer.`;
  }
  const parts = [`${observation.unresolved_qty} held, unvalued`];
  if (observation.claimed_qty > 0) {
    parts.push(`${observation.claimed_qty} named by a corrective transfer`);
  }
  if (observation.matched_qty > 0) parts.push(`${observation.matched_qty} matched`);
  return `${observation.qty} ${what}: ${parts.join(", ")}.`;
}

/** The wrong-goods pairs of a shipment whose short-expected half nobody has
 *  proposed yet (or whose proposal was rejected). */
export function unproposedPairs(record: TransferDispatchRow): ExcessObservation[] {
  return record.excess_observations.filter(
    (observation) =>
      observation.pairing_key &&
      !record.shortage_resolutions.some(
        (gap) => gap.pairing_key === observation.pairing_key && gap.state !== "rejected",
      ),
  );
}

/** Why a corrective proposal would be refused before the server is asked, or "". */
export function correctiveProblem(qty: number, unclaimed: number, available: number): string {
  if (!Number.isInteger(qty) || qty < 1) return "Name at least one piece.";
  if (qty > unclaimed) return `Only ${unclaimed} piece(s) of this excess are still unclaimed.`;
  if (qty > available) {
    return `The sender can show only ${available} eligible piece(s) of that item. The excess stays held until it can.`;
  }
  return "";
}

export const EXCESS_DECISION_LABEL: Record<string, string> = {
  pending: "Corrective transfer waiting for approval",
  approved: "Corrective transfer approved",
  rejected: "Rejected",
  withdrawn: "Withdrawn - its corrective transfer was cancelled",
};
