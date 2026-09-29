// PT preparation and approval — the wire shapes E098/E099/E122-E133/E169/E170/E234
// answer, and the pure rules the two screens (ticket 06) share. Nothing here
// recomputes a server decision (a calculated cell, a reconciliation, a policy
// band): what lives here is how a *reader* groups, labels and orders what the
// server already decided.

import { api } from "./api";

/** One row issue, as `price_line`/`validate_rows` attach it (design §3.4). */
export interface PtRowIssue {
  code: string;
  field?: string | null;
  message: string;
  line_key?: string | null;
}

/** The server-calculated half of a row (`ptmapper.goods_calc.Calculated`). */
export interface PtCalculated {
  p_rate_paise: string | null;
  mrp_paise: string | null;
  basic_paise: string | null;
  input_tax_pct: string | null;
  output_tax_pct: string | null;
  margin_pct: string | null;
  pricing_margin_pct: string | null;
  transport_pct: string | null;
  direction: string;
}

/** What a preparer supplied: BASIC/MRP, and the two check values a canonical
 *  upload can carry (`check_p_rate_paise`/`check_mrp_paise`) — never editable
 *  results, only what the row is checked against. */
export interface PtSupplied {
  basic_paise?: string | null;
  mrp_paise?: string | null;
  check_p_rate_paise?: string | null;
  check_mrp_paise?: string | null;
}

export interface PtLine {
  line_key: string;
  /** The frozen line's own row id (ticket 11) - only present when a detail read
   *  names an explicit `?version=`, since only an official version has one.
   *  Documented on E099's response schema; the rest of this interface describes
   *  the frozen payload, whose shape is the PT's own vocabulary rather than a
   *  fixed DTO, so it stays hand-written. */
  official_line_id?: string;
  sku_id: string | null;
  attributes: unknown[];
  season_id: string | null;
  alias_id: string | null;
  alias_as_used: string | null;
  qty: number;
  hsn: string | null;
  source_ref: string | null;
  direction_override?: string | null;
  coverage_requests: { lot_id: string; qty: number }[];
  supplied: PtSupplied;
  calculated?: PtCalculated;
  issues?: PtRowIssue[];
  reviewed: boolean;
  row_hash?: string;
  profile_version_id?: string;
  rate_version_id?: string;
  tax_version_id?: string;
}

export interface PtHeader {
  purpose: string;
  receipt_kind: "primary" | "supplement";
  grn_id: string;
  site_id: string;
  profile_version_id: string;
  direction: string;
  source: "typed" | "canonical_upload";
  source_evidence_ids: string[];
}

export interface PtHistoryEntry {
  id: string;
  kind: string;
  actor_id: string | null;
  recorded_at: string;
  revision: number | null;
  outcome: string | null;
  reason_code: string | null;
  evidence_ids: string[];
  related_document_id: string | null;
}

export interface PtData {
  header: PtHeader;
  lines: { items: PtLine[]; next_cursor: string | null; total: number };
  history: { items: PtHistoryEntry[]; next_history_cursor: string | null };
}

/** The stable document an approval is about (GSA-T04). Absent for subjects that
 *  are not documents, such as a configuration version. */
export interface ParentDocument {
  id: string;
  kind: string;
  purpose: string;
  number: string | null;
  revision: number;
}

/** `ApprovalDTO` (design §6.1). It names the exact revision it was submitted
 *  against and the exact document it belongs to, so a checker's screen opens
 *  that revision rather than matching a title or taking a revision off a later
 *  read of the subject (GSA-T04). */
export interface ApprovalDTO {
  id: string;
  subject_id: string;
  subject_kind: string;
  /** The subject's revision at submission. E234's `expected_revision` is this. */
  subject_revision: number;
  parent_document: ParentDocument | null;
  reviewed_hash: string;
  state: string;
  maker: { id: string; name: string };
  required_roles: string[];
  requested_action: string;
  policy_version_id: string | null;
  site_id: string | null;
  brand_id: string | null;
  title: string;
  requested_at: string | null;
  decision_at: string | null;
  reason_code: string | null;
  reconciliation: PtReconciliation | null;
}

