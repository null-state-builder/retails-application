// Bin moves, holds, releases and mark-damaged (ticket 12) — the wire shapes the
// Movements screen and the Stock screen's "Mark damaged" action share, and the
// pure reading rules over them.
//
// Nothing here decides what may move. Eligibility, exact source portions, the
// hold key and the acceptance rule are the server's own answers
// (`outbound/goods_movements.py`); this file only names them for a reader.

import type { paths } from "./api-schema";

/** The movement kinds this stage builds (`goods_movements.ACTIVE_KINDS`). */
export const MOVEMENT_KINDS = ["bin_move", "hold", "release"] as const;

/** Goods ticket 15A: evidenced adjustments ride the same routes
 *  (`goods_adjustments.KINDS`). A removal takes exact, unreserved, unheld pieces
 *  at their own recorded cost; a found line adds held custody. Each waits for a
 *  different person's approval (the Owner). */
export const ADJUSTMENT_KINDS = ["adjustment_down", "shrinkage", "adjustment_up"] as const;
export type AdjustmentKind = (typeof ADJUSTMENT_KINDS)[number];

type MovementWire =
  paths["/api/goods-v1/outbound/movements/{id}"]["get"]["responses"][200]["content"]["application/json"];
type MovementLineWire = NonNullable<
  NonNullable<NonNullable<MovementWire["data"]>["lines"]>["items"]
>[number];

/** Every kind the movement read can carry, read off the generated client. */
export type MovementKind = NonNullable<NonNullable<NonNullable<MovementWire["data"]>["header"]>["kind"]>;
/** How a line's value is established (`value_basis`, adjustments only). */
export type ValueBasis = NonNullable<MovementLineWire["value_basis"]>;

export const MOVEMENT_KIND_LABEL: Record<string, string> = {
  bin_move: "Moved between locations",
  hold: "Put on hold",
  mark_damaged: "Marked damaged",
  release: "Released from hold",
  adjustment_down: "Adjusted down (count correction)",
  shrinkage: "Shrinkage (pieces lost)",
  adjustment_up: "Found stock (adjusted up)",
  // Goods ticket 12B: the release a rejected damage report posts. It is a REL
  // like any release, but nobody drafted it: it is the reviewer's decision.
  damage_rejected: "Damage report rejected",
  // Goods ticket 15B.
  rtv: "Return to vendor",
  // Goods ticket 15C: the value is written off; the goods stay where they are.
  writeoff: "Write-off (goods kept in quarantine)",
  // Goods ticket 15D: goods actually destroyed or handed over for scrap.
  disposal: "Disposal (goods destroyed or scrapped)",
};

/** A document head state, in the words the screen uses for a movement. */
export const MOVEMENT_STATE_LABEL: Record<string, string> = {
  draft: "Draft",
  submitted: "Waiting for approval",
  official: "Recorded",
  reversed: "Reversed",
};

/** One frozen line of a movement: which pieces of which lot went where. */
export interface MovementLine {
  line_key: string;
  lot_id: string | null;
  qty: number;
  sku_id: string | null;
  origin_id: string | null;
  condition: string;
  /** Null only on a new-found line: those pieces came from nowhere. */
  source_location_id: string | null;
  /** Null on a removal: the pieces leave the site. */
  destination_location_id: string | null;
  hold_keys: string[];
  portions: { lot_id: string; lower: number; upper: number; origin_id?: string | null }[];
  value_basis?: ValueBasis;
  /** RTV only: quarantined recorded pieces, or accepted good stock. */
  source_pool?: RtvSourcePool | null;
  description?: string;
  cost_evidence_origin_id?: string | null;
  found_lot_id?: string | null;
}

export interface MovementHeader {
  kind: MovementKind;
  site_id: string;
  reason_code: string;
  evidence_ids: string[];
  /** Adjustments: the note that is (with or instead of a photo) their evidence. */
  evidence_note?: string | null;
  source_document_id: string | null;
  count_id: string | null;
  /** RTV only: the vendor that agreed to take the goods back, and the agreement's reference. */
  vendor_id?: string | null;
  agreement_reference?: string | null;
  /** Disposal only (goods ticket 15D): how and when the goods actually went. */
  disposal?: DisposalFacts | null;
}

