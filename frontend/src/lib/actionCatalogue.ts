// One language for Action Needed, Alerts and their top-bar popups (Anand,
// 23 Sep 2026: "tag them better, so a person understands them").
//
// Every approval, exception, deadline and notification gets a *topic* (which
// part of the business it is about) and a plain sentence: what happened, and
// what to do about it. It lives in this one file so the page, the popup and
// the Alerts feed can never name the same thing two ways.
//
// Colour is deliberately not part of a topic. On these screens colour means
// urgency (design-language §4: red overdue, amber due today, grey later), so a
// topic is a neutral chip with an icon and a word, never a seventh colour.
import type { LucideIcon } from "lucide-react";
import {
  Banknote,
  Boxes,
  ClipboardList,
  PackageOpen,
  ServerCog,
  Settings2,
  ShieldCheck,
  Store,
  Tag,
  Truck,
  Undo2,
} from "lucide-react";

import { dayHeading } from "../shell/bellModel";
import {
  countsFor,
  kindLabel,
  sortExceptions,
  type ExceptionRow,
  type NotificationRow,
} from "./goodsExceptions";
import type { ApprovalDTO } from "./goodsPt";
import { ptWorkPath } from "./ptWork";

export type Topic =
  | "receiving"
  | "acceptance"
  | "transfers"
  | "stock"
  | "opening"
  | "returns"
  | "cash"
  | "offers"
  | "setup"
  | "access"
  | "system";

export interface TopicInfo {
  label: string;
  icon: LucideIcon;
}

export const TOPICS: Record<Topic, TopicInfo> = {
  receiving: { label: "Receiving", icon: PackageOpen },
  acceptance: { label: "Store acceptance", icon: Store },
  transfers: { label: "Transfers", icon: Truck },
  stock: { label: "Stock", icon: Boxes },
  opening: { label: "Opening stock", icon: ClipboardList },
  returns: { label: "Returns to brand", icon: Undo2 },
  cash: { label: "Cash", icon: Banknote },
  offers: { label: "Offers", icon: Tag },
  setup: { label: "Setup", icon: Settings2 },
  access: { label: "Access", icon: ShieldCheck },
  system: { label: "System", icon: ServerCog },
};

/** The order topics are offered as filters: the goods' own journey first
 *  (in, accepted, moved, held), then the business around it. */
export const TOPIC_ORDER: readonly Topic[] = [
  "receiving",
  "acceptance",
  "transfers",
  "stock",
  "opening",
  "returns",
  "cash",
  "offers",
  "setup",
  "access",
  "system",
];

// --------------------------------------------------------------------------
// Exceptions
// --------------------------------------------------------------------------

export interface KindGuide {
  topic: Topic;
  /** What happened, as a person would say it. */
  title: string;
  /** What to do about it, in one sentence. */
  help: string;
}

/** Every kind `alerts.goods_services.EXCEPTION_DEFAULTS` registers, the ones
 *  nothing raises yet included, so a later ticket that starts raising one
 *  already reads as words. */