/** `reconcile_receipt`'s own shape: proposed coverage against the GRN's
 *  counted lots, one row per lot, plus the same totals (design §5.5). Also
 *  what E127 returns beside the submitted document, for the reconciliation
 *  panel the UX brief asks submit to open. */
export interface PtReconciliationLine {
  line_key: string | null;
  lot_id: string;
  counted_qty: number;
  proposed_qty: number;
  already_covered_qty: number;
  held_uncovered_qty: number;
  disposed_uncovered_qty: number;
  issues: PtRowIssue[];
}

export interface PtReconciliation {
  passed: boolean;
  issues: PtRowIssue[];
  lines: PtReconciliationLine[];
  totals: {
    counted_qty: number;
    proposed_qty: number;
    already_covered_qty: number;
    held_uncovered_qty: number;
    disposed_uncovered_qty: number;
    claimed_qty: number | null;
    value_paise: string | null;
  };
}

// ---------------------------------------------------------------------------
// Row issues
// ---------------------------------------------------------------------------

/** Plain-language names for the row issue codes the UX brief names by example
 *  (`COST_ABOVE_MRP`, `MRP_SLAB_GAP`, `DERIVED_MISMATCH`) and the rest
 *  `goods_calc`/`validate_rows` can raise. An unlisted code still renders —
 *  humanised from its own spelling — rather than disappearing. */
export const ISSUE_LABELS: Record<string, string> = {
  COST_ABOVE_MRP: "Cost is above MRP",
  MRP_SLAB_GAP: "No tax slab fits the calculated MRP",
  DERIVED_MISMATCH: "Supplied value differs from the calculation",
  TAX_SLAB_MISSING: "No tax slab fits the supplied MRP",
  COST_ZERO: "Cost cannot be zero",
  MRP_ZERO: "MRP cannot be zero",
  PERCENT_OUT_OF_RANGE: "A rate is out of range",
  MONEY_OUT_OF_RANGE: "An amount is out of range",
  DIRECTION_INVALID: "Unknown pricing direction",
  DIRECTION_NOT_ALLOWED: "This profile does not allow this direction",
  REQUIRED: "A required value is missing",
  QTY_INVALID: "Quantity must be 1 to 999,999",
  IDENTITY_UNRESOLVED: "No SKU resolved for this row",
  PROPOSAL_UNCONFIRMED: "The SKU is an unconfirmed proposal",
  IDENTITY_RETIRED: "The SKU is retired",
  IDENTITY_PROFILE_MISMATCH: "The SKU is not of this profile's product family",
  DEFINING_ATTRIBUTE_UNKNOWN: "A defining attribute is not known",
  ATTRIBUTE_MISMATCH: "An attribute does not match the SKU",
  COVERAGE_SKU_MISMATCH: "Coverage must use the SKU the goods were counted as",
  COVERAGE_SOURCE_INVALID: "Coverage must use a lot of this GRN",
  COVERAGE_QTY_MISMATCH: "Coverage does not add up to the line quantity",
  SEASON_REQUIRED: "A real mapped season is required",
  HSN_REQUIRED: "HSN is required",
  VALUE_MISSING: "P RATE and MRP must be calculated and positive",
  ROW_NOT_REVIEWED: "This row has not been reviewed at its current values",
};

export function issueLabel(issue: PtRowIssue): string {
  return ISSUE_LABELS[issue.code] || issue.message || issue.code.replace(/_/g, " ").toLowerCase();
}

/** The row issues that belong to one line — matched by `line_key`, the shape
 *  every PT refusal and calculated issue carries it in. */
export function issuesFor(line: PtLine): PtRowIssue[] {
  return line.issues ?? [];
}

// ---------------------------------------------------------------------------
// Review progress
// ---------------------------------------------------------------------------

export interface ReviewProgress {
  reviewed: number;
  total: number;
}