/** Who recorded the movement, and — for a release — who separately approved it. */
export interface MovementAuthority {
  kind: "actor" | "approval";
  actor: { id: string; name: string };
  approved_by: { id: string; name: string } | null;
  approved_at: string | null;
  approval_request_id: string | null;
  approval_state: string | null;
}

/** The GRN or PT an adjustment names (goods ticket 15A), by number. */
export interface MovementReference {
  id: string;
  kind: string;
  number: string | null;
}

/** One exception an adjustment opened, as its record lists it. */
export interface MovementException {
  id: string;
  kind: string;
  /** The exception's own subject, which its resolution route opens. */
  subject_id: string;
  state: string;
  reason_code: string;
  due_at: string | null;
  allowed_resolution_actions: string[];
}

/** `MovementDetailDTO` (design §6.2) plus this ticket's `authority` block. */
export interface MovementData {
  header: MovementHeader;
  lines: { items: MovementLine[]; next_cursor: string | null; total: number };
  authority: MovementAuthority;
  /** Adjustments: the GRN or PT named, or null (none, or not the reader's to see). */
  source_document?: MovementReference | null;
  /** Adjustments and RTVs: its owned work, or null for a reader without exception access. */
  exceptions?: MovementException[] | null;
  /** RTV only (goods ticket 15B): status, balances, reasons and pickups. */
  rtv?: RtvDetail | null;
  /** Write-off only (goods ticket 15C): what was written off, and that the goods are still here. */
  write_off?: WriteOffDetail | null;
  /** Disposal only (goods ticket 15D): what left custody, and how its loss stands. */
  disposal?: DisposalDetail | null;
}

/** Where a named GRN or PT opens: its delivery's own page. */
export function referencePath(ref: MovementReference): string {
  const key = ref.kind === "GRN" || ref.kind === "CGRN" ? "grn" : "pt";
  return `/goods/receive/${key}/${encodeURIComponent(ref.id)}`;
}

/** "GRN X/GRN/26-27/000001" — a reference said the way the documents say it. */
export function referenceWords(ref: MovementReference): string {
  const kind = ref.kind === "RPT" || ref.kind === "OPT" ? "PT" : ref.kind;
  return `${kind} ${ref.number ?? ref.id.slice(0, 8)}`;
}

/** `ResourceSummary` as the movements list answers it (E104). */
export interface MovementSummary {
  id: string;
  kind: string;
  purpose: string | null;
  number: string | null;
  state: string;
  site_id: string;
  created_at: string;
  updated_at: string;
  /** RTV only: where the return stands; null before approval and for other kinds. */
  rtv_state?: RtvState | null;
}

/** One row of the stock search (E210): `StockDTO`, plus the basis its page names. */
export interface StockSearchRow {
  site_id: string | null;
  location_id: string | null;
  sku_id: string | null;
  description: string;
  origin_id: string | null;
  source_kind: string;
  condition: string;
  physical_qty: number;
  valued_qty: number;
  accepted_qty: number;
  held_qty: number;
  reserved_qty: number;
  ats_qty: number;
  transferable_qty: number;
  eligibility_reasons: string[];
  cost_value_paise?: string | null;
  ticket_value_paise?: string | null;
}

export interface StockSearchPage {
  items: StockSearchRow[];
  next_cursor: string | null;
  as_of: string;
  basis: "quantity" | "cost" | "ticket";
}

/** The label a movement carries in a list: its purpose, not its document kind —
 *  mark-damaged and an ordinary hold are both `HLD` documents, and a reader has
 *  to be able to tell them apart. */
export function movementLabel(purpose: string | null, kind: string): string {
  return MOVEMENT_KIND_LABEL[purpose ?? ""] ?? MOVEMENT_KIND_LABEL[kind] ?? kind;
}

/** How many pieces a movement's shown lines cover. */
export function movedQty(lines: MovementLine[]): number {
  return lines.reduce((total, line) => total + line.qty, 0);
}