const KIND_GUIDE: Record<string, KindGuide> = {
  grn_awaiting_pt: {
    topic: "receiving",
    title: "Goods received, PT not made yet",
    help: "Make the PT for these goods so the store can take them in.",
  },
  receipt_discrepancy: {
    topic: "receiving",
    title: "Goods don't match the booking",
    help: "Check what is short, extra, wrong or damaged, and decide what happens to it.",
  },
  three_way_mismatch: {
    topic: "receiving",
    title: "Booking, invoice and count don't agree",
    help: "Check each line's booked, invoiced and counted figures, then correct the booking, the invoice or the count.",
  },
  unbooked_arrival: {
    topic: "receiving",
    title: "Goods arrived without a booking",
    help: "Link the delivery to its booking, or confirm it has none.",
  },
  label_print_failed: {
    topic: "receiving",
    title: "Labels didn't print",
    help: "Print the labels again.",
  },
  approval_pending: {
    topic: "stock",
    title: "Waiting for an approval",
    help: "A second person has to approve this. If you're the approver, it's also under Approvals.",
  },
  transfer_stalled: {
    topic: "transfers",
    title: "Transfer hasn't arrived",
    help: "Find the goods, or record what happened to them.",
  },
  transfer_discrepancy: {
    topic: "transfers",
    title: "Transfer problem",
    help: "Record what is missing, or release the stock that blocks it.",
  },
  eway_missing: {
    topic: "transfers",
    title: "E-way bill missing",
    help: "Attach the e-way bill number to the movement.",
  },
  acceptance_remaining: {
    topic: "acceptance",
    title: "Goods still to accept at the store",
    help: "Scan in the rest of the delivered goods.",
  },
  acceptance_discrepancy: {
    topic: "acceptance",
    title: "Wrong or damaged goods at the store",
    help: "Check the goods and add a note saying what you found.",
  },
  rtv_not_dispatched: {
    topic: "returns",
    title: "Return to brand not sent yet",
    help: "Send the goods back, or cancel the return.",
  },
  rtv_receipt_pending: {
    topic: "returns",
    title: "Returned goods not confirmed by the brand yet",
    help: "Record what the brand says it received, or the goods that came back.",
  },
  rtv_acknowledgement_discrepancy: {
    topic: "returns",
    title: "Brand confirmed fewer pieces than were sent",
    help: "Record a new confirmation if the brand finds the rest. Otherwise the warehouse asks the Owner to close the difference, with the brand's letter or a note, and the Owner approves it.",
  },
  count_session_stale: {
    topic: "stock",
    title: "Stock count left open",
    help: "A counting pass was left idle for a day. Resume it and finish, or cancel the count.",
  },
  stock_hold_active: {
    topic: "stock",
    title: "Stock on hold",
    help: "Release the hold, or decide what happens to the goods.",
  },
  opening_variance: {
    topic: "opening",
    title: "Opening stock count doesn't match",
    help: "Approve or reject the difference on the opening stock screen.",
  },
  opening_origin_unavailable: {
    topic: "opening",
    title: "Opening stock has no purchase record",
    help: "Confirm on the opening stock screen that no older record exists.",
  },
  setup_configuration_invalid: {
    topic: "setup",
    title: "Store setup is missing or wrong",
    help: "Put the store's setup right on its readiness page.",
  },
  setup_item_missing: {
    topic: "setup",
    title: "Setup item missing",
    help: "Add the missing setup item.",
  },
  site_readiness_failed: {
    topic: "setup",
    title: "New store isn't ready yet",
    help: "Fix the checks that failed before the store opens.",
  },
  site_closure_residual: {
    topic: "setup",
    title: "Closing store still has stock or staff",
    help: "Move or settle what is left before the store closes.",
  },
  master_resolution_required: {
    topic: "setup",
    title: "A supplier code isn't linked to a product",
    help: "Link the code to the right product.",
  },
  identity_ambiguity: {
    topic: "setup",
    title: "A scanned code matches two products",
    help: "Choose which product it really is.",
  },
  unresolved_identity: {
    topic: "setup",
    title: "A product code isn't recognised",
    help: "Link the code to the right product.",
  },
  privileged_change_review: {
    topic: "access",
    title: "Access change to review",
    help: "Someone changed a login or access, ran an export or a recovery. A different person checks each change.",
  },
  export_failed: {
    topic: "system",
    title: "A download (export) failed",
    help: "Ask for the export again.",
  },
  // Ticket 41 (ST-MNY-2): a day-close cash count that did not match.
  cash_variance: {
    topic: "cash",
    title: "Cash count didn't match",
    help: "Find out why the drawer was short or over, and write down the reason.",
  },
  // Ticket 42 (ST-MNY-3): a petty cash spend saved with no bill photo.
  petty_cash_no_bill: {
    topic: "cash",
    title: "A petty cash bill photo is missing",
    help: "Take a photo of the bill and add it to the spend on Money, Petty Cash.",
  },
  series_hole: {
    topic: "system",
    title: "A document number is missing",
    help: "Explain why the number was skipped.",
  },
  post_restore_recovery: {
    topic: "system",
    title: "Data was restored",
    help: "Re-enter any work that was lost, or confirm nothing was.",
  },
  evidence_protection: {
    topic: "system",
    title: "Record check failed",
    help: "Tell the platform team straight away.",
  },
};

/** Where one kind covers two different stories, the reason picks the story.
 *  Such rows form their own group, so each group still says one thing. */
