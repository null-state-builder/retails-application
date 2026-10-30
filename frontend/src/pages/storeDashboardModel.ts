import type { Capability } from "../shell/navConfig";

// The store Dashboard's payload, and the pure part of drawing it (#174, D10 §2).
//
// Split out of the component so it can be tested: this suite renders nothing, so
// anything worth asserting has to be a function that takes the payload and
// returns what the screen will say. What lives here is the two things most
// likely to go quietly wrong - which screen each action-queue row opens, and how
// a sparkline of all-noughts is scaled.

/** `GET /api/store/dashboard`. `manager` is *absent*, not null, for a cashier. */
export interface DashboardPayload {
  store: string;
  /** False until the Sale document and the till land (#177/#181). Tells "no
   *  bills yet today" apart from "this store cannot bill at all". */
  sales_live: boolean;
  today: {
    net_sales_paise: number;
    bills: number;
    avg_bill_paise: number;
    pieces: number;
    collections: { cash: number; card: number; upi: number; credit_note: number };
    vs_yesterday_pct: number | null;
  };
  action_queue: { key: string; count: number }[];
  live: {
    offers: { id: number; brand: string; one_liner: string }[];
    in_transit: { id: number; doc_number: string; pieces: number; expected: string }[];
  };
  last7: { date: string; net_sales_paise: number }[];
  manager?: {
    day_close: { date: string; state: string };
    mtd_net_paise: number;
    target_paise: number;
  };
}

export interface QueueRow {
  key: string;
  count: number;
  /** What the number is, as a person would say it. */
  label: string;
  /** Where clearing it happens. */
  to: string;
}

/** A row whose screen answers to a section this person may not hold. The
 *  Dashboard itself is `home: view` - the rung everybody has - so most rows open
 *  onto work their own gate has already let this person see. `continuity_flags`
 *  is the exception: it opens the Money section, and a count that sends somebody
 *  to a screen the API refuses is worse than no count (the `canSeeMoney` rule
 *  Home already holds to). */
type Needs = { section: string; capability: Capability };

/** "This person holds everything" - for callers that are asking about the
 *  mapping rather than about anybody's access. Named rather than inlined so a
 *  grep for it finds every place the gate has been stood down. */
export const ALWAYS = (): boolean => true;

/** Key → the sentence and the screen. The server decides the order and which
 *  keys exist at all; this side only knows what each one means.
 *
 *  All the contract's keys are here as of #188 (`grn_pt_pending` left with
 *  legacy receiving in OPS-18). They arrived one ticket at a time and each one
 *  waited for the screen that clears it, which is the rule this map exists to
 *  keep: a count you cannot click is a count nobody can clear. */
const QUEUE_MEANING: Record<string, { label: string; to: string; needs?: Needs }> = {
  approvals_pending: { label: "Waiting for your approval", to: "/action-needed?show=approvals" },
  // Not `/transfer/in-transit`: that screen is the *sender's*, scoped to the
  // source store, so a receiving store clicked through from "1 carton to
  // receive" and was told nothing was on the road (found in browser QA). The
  // Transfers list carries both directions, and the Receive button lives on the
  // transfer itself - which the live-in-store card links to piece by piece.
  transfers_to_receive: { label: "Cartons to receive", to: "/transfer" },
  // The store's three folded sections are addressed through the Inventory fold
  // (#170), which is the arrangement a store person actually has.
  quarantine_to_confirm: { label: "Damage reports to confirm", to: "/inventory?tab=damage" },
  rtb_windows_closing: { label: "Return windows closing", to: "/alerts" },
  open_count_session: { label: "Stock counts open", to: "/inventory?tab=count" },
  // The hold list lives inside Billing rather than at an address of its own -
  // picking a parked bill up *is* billing - so the row opens the counter with
  // the list already showing (#185).
  held_bills: { label: "Bills on hold", to: "/sell?holds=1" },
  // Pieces sold before their paperwork (#186). The row goes to Receive Goods
  // rather than to a list of the bills, and that is the honest destination:
  // nobody clears this queue by looking at it - it clears itself the moment the
  // PT that prices the piece is posted, so the work is *over there*. A screen
  // listing the waiting lines belongs with the daily check (#188), which is what
  // a person reads when the paperwork is not coming at all.
  uncosted_sale_lines: { label: "Sold before inward", to: "/goods/receive" },
  // The counter's exceptions (#188) - a hole in the numbering, tax that is not
  // the dated slab's, a seller who took back six. They live under the day they
  // belong to, on the screen that also shows what the day took, because "the
  // drawer is short" and "three bills never arrived" are the same conversation.
  continuity_flags: {
    label: "Exceptions to look at",
    to: "/reports/day-summary",
    needs: { section: "money", capability: "view" },
  },
  // Store operations ticket 35 (ST-INV-3): the scheduled counts due today and not
  // yet done. Sent only where the scheduled-counts switch is on. The schedule
  // lists each one with a "Count now" link to the store's blind count; the
  // store reaches it through the Inventory fold.
  counts_due_today: {
    label: "Counts due today",
    to: "/inventory?tab=schedule",
    needs: { section: "stock_count", capability: "view" },
  },
  // Store operations ticket 49 (ST-OPS-4): checklist items whose time passed and
  // are still not ticked, today's and the last few days'. Sent only where the
  // store-checklists switch is on; the checklist is where they are ticked.
  checklist_missed: { label: "Checklist items missed", to: "/checklist" },
};