/** The hold keys a hold movement activated, ready for a release to quote back.
 *  A hold always names one key per line, so this is empty only for a movement
 *  that placed no hold at all — a bin move, or a release. */
export function heldKeys(lines: MovementLine[]): string[] {
  return lines.flatMap((line) => line.hold_keys);
}

/** "3 pieces of lot 1a2b… (0–3)" — a portion said out loud, so a reader can
 *  check the movement against the shelf without reading a range type. */
export function portionWords(portion: { lot_id: string; lower: number; upper: number }): string {
  const size = portion.upper - portion.lower;
  const piece = size === 1 ? "piece" : "pieces";
  return `${size} ${piece} of lot ${portion.lot_id.slice(0, 8)}… (${portion.lower}–${portion.upper})`;
}

/** Words for how an adjustment line is valued. Never a money amount: that is
 *  behind the cost grant, and the movement read publishes none. */
export const VALUE_BASIS_LABEL: Record<ValueBasis, string> = {
  recorded_layer_cost: "At each piece's own recorded cost",
  origin_evidence: "At the recorded cost of this site's own receipt of the SKU",
  unvalued: "No value yet — held until it is valued and accepted",
  written_off: "Loss already recognised by the write-off it follows",
};

export function isAdjustment(kind: string): kind is AdjustmentKind {
  return (ADJUSTMENT_KINDS as readonly string[]).includes(kind);
}

/** Pieces an adjustment may take from a row: standing there, valued, and neither
 *  reserved nor held. A stock row is an aggregate, so this is only a hint — the
 *  server picks the exact pieces and refuses what it cannot take. */
export function removableQty(row: StockSearchRow): number {
  if (!hasSource(row) || row.valued_qty === 0) return 0;
  return Math.max(0, Math.min(row.valued_qty, row.physical_qty) - row.held_qty - row.reserved_qty);
}

/** A row can be moved or held only where there is something physically there.
 *  A row that is entirely in transit or already gone has no source to name. */
export function hasSource(row: StockSearchRow): boolean {
  return row.physical_qty > 0 && Boolean(row.location_id);
}

/** What a release may put back: the server refuses a portion that was never
 *  accepted here, and refuses damaged goods outright — those stay in quarantine
 *  until they are returned, disposed of or their damage is valued. The screen
 *  says so before the button is pressed rather than letting a reader discover
 *  it as a refusal. */
export function releasable(row: StockSearchRow): boolean {
  return row.held_qty > 0 && row.accepted_qty > 0 && row.condition !== "damaged";
}

// ---------------------------------------------------------------------------
// Damage reports and their independent review (OPS-05; goods tickets 12A, 12B)
// ---------------------------------------------------------------------------

/** One frozen line of a damage report: which pieces it covers, and where they
 *  stood before they were quarantined. */
export interface DamageReportLine {
  line_key: string;
  lot_id: string | null;
  sku_id: string | null;
  origin_id: string | null;
  qty: number;
  source_location_id: string | null;
  hold_keys: string[];
  portions: { lot_id: string; lower: number; upper: number; condition?: string }[];
}

type DamageReportWire =
  paths["/api/goods-v1/outbound/damage-reports"]["get"]["responses"][200]["content"]["application/json"]["items"][number];

export type DamageState = DamageReportWire["state"];
export type DamageSource = DamageReportWire["source"];

/** `DamageReportDTO` (E252), read off the generated client so a field or source
 *  the server adds cannot drift from what the screens show. The goods are
 *  already out of availability when this row exists; the state says what a
 *  second person decided, not whether they can be sold. Only `lines` is typed
 *  here by hand: the wire publishes it as open objects. */
export type DamageReport = Omit<DamageReportWire, "lines"> & { lines: DamageReportLine[] };

/** Deliberately not "damaged" / "not damaged": until somebody decides, the
 *  goods are held either way, and the label has to say which of those two facts
 *  it is reporting. */
export const DAMAGE_STATE_LABEL: Record<string, string> = {
  pending: "Pending review",
  confirmed: "Confirmed damaged",
  rejected: "Report rejected",
  // Goods ticket 15B: not a review - every piece went back to the vendor.
  closed: "Closed",
};

