// PT Work (OPS-17, store and warehouse operations PRD §5.1 and §5.4): the
// warehouse's three PT tabs - To prepare, To approve and Mapping rules - and
// the few pure rules the screen needs. Everything here is presentation: which
// tab a person is shown follows the grants the server already enforces, the
// To prepare list is read off the receiving inbox and the PT list, and the
// hash a mapping decision quotes is the server's own canonical hash of the row
// the person reviewed. Nothing here decides anything.

import type { InboxItem } from "./goodsReceiving";

export type PtWorkTab = "prepare" | "approve" | "rules";

export const PT_WORK_TABS: { tab: PtWorkTab; label: string }[] = [
  { tab: "prepare", label: "To prepare" },
  { tab: "approve", label: "To approve" },
  { tab: "rules", label: "Mapping rules" },
];

/** Preparing a PT: a receipt PT, or an opening one. */
export const PREPARE_ACTIONS = ["pt.prepare", "pt.prepare.opening"];

/** The approvals queue: the checkers, and the preparers whose own reissue lives
 *  there (design E131). The same grants the queue itself always answered. */
export const APPROVE_ACTIONS = [
  "pt.approve.receipt",
  "pt.approve.opening",
  "pt.reversal.approve",
  "pt.reversal.request",
  ...PREPARE_ACTIONS,
];

/** Who reads the review queue and the proposals list (E227/E228: the server's
 *  `VOCAB_ACTIONS`). */
export const RULE_READ_ACTIONS = [
  "product.master.manage",
  "product.master.propose",
  "crosswalk.manage",
  "crosswalk.propose",
  "identity.resolve",
  ...PREPARE_ACTIONS,
];

/** Who may propose a target for a waiting source word (E230: `PROPOSER_ACTIONS`). */
export const RULE_PROPOSE_ACTIONS = [
  "crosswalk.propose",
  "crosswalk.manage",
  "product.master.propose",
  ...PREPARE_ACTIONS,
];

/** Who confirms or rejects a proposed rule: the product-master owner, and only
 *  them (E231 answers `crosswalk.manage` alone). */
export const RULE_DECIDE_ACTION = "crosswalk.manage";

/** Every grant that opens PT Work at all - the menu line's gate. */
export const PT_WORK_ACTIONS = [...new Set([...APPROVE_ACTIONS, ...RULE_READ_ACTIONS])];

function holdsAny(actions: readonly string[], wanted: readonly string[]): boolean {
  return wanted.some((action) => actions.includes(action));
}

/** The tabs this person is shown, in the PRD's order. */
export function ptWorkTabs(actions: readonly string[]): PtWorkTab[] {
  const out: PtWorkTab[] = [];
  if (holdsAny(actions, PREPARE_ACTIONS)) out.push("prepare");
  if (holdsAny(actions, APPROVE_ACTIONS)) out.push("approve");
  if (holdsAny(actions, RULE_READ_ACTIONS)) out.push("rules");
  return out;
}

/** The tab a URL names, when this person is shown it; otherwise their first. */
export function resolvePtWorkTab(
  shown: PtWorkTab[],
  asked: string | null | undefined,
): PtWorkTab | null {
  return shown.find((tab) => tab === asked) ?? shown[0] ?? null;
}

/** PT Work's address: a tab, or the grid for one PT, or the start for one GRN.
 *  A PT or a GRN always opens on To prepare, which is where the grid lives. */
export function ptWorkPath(target: {
  tab?: PtWorkTab;
  pt?: string | null;
  grn?: string | null;
}): string {
  const query = new URLSearchParams();
  if (target.pt) {
    query.set("pt", target.pt);
  } else if (target.grn) {
    query.set("grn", target.grn);
  } else if (target.tab && target.tab !== "prepare") {
    query.set("tab", target.tab);
  }
  const text = query.toString();
  return text ? `/goods/pt-work?${text}` : "/goods/pt-work";
}

// ---------------------------------------------------------------------------
// To prepare
// ---------------------------------------------------------------------------

/** A row of the PT list (E098), as much of it as To prepare reads. */
export interface PtSummary {
  id: string;
  number: string | null;
  state: string;
  purpose: string;
  site_id: string;
  created_at: string;
}

/** One piece of PT work waiting for a preparer. */
export interface PrepareRow {
  key: string;
  /** A receipt whose good pieces no live PT covers yet, or a PT still a draft. */
  kind: "receipt" | "draft";
  /** What a person calls it: the delivery, or the draft's own purpose. */
  label: string;
  grnId: string | null;
  grnNumber: string | null;
  ptId: string | null;
  ptNumber: string | null;
  siteId: string;
  since: string;
}

/**
 * Everything To prepare lists, in two parts, each read off the server's own
 * answer rather than worked out here:
 *
 *   · the deliveries the inbox says are waiting on their PT - a GRN with good
 *     pieces no PT covers yet, with its draft PT when one has been started;
 *   · every other PT still a draft - an opening PT, a supplement - that no
 *     delivery above already carries.
 */