const REASON_GUIDE: Record<string, KindGuide> = {
  "approval_pending:VALUE_DAMAGE_APPROVAL": {
    topic: "receiving",
    title: "Damaged goods value waiting for approval",
    help: "A second person approves the value of damaged goods. If you're the approver, it's also under Approvals.",
  },
  "approval_pending:RELEASE_APPROVAL": {
    topic: "stock",
    title: "Stock release waiting for approval",
    help: "A second person approves taking stock off hold. If you're the approver, it's also under Approvals.",
  },
  "transfer_discrepancy:TRANSIT_SHORTAGE": {
    topic: "transfers",
    title: "Transfer arrived short",
    help: "Record what is missing so someone owns the difference.",
  },
  "transfer_discrepancy:RESERVED_STOCK_HELD": {
    topic: "transfers",
    title: "Transfer blocked by stock on hold",
    help: "Release the hold, or decide what happens to the goods.",
  },
  "acceptance_discrepancy:WRONG_GOODS": {
    topic: "acceptance",
    title: "Wrong goods found at the store",
    help: "Keep them apart, check them, and add a note saying what you found.",
  },
  "acceptance_discrepancy:DAMAGED_AT_ACCEPTANCE": {
    topic: "acceptance",
    title: "Damaged goods found at the store",
    help: "The damaged pieces are on hold. Add a note saying what you found.",
  },
};

/** The key a row is grouped under: its kind, or its kind and reason where the
 *  reason tells a different story. Safe in a test id (no spaces). */
export function guideKey(row: Pick<ExceptionRow, "kind" | "reason_code">): string {
  const specific = row.reason_code ? `${row.kind}:${row.reason_code}` : "";
  return specific && REASON_GUIDE[specific] ? specific : row.kind;
}

/** The plain story for one exception. A kind this build has never seen still
 *  reads as words, filed under System, rather than as a blank. */
export function kindGuide(kind: string, reasonCode: string | null = null): KindGuide {
  const specific = reasonCode ? REASON_GUIDE[`${kind}:${reasonCode}`] : undefined;
  return (
    specific ??
    KIND_GUIDE[kind] ?? {
      topic: "system",
      title: kindLabel(kind),
      help: "Open the details to see how this one is fixed.",
    }
  );
}

export function exceptionTopic(row: Pick<ExceptionRow, "kind" | "reason_code">): Topic {
  return kindGuide(row.kind, row.reason_code).topic;
}

/** Every kind with a written guide, for the test that keeps this file and
 *  `KIND_LABEL` in step. */
export const GUIDED_KINDS: readonly string[] = Object.keys(KIND_GUIDE);

/** One card on Action Needed: every open exception that tells the same story. */
export interface ExceptionCard {
  key: string;
  kind: string;
  guide: KindGuide;
  /** Most urgent first (`sortExceptions`). */
  rows: ExceptionRow[];
  overdue: number;
  dueToday: number;
}

/** Open exceptions as the page shows them: one card per story, and the card
 *  holding the most urgent row first, so a morning's overdue work leads. Eighty
 *  of one kind become one card with a count, not eighty lines. */
export function groupForPage(rows: ExceptionRow[], nowMs: number = Date.now()): ExceptionCard[] {
  const groups = new Map<string, ExceptionCard>();
  for (const row of sortExceptions(rows)) {
    const key = guideKey(row);
    const group = groups.get(key);
    if (group) group.rows.push(row);
    else
      groups.set(key, {
        key,
        kind: row.kind,
        guide: kindGuide(row.kind, row.reason_code),
        rows: [row],
        overdue: 0,
        dueToday: 0,
      });
  }
  return [...groups.values()].map((group) => {
    const counts = countsFor(group.rows, nowMs);
    return { ...group, overdue: counts.overdue, dueToday: counts.dueToday };
  });
}

// --------------------------------------------------------------------------
// Approvals
// --------------------------------------------------------------------------

/** The older approvals inbox (`/approvals/inbox`), by document family. */
const APPROVAL_TOPIC: Record<string, Topic> = {
  writeoff: "stock",
  vflip: "stock",
  adjustment: "stock",
  damage: "stock",
  gap_closure: "transfers",
  transfer: "transfers",
  stock_request: "transfers",
  return_to_brand: "returns",
  booking: "receiving",
  pt_reverse: "receiving",
  offer: "offers",
  access_change: "access",
  debit_note: "receiving",
  petty_cash: "cash",
  open_to_buy: "receiving",
};

export function approvalTopic(kind: string): Topic | null {
  return APPROVAL_TOPIC[kind] ?? null;
}

interface GoodsApprovalGuide {
  topic: Topic;
  /** What is being approved, as a noun. */
  label: string;
  /** The screen that shows the exact revision and takes the decision. */
  screen: string;
}

/** Where a PT approval is decided: PT Work's To approve tab (OPS-17). */
const PT_APPROVALS = ptWorkPath({ tab: "approve" });