export const DAMAGE_SOURCE_LABEL: Record<DamageSource, string> = {
  movement: "Reported on the stock screen",
  receiving: "Found while receiving",
  transfer_arrival: "Found when a transfer arrived",
  transfer_return: "Found when a failed delivery came back",
};

/** What rejecting a report does, said before the button is pressed (ticket 12B).
 *  It lifts the damage and nothing else, so the words cannot promise the goods
 *  are back on sale. */
export const DAMAGE_REJECT_SCOPE =
  "Rejecting lifts only the damage. Pieces another hold still covers stay in quarantine, " +
  "reserved pieces stay reserved, and pieces never accepted go back to receiving and still " +
  "have to be accepted.";

/** Why the reject button is not drawn: the server's `can_reject` is false. */
export const DAMAGE_NO_REJECT =
  "This report can only be confirmed: its pieces were valued as damaged, or they no longer " +
  "stand where the report left them.";

/** The state and, once it has one, the person who decided it — which is what
 *  makes a decision answerable rather than a status word. A closed report had
 *  no reviewer (goods ticket 15B): its note says why it closed instead. */
export function damageReviewWords(report: DamageReport): string {
  const state = DAMAGE_STATE_LABEL[report.state] ?? report.state;
  const who = report.reviewed_by?.name || report.reviewed_by?.id;
  if (who) return `${state} · ${who}`;
  return report.state === "closed" && report.review_reason
    ? `${state} · ${report.review_reason}`
    : state;
}

/** The reports that speak for one damaged stock row, most useful first.
 *
 *  A stock row is an aggregate — one (site, location, SKU, origin, condition)
 *  group — and a report names exact portions, so this is a match on what the
 *  two share: the site and the SKU. A report line that carries no SKU (damage
 *  held while receiving, before the goods were named) matches any row at its
 *  site rather than silently disappearing. Rows that are not damaged have no
 *  damage report by definition. */
export function damageReportsFor(
  row: { site_id: string | null; sku_id: string | null; condition: string },
  reports: DamageReport[],
): DamageReport[] {
  if (row.condition !== "damaged" || !row.site_id) return [];
  const matching = reports.filter(
    (report) =>
      report.site_id === row.site_id &&
      report.lines.some((line) => !line.sku_id || !row.sku_id || line.sku_id === row.sku_id),
  );
  // Whatever is still waiting for somebody comes first; after that, the most
  // recently reported, because that is the one a reader is asking about.
  return [...matching].sort((a, b) => {
    if (a.state !== b.state) return a.state === "pending" ? -1 : b.state === "pending" ? 1 : 0;
    return b.reported_at.localeCompare(a.reported_at);
  });
}

/** One line of words for a damaged stock row, or null when nothing speaks for it. */
export function damageRowWords(
  row: { site_id: string | null; sku_id: string | null; condition: string },
  reports: DamageReport[],
): string | null {
  const [first, ...rest] = damageReportsFor(row, reports);
  if (!first) return null;
  return rest.length ? `${damageReviewWords(first)} (+${rest.length} more)` : damageReviewWords(first);
}

// ---------------------------------------------------------------------------
// Return to vendor (goods ticket 15B)
// ---------------------------------------------------------------------------

/** `data.rtv` of the movement read, off the generated client. */
export type RtvDetail = NonNullable<NonNullable<MovementWire["data"]>["rtv"]>;
export type RtvState = NonNullable<RtvDetail["state"]>;
export type RtvEventRow = NonNullable<RtvDetail["events"]>[number];
export type RtvLeftBehind = NonNullable<RtvDetail["left_behind"]>[number];
export type LeftBehindReason = RtvLeftBehind["reason"];
export type RtvSourcePool = NonNullable<MovementLineWire["source_pool"]>;

type PickupBody = NonNullable<
  paths["/api/goods-v1/outbound/movements/{id}/rtv-pickups"]["post"]["requestBody"]
>["content"]["application/json"];
type WithdrawalBody = NonNullable<
  paths["/api/goods-v1/outbound/movements/{id}/rtv-withdrawals"]["post"]["requestBody"]