/** "N of M rows reviewed" (UX brief) — counted from the `reviewed` flag the
 *  server itself computed against each row's current hash (E099 `_line_item`),
 *  never from a client-side guess at what changed. */
export function reviewProgress(lines: PtLine[]): ReviewProgress {
  return { reviewed: lines.filter((line) => line.reviewed).length, total: lines.length };
}

export function allReviewed(lines: PtLine[]): boolean {
  return lines.length > 0 && lines.every((line) => line.reviewed);
}

// ---------------------------------------------------------------------------
// Reconciliation
// ---------------------------------------------------------------------------

export interface ReconciliationView {
  passed: boolean;
  issues: PtRowIssue[];
  totalProposedQty: number | null;
  totalValuePaise: string | null;
  /** Pieces this PT proposes that the GRN's own counts cannot support — the
   *  "gap" the UX brief asks the panel to name, never silently absorbed into
   *  the total. */
  gapQty: number;
}

export function reconciliationView(
  reconciliation: PtReconciliation | null | undefined,
): ReconciliationView | null {
  if (!reconciliation) return null;
  const totals = reconciliation.totals;
  return {
    passed: reconciliation.passed,
    issues: reconciliation.issues ?? [],
    totalProposedQty: totals?.proposed_qty ?? null,
    totalValuePaise: totals?.value_paise ?? null,
    gapQty: Math.max(0, (totals?.proposed_qty ?? 0) - (totals?.counted_qty ?? 0)),
  };
}

// ---------------------------------------------------------------------------
// Approval queue ordering and staleness
// ---------------------------------------------------------------------------

/** Newest first (UX brief), from `requested_at` — the inbox itself answers
 *  oldest-first (E169 orders by `created_at` ascending, the fair-queue order a
 *  workday works it in), so the screen re-orders for reading, not for working. */
export function newestFirst<T extends { requested_at: string | null }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => (b.requested_at ?? "").localeCompare(a.requested_at ?? ""));
}

/** A pending PT approval is superseded the moment the document it names is no
 *  longer the exact submitted revision it was requested against — a further
 *  row edit invalidates the submission (E124 step 11), a recall returns it to
 *  draft (E128), and a later resubmission opens a fresh request under a new
 *  hash. The approval now carries its own `subject_revision`, so this compares
 *  two exact numbers as well as the state and hash it always did. */
/** The PT state an approval's subject must still be in for `reviewed_hash` to
 *  mean anything — `pt.approve.receipt` reviews a submitted draft revision
 *  (E127); `pt.reversal.approve` reviews the live official version instead
 *  (E130 pins `reviewed_hash` to `live.content_hash`, and the subject is
 *  already official when the reversal is requested). An unrecognised action
 *  names no live state this reads honestly. */
const EXPECTED_STATE: Record<string, string> = {
  "pt.approve.receipt": "submitted",
  "pt.reversal.approve": "official",
};

export function isSuperseded(
  approval: ApprovalDTO,
  pt: { state: string; content_hash: string; revision?: number } | null,
): boolean {
  if (!pt) return false;
  const expected = EXPECTED_STATE[approval.requested_action];
  if (expected && pt.state !== expected) return true;
  // The exact fact, now that the approval carries it: the document has moved on
  // from the revision this request was submitted against.
  if (typeof pt.revision === "number" && pt.revision !== approval.subject_revision) return true;
  // The submitted-draft path's `content_hash` is the reviewed revision's own
  // hash (an exact match against `reviewed_hash` when nothing has changed).
  // The official-version path has no such field to compare: a live head with
  // no pending draft revision answers `content_hash` from `resource_dto`'s
  // own wrapper hash, not `live_version.content_hash` (E099's `_content`
  // returns "" when `head.draft_revision` is None) — comparing it to
  // `reviewed_hash` would read a merely-quiet document as superseded. The
  // state check above is what a reversal approval actually has to stay true.
  if (approval.requested_action === "pt.reversal.approve") return false;
  return pt.content_hash !== approval.reviewed_hash;
}