/** The queue as rows to draw. A key this build has no screen for is dropped
 *  rather than rendered as a dead number - a count you cannot click is a count
 *  nobody can clear - and so is one whose screen this person may not open.
 *
 *  `may` is required and has no permissive default on purpose: it is a
 *  security-adjacent predicate, and a caller that forgot it would silently draw
 *  a row somebody may not click. The screen passes `userCan`; a test that does
 *  not care passes `ALWAYS`. */
export function queueRows(
  payload: DashboardPayload,
  may: (section: string, capability: Capability) => boolean,
): QueueRow[] {
  return payload.action_queue.flatMap((row) => {
    const meaning = QUEUE_MEANING[row.key];
    if (!meaning) return [];
    if (meaning.needs && !may(meaning.needs.section, meaning.needs.capability)) return [];
    const { needs: _needs, ...rest } = meaning;
    return [{ key: row.key, count: row.count, ...rest }];
  });
}

/** Every key this side can draw - the frontend half of the contract, asserted
 *  in `storeDashboardModel.test.ts` against the six the server sends today. */
export const KNOWN_QUEUE_KEYS = Object.keys(QUEUE_MEANING);

export interface SparkBar {
  date: string;
  paise: number;
  /** Height as a percentage of the tallest day: 0 when nothing sold at all,
   *  and never negative - a day whose returns outran its sales is a bar with
   *  no height, not a bar drawn downwards through the axis. */
  heightPct: number;
}

/** Seven days scaled against their own best day.
 *
 *  A week of noughts scales to nought rather than to a flat full-height row:
 *  dividing by the maximum would make every bar 100% of nothing, which reads as
 *  a week of identical trade. The bars keep their dates either way, so the strip
 *  has an axis before it has any values. */
export function sparkBars(last7: DashboardPayload["last7"]): SparkBar[] {
  const peak = Math.max(0, ...last7.map((d) => d.net_sales_paise));
  return last7.map((d) => ({
    date: d.date,
    paise: d.net_sales_paise,
    heightPct: peak > 0 ? Math.max(0, Math.round((d.net_sales_paise / peak) * 100)) : 0,
  }));
}

/** How far through the month's number this store is, clamped to 0-100 for the
 *  bar's width - the figure beside the bar is never clamped, so a store past its
 *  target still reads what it actually did.
 *
 *  A month with no target set returns `null`: no target is not 0% of a target,
 *  and a bar drawn at zero against nothing would read as failure. */
export function targetProgressPct(mtdPaise: number, targetPaise: number): number | null {
  if (targetPaise <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((mtdPaise / targetPaise) * 100)));
}

/** "30 Jul" - the sparkline's tick. Built from the ISO parts rather than
 *  `new Date(iso)`, which parses a bare date as UTC and can shift it a day. */
export function shortDay(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (
    y === undefined ||
    m === undefined ||
    d === undefined ||
    !Number.isInteger(y) ||
    !Number.isInteger(m) ||
    !Number.isInteger(d)
  )
    return iso;
  return new Date(y, m - 1, d).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

/** "Today, Thu 29 Oct" - the Home heading (design system v2 Dashboard: open on
 *  the day, not on a greeting). Built from the parts so en-IN's "Thu, 29 Oct"
 *  comma does not land between the weekday and the date. */
export function todayHeading(now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
  }).formatToParts(now);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? "";
  return `Today, ${part("weekday")} ${part("day")} ${part("month")}`;
}