>["content"]["application/json"];
/** What the pickup and withdrawal commands accept, without MutationMeta. */
export type RtvPickupInput = Omit<PickupBody, "command_id" | "contract_version">;
export type RtvWithdrawalInput = Omit<WithdrawalBody, "command_id" | "contract_version">;
export type WithdrawalReason = WithdrawalBody["reason"];

// ---------------------------------------------------------------------------
// Shipped RTV and vendor receipt (goods ticket 15F)
// ---------------------------------------------------------------------------

export type RtvShipment = NonNullable<RtvDetail["shipments"]>[number];
export type ShipmentStatus = NonNullable<RtvShipment["status"]>;
export type ShipmentAction = NonNullable<RtvShipment["allowed_actions"]>[number];

type ShipmentsPath = "/api/goods-v1/outbound/movements/{id}/rtv-shipments";
type ShipmentPath<T extends string> =
  `/api/goods-v1/outbound/movements/{id}/rtv-shipments/{shipment_id}/${T}`;
type BodyOf<P extends keyof paths> = paths[P] extends {
  post: { requestBody?: { content: { "application/json": infer B } } };
}
  ? Omit<B, "command_id" | "contract_version">
  : never;
/** What the five shipped-RTV commands accept, without MutationMeta. */
export type RtvShipmentInput = BodyOf<ShipmentsPath>;
export type RtvAcknowledgementInput = BodyOf<ShipmentPath<"acknowledgements">>;
export type RtvSourceReturnInput = BodyOf<ShipmentPath<"returns">>;
export type RtvPutawayInput = BodyOf<ShipmentPath<"putaways">>;
export type RtvEwayInput = BodyOf<ShipmentPath<"eway">>;
/** Goods ticket 15H: preparing the Owner's closure of a shipment's shortfall. */
export type RtvClosureInput = BodyOf<ShipmentPath<"shortfall-closures">>;
export type RtvPendingClosure = NonNullable<RtvShipment["pending_closure"]>;

/** Where one shipment stands (transfers PRD §7): departure is not receipt. */
export const SHIPMENT_STATUS_LABEL: Record<ShipmentStatus, string> = {
  awaiting_receipt: "On its way — waiting for the vendor to confirm receipt",
  short_acknowledged: "Vendor confirmed fewer pieces — a difference only the Owner closes",
  delivered: "Delivered — the vendor confirmed every piece",
  partly_returned: "Accounted for — part delivered, part came back",
  returned_to_source: "Came back — delivery failed",
  shortfall_closed: "Closed — the Owner recognised the pieces the vendor never confirmed as a shortfall",
};

/** A shipment's e-way evidence as three separate facts, in one line. */
export function ewayWords(eway: RtvShipment["eway"]): string {
  if (!eway) return "—";
  const left = eway.at_dispatch === "present" ? "left with an e-way bill" : "left without an e-way bill";
  if (!eway.reference) return left;
  const later = eway.attached_at ? `; ${eway.reference} attached later` : `; ${eway.reference}`;
  return `${left}${later}${eway.verified ? " · verified" : " · not verified"}`;
}

/** How many pieces of each line an acknowledgement may name: what left and did
 *  not come back. The server refuses more (ACK_EXCEEDS_SHIPPED). */
export function acknowledgeable(line: NonNullable<RtvShipment["lines"]>[number]): number {
  return (line.shipped_qty ?? 0) - (line.returned_qty ?? 0);
}

/** Quarantine outcomes PRD §4's statuses, in the words the screen uses. Before
 *  approval an RTV has no status: it is a draft or waiting for the Owner. */
export const RTV_STATE_LABEL: Record<RtvState, string> = {
  initiated: "Initiated — waiting for the vendor",
  completed: "Completed — every piece reached the vendor",
  closed_partially_returned: "Closed — partially returned",
  cancelled: "Cancelled — nothing left",
};

/** Quarantine outcomes PRD §5: why pieces were left behind at a pickup. */
export const LEFT_BEHIND_LABEL: Record<LeftBehindReason, string> = {
  vendor_rejected: "Vendor rejected the goods",
  collect_later: "Vendor will collect the balance later",
  not_ready: "Goods were not ready for handover",
  withdrawn_by_us: "Return withdrawn by us",
  other: "Other",
};