/** The goods approvals inbox (`/goods-v1/approvals/inbox`), by the action the
 *  request asks for. These are decided on their own screens, never inline:
 *  the checker reviews the exact submitted revision there, with step-up. */
const GOODS_APPROVAL_GUIDE: Record<string, GoodsApprovalGuide> = {
  // PT Work's To approve tab (OPS-17): the PT approvals screen is that tab now.
  "pt.approve.receipt": { topic: "receiving", label: "Receipt PT", screen: PT_APPROVALS },
  "pt.reversal.approve": { topic: "receiving", label: "PT reversal", screen: PT_APPROVALS },
  "pt.approve.opening": { topic: "opening", label: "Opening PT", screen: PT_APPROVALS },
  "opening.manifest.approve": {
    topic: "opening",
    label: "Opening stock list",
    screen: "/goods/opening",
  },
  "opening.variance.approve": {
    topic: "opening",
    label: "Opening stock difference",
    screen: "/goods/opening",
  },
  // Decided on the delivery's own GRN and Discrepancies steps (OPS-17). The
  // request names no GRN (a disposition's subject is its draft; a counter-GRN's
  // is its own document), so the way in is the inbox, where that delivery
  // stands at its step.
  "receipt.disposition.decide": {
    topic: "receiving",
    label: "Decision on received goods",
    screen: "/goods/receive",
  },
  "receipt.counter_grn.approve": {
    topic: "receiving",
    label: "Counter-GRN",
    screen: "/goods/receive",
  },
  "movement.approve": { topic: "stock", label: "Stock release", screen: "/goods/movements" },
  "config.approve": { topic: "setup", label: "Setting change", screen: "/setup/configuration" },
  "product.master.manage": {
    topic: "setup",
    label: "Product list change",
    screen: "/setup/products-parties",
  },
};

/** The actions a goods approval can ask for. Holding one of them is what makes
 *  the goods inbox worth asking for at all, so nobody else sends the request. */
export const GOODS_APPROVAL_ACTIONS: readonly string[] = Object.keys(GOODS_APPROVAL_GUIDE);

export function holdsGoodsApprovals(session: { display_actions: string[] } | null): boolean {
  return GOODS_APPROVAL_ACTIONS.some((action) => session?.display_actions?.includes(action));
}

export interface GoodsApprovalView {
  topic: Topic;
  label: string;
  /** Where to review and decide it. */
  to: string;
  /** The document's own number, where the request names one. */
  number: string | null;
}

/** A new item's style or SKU, proposed while preparing a PT (`master_proposal`). */
function isNewItemProposal(a: Pick<ApprovalDTO, "subject_kind" | "subject_id">): boolean {
  return (
    a.subject_kind === "master_proposal" &&
    (a.subject_id.startsWith("sku:") || a.subject_id.startsWith("style:"))
  );
}

export function goodsApprovalView(
  a: Pick<ApprovalDTO, "requested_action" | "subject_kind" | "subject_id" | "parent_document">,
): GoodsApprovalView {
  const guide = GOODS_APPROVAL_GUIDE[a.requested_action] ?? {
    topic: "stock" as const,
    label: "Approval",
    screen: "/action-needed?show=approvals",
  };
  // The movements screen opens one movement by id; its release request names
  // that movement document as its subject. A new item a PT proposed (its style
  // or its SKU) is confirmed or rejected under PT Work -> Mapping rules, the
  // one place that decides it (OPS-17A).
  const to =
    a.requested_action === "movement.approve" && a.subject_kind === "document"
      ? `${guide.screen}?movement=${encodeURIComponent(a.subject_id)}`
      : isNewItemProposal(a)
        ? ptWorkPath({ tab: "rules" })
        : guide.screen;
  const label = isNewItemProposal(a) ? "New item to confirm" : guide.label;
  return { topic: guide.topic, label, to, number: a.parent_document?.number ?? null };
}

// --------------------------------------------------------------------------
// Deadlines and notifications
// --------------------------------------------------------------------------

/** The kinds the daily check raises (`alerts.models.AlertKind`). */
const DEADLINE_TOPIC: Record<string, Topic> = {
  in_transit_aging: "transfers",
  return_window: "returns",
  stock_ageing: "stock",
  broken_size: "stock",
  count_due: "stock",
  cash_variance: "cash",
  brand_claim_flagged: "cash",
  sor_ageing: "stock",
  no_bill_return_cap: "returns",
  checklist_missed: "stock",
};