/** Best-effort, client-side "you prepared or reviewed this" signal — the exact
 *  rule the server enforces (`_refuse_self_decision`) also catches anyone who
 *  marked a row reviewed, and the API never says who that was (GSA-T06 open
 *  item). Matching the request's `maker` at least catches the common case —
 *  the preparer who pressed "Submit" — pre-emptively; the server's own
 *  `SELF_APPROVAL` refusal is the backstop for the rest. */
export function isLikelySelfApproval(approval: ApprovalDTO, humanId: string | null | undefined): boolean {
  return Boolean(humanId) && approval.maker.id === humanId;
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

/** The attempt and refusal history the approvals viewer shows: submissions,
 *  recalls, decisions and reversal events, newest first (the PT detail already
 *  answers history that way — design §5.8). */
export function attemptHistory(history: PtHistoryEntry[]): PtHistoryEntry[] {
  const KINDS = new Set([
    "submitted",
    "withdrawn",
    "approved",
    "rejected",
    "reversal_requested",
    "reversed",
  ]);
  return history.filter((entry) => KINDS.has(entry.kind));
}

// ---------------------------------------------------------------------------
// Money entry
// ---------------------------------------------------------------------------

/** A rupee amount as typed in a grid cell, to the integer-paise string the
 *  wire takes (`core.goods_money.paise_from_json`: digits only, no decimal
 *  point). Blank types to `null` (unknown, never zero). `undefined` means
 *  what is typed so far does not parse as an amount — not necessarily wrong,
 *  possibly mid-edit ("95.") — and the caller keeps showing the raw text
 *  rather than writing anything from it yet. */
export function rupeesToPaiseString(text: string): string | null | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const typed = trimmed.replace(/[,\s]/g, "");
  if (!/^\d+(\.\d{1,2})?$/.test(typed)) return undefined;
  const [whole, fraction = ""] = typed.split(".");
  const paise = BigInt(whole) * 100n + BigInt((fraction + "00").slice(0, 2));
  return paise.toString();
}

// ---------------------------------------------------------------------------
// Pinned versions
// ---------------------------------------------------------------------------

export interface PinnedVersions {
  profileVersionId: string | null;
  rateVersionId: string | null;
  taxVersionId: string | null;
}

/** The profile/rate/tax versions this revision priced under, read off its own
 *  lines (`price_line` stamps every line with the versions it used) rather
 *  than the header, which only names `profile_version_id`. A revision with no
 *  lines yet has pinned nothing to show. */
export function pinnedVersions(lines: PtLine[]): PinnedVersions {
  const first = lines[0];
  return {
    profileVersionId: first?.profile_version_id ?? null,
    rateVersionId: first?.rate_version_id ?? null,
    taxVersionId: first?.tax_version_id ?? null,
  };
}

// ---------------------------------------------------------------------------
// Evidence upload
// ---------------------------------------------------------------------------

/** The SHA-256 of a chosen file, hex, as `expected_sha256` takes it. */
export async function sha256Hex(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Upload one evidence file scoped to a site and return its id. Every screen that
 *  creates a document from a file (PT preparation, opening manifests) sends the
 *  same multipart shape, including the digest the server checks the bytes against. */
export async function uploadEvidence(
  file: File,
  // `siteId` follows its caller's own read; a GRN header whose site is not yet
  // known sends what it has, exactly as before this was shared.
  { kind, siteId }: { kind: string; siteId: string | null },
): Promise<string> {
  const form = new FormData();
  form.append("file", file);
  form.append("command_id", crypto.randomUUID());
  form.append("contract_version", "goods-v1");
  form.append("kind", kind);
  form.append("expected_sha256", await sha256Hex(file));
  form.append("scope", JSON.stringify({ scope_kind: "sites", site_ids: [siteId] }));
  const uploaded = await api.post<{ id: string }>("/goods-v1/files/uploads", form);
  return uploaded.data.id;
}