/** Each queue key as a clause, singular and plural, for the sentence under the
 *  heading. A key with no clause here still shows in the list; it is only left
 *  out of the sentence's two named items and counted in "more". */
const QUEUE_CLAUSE: Record<string, [string, string]> = {
  approvals_pending: ["item waits for your approval", "items wait for your approval"],
  transfers_to_receive: ["carton waits to be received", "cartons wait to be received"],
  quarantine_to_confirm: [
    "damage report waits to be confirmed",
    "damage reports wait to be confirmed",
  ],
  rtb_windows_closing: ["return window is closing", "return windows are closing"],
  open_count_session: ["stock count is open", "stock counts are open"],
  held_bills: ["bill is on hold", "bills are on hold"],
  uncosted_sale_lines: ["line was sold before inward", "lines were sold before inward"],
  continuity_flags: ["exception waits to be looked at", "exceptions wait to be looked at"],
  counts_due_today: ["count is due today", "counts are due today"],
  checklist_missed: ["checklist item was missed", "checklist items were missed"],
};

/** What is waiting, as one sentence: the first two non-zero rows in the
 *  server's order, then how many more. Built from the rows the person may open,
 *  so the sentence never names work the list below does not show. */
export function waitingSentence(rows: QueueRow[]): string {
  const waiting = rows.filter((r) => r.count > 0);
  if (waiting.length === 0) return "Nothing is waiting on you.";
  const named = waiting.filter((r) => QUEUE_CLAUSE[r.key]).slice(0, 2);
  const clauses = named.map((r) => {
    const [one, many] = QUEUE_CLAUSE[r.key] ?? ["", ""];
    return `${r.count.toLocaleString("en-IN")} ${r.count === 1 ? one : many}`;
  });
  const more = waiting.length - named.length;
  if (clauses.length === 0) return `${more} kinds of work wait on you below.`;
  const head = clauses.join(" and ");
  const sentence = `${head.charAt(0).toUpperCase()}${head.slice(1)}`;
  return more > 0 ? `${sentence}, with ${more} more below.` : `${sentence}.`;
}

export interface TenderRank {
  label: string;
  paise: number;
  /** Share of the day's collections, 0-100; null when nothing was collected. */
  sharePct: number | null;
  /** Bar length against the leader, 0-100. */
  barPct: number;
}

/** The day's collections ranked by tender, largest first. All four are kept -
 *  dropping one would hide money from the list - and ties keep the counter's
 *  usual order (cash, card, UPI, credit note). */
export function tenderRanks(c: DashboardPayload["today"]["collections"]): TenderRank[] {
  const rows: [string, number][] = [
    ["Cash", c.cash],
    ["Card", c.card],
    ["UPI", c.upi],
    ["Credit note", c.credit_note],
  ];
  const total = rows.reduce((sum, [, p]) => sum + Math.max(0, p), 0);
  const peak = Math.max(0, ...rows.map(([, p]) => p));
  return rows
    .map(([label, paise], i) => ({ label, paise, i }))
    .sort((a, b) => b.paise - a.paise || a.i - b.i)
    .map(({ label, paise }) => ({
      label,
      paise,
      sharePct: total > 0 ? Math.round((Math.max(0, paise) / total) * 100) : null,
      barPct: peak > 0 ? Math.max(0, Math.round((paise / peak) * 100)) : 0,
    }));
}

/** The week's best day and its total, for the line under the strip; null for a
 *  week with no sales. */
export function weekSummary(last7: DashboardPayload["last7"]): {
  totalPaise: number;
  best: { date: string; paise: number } | null;
} {
  const totalPaise = last7.reduce((sum, d) => sum + d.net_sales_paise, 0);
  const top = last7.reduce<{ date: string; paise: number } | null>(
    (acc, d) =>
      d.net_sales_paise > 0 && (!acc || d.net_sales_paise > acc.paise)
        ? { date: d.date, paise: d.net_sales_paise }
        : acc,
    null,
  );
  return { totalPaise, best: top };
}