export function toPrepareRows(inbox: InboxItem[], pts: PtSummary[]): PrepareRow[] {
  const receipts: PrepareRow[] = inbox
    .filter((item) => item.next_step === "pt_prepare" && item.grn_id)
    .map((item) => ({
      key: `grn:${item.grn_id}`,
      kind: "receipt",
      label: item.reference,
      grnId: item.grn_id,
      grnNumber: item.grn_number,
      ptId: item.pt_id,
      ptNumber: item.pt_number,
      siteId: item.site_id,
      since: item.updated_at || item.arrived_at,
    }));
  const carried = new Set(receipts.map((row) => row.ptId).filter(Boolean));
  const drafts: PrepareRow[] = pts
    .filter((pt) => pt.state === "draft" && !carried.has(pt.id))
    .map((pt) => ({
      key: `pt:${pt.id}`,
      kind: "draft",
      label: pt.purpose === "opening" ? "Opening stock PT" : "Receipt PT",
      grnId: null,
      grnNumber: null,
      ptId: pt.id,
      ptNumber: pt.number,
      siteId: pt.site_id,
      since: pt.created_at,
    }));
  return [...receipts, ...drafts];
}

// ---------------------------------------------------------------------------
// Mapping rules
// ---------------------------------------------------------------------------

/** One row of the review queue (E227) or the proposals list (E228): a brand's
 *  word waiting for its KDPS value, or a proposed value waiting for the owner. */
export interface MappingChoice {
  id: string;
  dimension: string;
  value: string;
  label: string;
  state: string;
  source_key: string | null;
  issuer_key: string | null;
  target_id: string | null;
  target_label: string | null;
  profile_version_id: string | null;
  revision: number;
  match: string | null;
  reason: string | null;
}

/** Sorted-key, whitespace-free JSON - `core.canonical.canonical_json` for the
 *  JSON-native values an API row carries (strings, integers, booleans, null,
 *  arrays and objects). A float has no canonical spelling there and none here. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isInteger(value)) throw new TypeError(`no canonical form for ${value}`);
    return String(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`).join(",")}}`;
  }
  throw new TypeError(`no canonical form for ${typeof value}`);
}

/** The SHA-256 of a row's canonical JSON: the `reviewed_hash` a mapping decision
 *  quotes, so the server can refuse a row that changed since it was read. */
export async function contentHash(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonicalJson(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Whose words these are, in a reader's terms: a brand's file, a vendor's, or
 *  every issuer's. */
export function issuerLabel(issuer: string | null, brandName: (id: string) => string): string {
  if (!issuer || issuer === "*") return "Every file";
  if (issuer.startsWith("brand:")) return `${brandName(issuer.slice(6))} files`;
  return `${issuer} files`;
}

// ---------------------------------------------------------------------------
// New items waiting for the product-master owner (OPS-17A)
// ---------------------------------------------------------------------------

/** One pending approval a new item waits on (`/goods-v1/masters/item-proposals`). */
export interface ItemApproval {
  id: string;
  part: "style" | "sku";
  revision: number;
  reviewed_hash: string | null;
  can_decide: boolean;
}

/** A new item a PT proposed, as the server lists it for this person. */
export interface ItemProposal {
  id: string;
  kind: "sku" | "style";
  style_id: string;
  style_code: string;
  style_is_new: boolean;
  style_shared: boolean;
  brand_id: string;
  brand_name: string;
  profile_family: string;
  describing: { field_id: string; label: string }[];
  pt: { id: string; number: string | null; site_id: string | null } | null;
  proposed_by: { id: string; name: string };
  proposed_at: string | null;
  approvals: ItemApproval[];
  can_decide: boolean;
}

/** Why the product-master owner turns a new item down - the reason the PT row
 *  then shows its preparer. */
export const ITEM_REJECT_REASONS: { code: string; label: string }[] = [
  { code: "ITEM_ALREADY_EXISTS", label: "The item already exists" },
  { code: "ITEM_DETAILS_WRONG", label: "The item's details are wrong" },
  { code: "ITEM_NOT_STOCKED", label: "We do not stock this item" },
];

export function itemRejectReasonLabel(code: string | null | undefined): string {
  if (!code) return "no reason given";
  return (
    ITEM_REJECT_REASONS.find((reason) => reason.code === code)?.label ??
    code.replace(/_/g, " ").toLowerCase()
  );
}

/**
 * The approvals one decision on a new item runs through E234, in order.
 *
 *   · Confirm: the new style first, then the item - a SKU is confirmed only
 *     under a confirmed style.
 *   · Reject: the item first, then its new style, unless another waiting item
 *     uses that style too; that one keeps it.
 */
export function itemDecisions(item: ItemProposal, decision: "approve" | "reject"): ItemApproval[] {
  const style = item.approvals.filter((approval) => approval.part === "style");
  const sku = item.approvals.filter((approval) => approval.part === "sku");
  if (decision === "approve") return [...style, ...sku];
  return [...sku, ...(item.style_shared ? [] : style)];
}

/** The describing values a preparer proposed, in a reader's words. */
export function describingText(item: Pick<ItemProposal, "describing">): string {
  return item.describing.map((entry) => `${entry.field_id}: ${entry.label}`).join(", ");
}