export function deadlineTopic(kind: string): Topic {
  return DEADLINE_TOPIC[kind] ?? "stock";
}

const EXCEPTION_EVENT = "exception.";

/** The exception kind a notification is about, or null for a notice that is
 *  not about an exception (`pt.rejected`). */
export function notificationKind(row: Pick<NotificationRow, "event_kind">): string | null {
  return row.event_kind.startsWith(EXCEPTION_EVENT)
    ? row.event_kind.slice(EXCEPTION_EVENT.length)
    : null;
}

/** The reason code the backend appends to an exception notification's title
 *  (`"<kind>: <REASON_CODE>"`), when it did. */
export function notificationReason(row: Pick<NotificationRow, "title">): string | null {
  return /:\s*([A-Z][A-Z0-9_+]*)$/.exec(row.title)?.[1] ?? null;
}

export function notificationTopic(row: Pick<NotificationRow, "event_kind" | "title">): Topic {
  const kind = notificationKind(row);
  if (kind) return kindGuide(kind, notificationReason(row)).topic;
  if (row.event_kind.startsWith("pt.")) return "receiving";
  return "system";
}

/** A notification as one sentence. An exception opening reads as news ("New
 *  problem: …"); any other notice keeps the words the server wrote. */
export function notificationSentence(row: Pick<NotificationRow, "event_kind" | "title">): string {
  const kind = notificationKind(row);
  if (!kind) return row.title;
  return `New problem: ${kindGuide(kind, notificationReason(row)).title}`;
}

/** Where a notification's Open goes: the exceptions of that kind on Action
 *  Needed, or the PT approvals screen for a PT sent back to its maker. */
export function notificationPath(row: Pick<NotificationRow, "event_kind">): string | null {
  const kind = notificationKind(row);
  if (kind) return `/action-needed?show=exceptions&kind=${encodeURIComponent(kind)}`;
  if (row.event_kind.startsWith("pt.")) return PT_APPROVALS;
  return null;
}

/** How a bundle of same-kind notifications reads: "65 new problems: Access
 *  change to review". */
export function notificationBundleSentence(
  sample: Pick<NotificationRow, "event_kind" | "title">,
  count: number,
): string {
  const kind = notificationKind(sample);
  if (!kind) return `${count} × ${sample.title}`;
  return `${count} new problems: ${kindGuide(kind, notificationReason(sample)).title}`;
}

/** Three or more of one story on one day read as a single line. */
export const BUNDLE_AT = 3;

export type NotificationItem =
  | { type: "one"; row: NotificationRow }
  | { type: "bundle"; key: string; rows: NotificationRow[] };

export interface NotificationDay {
  /** `YYYY-MM-DD` on the reader's own calendar. */
  day: string;
  /** "Today", "Yesterday", or the date. */
  heading: string;
  items: NotificationItem[];
}

function bundleKey(row: NotificationRow): string {
  const kind = notificationKind(row);
  return kind ? guideKey({ kind, reason_code: notificationReason(row) }) : row.event_kind;
}

/** Notifications under the day they arrived, newest first, with a run of the
 *  same story on one day folded into one line at the place of its newest row.
 *  The day is the reader's local calendar day, as `groupResolvedByDay` does it. */
export function notificationDays(
  rows: NotificationRow[],
  now: Date = new Date(),
): NotificationDay[] {
  const newestFirst = [...rows].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
  const byDay = new Map<string, NotificationRow[]>();
  for (const row of newestFirst) {
    const day = new Date(row.created_at).toLocaleDateString("en-CA");
    const bucket = byDay.get(day);
    if (bucket) bucket.push(row);
    else byDay.set(day, [row]);
  }
  return [...byDay.entries()].map(([day, dayRows]) => {
    const sizes = new Map<string, number>();
    for (const row of dayRows) sizes.set(bundleKey(row), (sizes.get(bundleKey(row)) ?? 0) + 1);
    const placed = new Set<string>();
    const items: NotificationItem[] = [];
    for (const row of dayRows) {
      const key = bundleKey(row);
      if ((sizes.get(key) ?? 0) < BUNDLE_AT) {
        items.push({ type: "one", row });
        continue;
      }
      if (placed.has(key)) continue;
      placed.add(key);
      items.push({ type: "bundle", key, rows: dayRows.filter((r) => bundleKey(r) === key) });
    }
    return { day, heading: dayHeading(day, now), items };
  });
}