export const LEFT_BEHIND_REASONS = Object.keys(LEFT_BEHIND_LABEL) as LeftBehindReason[];

/** Whether another pickup is expected, as each reason already says; `other` says so
 *  itself (null here). Mirrors `goods_rtv.FURTHER_PICKUP`. */
export const FURTHER_PICKUP: Record<LeftBehindReason, boolean | null> = {
  vendor_rejected: false,
  collect_later: true,
  not_ready: true,
  withdrawn_by_us: false,
  other: null,
};

export const WITHDRAWAL_LABEL: Record<WithdrawalReason, string> = {
  vendor_rejected: "Vendor rejected the goods",
  withdrawn_by_us: "Return withdrawn by us",
  other: "Other",
};

export const WITHDRAWAL_REASONS = Object.keys(WITHDRAWAL_LABEL) as WithdrawalReason[];

export function rtvStateWords(state: RtvState | null | undefined, headState: string): string {
  if (state) return RTV_STATE_LABEL[state];
  return headState === "submitted" ? "Waiting for the Owner's approval" : "Draft — not approved yet";
}

/** Pieces of a stock row an RTV may take, as a hint: quarantined recorded pieces
 *  under a hold, or accepted good stock nobody holds — never reserved pieces, and
 *  never unvalued (pre-PT) custody. The server picks the exact pieces and refuses
 *  what it cannot take. */
export function returnableQty(row: StockSearchRow, locationKind: string | undefined): number {
  if (!hasSource(row) || !row.sku_id || row.valued_qty === 0) return 0;
  const valued = Math.min(row.valued_qty, row.physical_qty);
  if (locationKind === "quarantine") return Math.max(0, Math.min(valued, row.held_qty) - row.reserved_qty);
  if (row.condition !== "good" || row.accepted_qty === 0) return 0;
  return removableQty(row);
}

/** How many pieces of each line stay behind after collecting `taken`, and whether
 *  the reasons given cover exactly that many — the rule the server enforces
 *  (REASONS_INCOMPLETE), said before the button is pressed. */
export function leftBehindGap(
  outstanding: Record<string, number>,
  taken: Record<string, number>,
  reasons: { line_key: string; qty: number }[],
): Record<string, number> {
  const given: Record<string, number> = {};
  for (const reason of reasons) given[reason.line_key] = (given[reason.line_key] ?? 0) + reason.qty;
  const gap: Record<string, number> = {};
  for (const [key, qty] of Object.entries(outstanding)) {
    const left = qty - (taken[key] ?? 0);
    const missing = left - (given[key] ?? 0);
    if (missing !== 0) gap[key] = missing;
  }
  return gap;
}

/** One pickup, withdrawal or shipment said in a line of words. */
export function rtvEventWords(event: RtvEventRow): string {
  const who = event.actor?.name || event.actor?.id || "someone";
  const details = event.details ?? {};
  if (event.kind === "shipment") {
    // Goods ticket 15F: a shipment left for the vendor; it is not received yet.
    const via = details.carrier ? ` via ${details.carrier}` : "";
    const ref = details.evidence_reference ? ` (${details.evidence_reference})` : "";
    const eway = details.eway_at_dispatch === "present" ? "" : " · no e-way bill";
    const reports = details.closed_damage_report_ids?.length ?? 0;
    const closed = reports
      ? ` · closed ${reports} pending damage report(s) as shipped to vendor`
      : "";
    return `${event.quantity} sent to the vendor${via}${ref}${eway}${closed} · recorded by ${who}`;
  }
  if (event.kind === "pickup") {
    const by = details.collected_by ? ` by ${details.collected_by}` : "";
    const ref = details.evidence_reference ? ` (${details.evidence_reference})` : "";
    // Anand's 15B decision 3: the pickup that took back the last piece of a
    // pending damage report closed it.
    const reports = details.closed_damage_report_ids?.length ?? 0;
    const closed = reports
      ? ` · closed ${reports} pending damage report(s) as returned to vendor`
      : "";
    return `${event.quantity} collected${by}${ref}${closed} · recorded by ${who}`;
  }
  const reason = details.reason ? WITHDRAWAL_LABEL[details.reason] : "Withdrawn";
  const remark = details.remark ? ` — ${details.remark}` : "";
  return `${event.quantity} withdrawn: ${reason}${remark} · recorded by ${who}`;
}

// ---------------------------------------------------------------------------
// Write-off without physical disposal (goods ticket 15C)
// ---------------------------------------------------------------------------

/** `data.write_off` of the movement read, off the generated client. */
export type WriteOffDetail = NonNullable<NonNullable<MovementWire["data"]>["write_off"]>;

/** Pieces of a stock row a write-off may take, as a hint: recorded pieces held in
 *  the site's quarantine that nobody has reserved. A row is an aggregate, so the
 *  server picks the exact pieces and refuses what it cannot take — unvalued
 *  (pre-PT) custody, a damage memo value, vendor-owned goods, or pieces already
 *  written off. Goods still available are put on hold first. */
export function writeOffQty(row: StockSearchRow, locationKind: string | undefined): number {
  if (locationKind !== "quarantine" || !hasSource(row) || !row.sku_id || row.valued_qty === 0) return 0;
  const valued = Math.min(row.valued_qty, row.physical_qty);
  return Math.max(0, Math.min(valued, row.held_qty) - row.reserved_qty);
}

/** Where a write-off stands, in the words the screen uses. Before the Owner decides
 *  nothing is recognised; after, the value is gone and the goods are not. */
export function writeOffStateWords(detail: WriteOffDetail | null | undefined, headState: string): string {
  if (detail?.state === "written_off") return "Written off — the goods stay here, in quarantine";
  return headState === "submitted" ? "Waiting for the Owner's approval" : "Draft — nothing written off yet";
}


// ---------------------------------------------------------------------------
// Disposal of recorded stock (goods ticket 15D)
// ---------------------------------------------------------------------------

/** `data.header.disposal` of the movement read, off the generated client. */
export type DisposalFacts = NonNullable<
  NonNullable<NonNullable<MovementWire["data"]>["header"]>["disposal"]
>;
/** `data.disposal` of the movement read, off the generated client. */
export type DisposalDetail = NonNullable<NonNullable<MovementWire["data"]>["disposal"]>;
export type DisposalMethod = DisposalFacts["method"];

/** Quarantine outcomes PRD §7.2's two methods. Donation and sale as damaged
 *  merchandise are not offered (the server refuses them too). */
export const DISPOSAL_METHOD_LABEL: Record<DisposalMethod, string> = {
  destruction: "Destroyed",
  scrap_handover: "Handed over for scrap/recycling",
};

export const DISPOSAL_METHODS = Object.keys(DISPOSAL_METHOD_LABEL) as DisposalMethod[];

/** Where a disposal stands, in the words the screen uses. Before the Owner decides
 *  the goods are still on the books; after, exactly the disposed pieces are gone. */
export function disposalStateWords(detail: DisposalDetail | null | undefined, headState: string): string {
  if (detail?.state === "disposed") return "Disposed of — these pieces have left the site";
  return headState === "submitted"
    ? "Waiting for the Owner's approval"
    : "Draft — the goods are still on the books";
}

/** How the loss of the disposed pieces stands: recognised by this disposal at their
 *  recorded cost, or already recognised by the write-off it follows — never twice. */
export function disposalValueWords(detail: DisposalDetail): string {
  if (detail.value_basis === "written_off") {
    const number = detail.write_off?.number ?? "its write-off";
    return `Already recognised by ${number}; this disposal recognises no loss again`;
  }
  return "Recognised by this disposal, at each piece's own recorded cost";
}

/** A `datetime-local` input's value (the browser's own time zone) as the ISO
 *  date-time with offset the server needs; `null` when it does not parse. */
export function localToIso(value: string): string | null {
  if (!value) return null;
  const when = new Date(value);
  return Number.isNaN(when.getTime()) ? null : when.toISOString();
}
