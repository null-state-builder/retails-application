// Exceptions and notification centre (ticket 08): shared shapes and the pure
// helpers the screen (`pages/GoodsExceptions.tsx`) renders from — age words,
// the sort order, the site/kind/owner/mine filters, and the resolution link
// each exception kind maps to.
//
// An exception resolves only through the command that fixes its cause
// (design E185 step 5, E186 step 9/10): there is no generic close here, only
// `resolutionFor`, which *names* that command and links to the screen that
// runs it.

import { privilegedChangePath } from "./administrativeHistory";
import { deliveryStepPath } from "./goodsReceiving";
import { hold } from "./goodsScreen";

/** One owned exception, as E185/E186 answer it. */
export interface ExceptionRow {
  id: string;
  kind: string;
  subject_id: string;
  /** Null where the exception belongs to no one site — a tenant-wide export
   *  failure has no site to own it (GSA-T18). */
  site_id: string | null;
  owner_role: string | null;
  owner_human_id: string | null;
  opened_at: string;
  /** Null when this kind's SLA is measured in working days and the business has
   *  approved no working calendar (GSA-T08): no deadline, never an inferred
   *  one. Such a row is never overdue and sorts last. */
  due_at: string | null;
  age_seconds: number;
  overdue: boolean;
  state: string;
  reason_code: string | null;
  allowed_resolution_actions: string[];
  evidence_ids: string[];
  revision: number;
  /** GSA-T08: the approved `working_calendar` version `due_at` was fixed
   *  against, when a working-day SLA had one approved at open. Absent for a
   *  days/hours/minutes SLA, or a working-day one opened before any tenant
   *  approved a calendar. */
  calendar_version_id: string | null;
}

/** One event on an exception's append-only log (the new GET on E186's route). */
export interface ExceptionEventRow {
  id: string;
  event_kind: "opened" | "assigned" | "note" | "resolved" | "reopened" | string;
  actor_id: string | null;
  recorded_at: string;
  reason_code: string | null;
  payload: Record<string, unknown> | null;
}

/** One notification, as E179/E180 answer it. */
export interface NotificationRow {
  id: string;
  event_kind: string;
  subject_id: string;
  site_id: string | null;
  title: string;
  created_at: string;
  due_at: string | null;
  seen: boolean;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** What a screen shows where a deadline would go when there is none — a
 *  working-day SLA in a business that has approved no working calendar
 *  (GSA-T08). Says the deadline is absent, never guesses one. */
export const NO_DUE_DATE = "No due date";

/** "3 days overdue" / "due today" / "due tomorrow" / "due in 5 days" — the
 *  UX brief's own words. Rounds to whole days rather than tracking exact
 *  calendar-day boundaries in the reader's own time zone, which is close
 *  enough for a deadline measured in days. A null due date (GSA-T08: a
 *  working-day SLA with no approved calendar) says so plainly instead. */
export function ageWords(dueAtIso: string | null, nowMs: number = Date.now()): string {
  if (dueAtIso === null) return "no due date";
  const diffMs = new Date(dueAtIso).getTime() - nowMs;
  if (diffMs < 0) {
    const days = Math.max(1, Math.round(-diffMs / DAY_MS));
    return days === 1 ? "1 day overdue" : `${days} days overdue`;
  }
  const days = Math.round(diffMs / DAY_MS);
  if (days === 0) return "due today";
  if (days === 1) return "due tomorrow";
  return `due in ${days} days`;
}

/** How long since this exception was raised — a distinct fact from the due
 *  date (the ticket's own acceptance criterion lists "owner, due date, age
 *  and original cause" as four separate things). Reads `age_seconds` rather
 *  than re-deriving it from `opened_at` and the caller's clock, so it agrees
 *  exactly with what the server just computed it against. */
export function raisedAgo(ageSeconds: number): string {
  const days = Math.floor(ageSeconds / (24 * 60 * 60));
  if (days >= 1) return days === 1 ? "raised 1 day ago" : `raised ${days} days ago`;
  const hours = Math.floor(ageSeconds / (60 * 60));
  if (hours >= 1) return hours === 1 ? "raised 1 hour ago" : `raised ${hours} hours ago`;
  return "raised just now";
}

const KIND_LABEL: Record<string, string> = {
  grn_awaiting_pt: "GRN awaiting PT",
  approval_pending: "Approval pending",
  receipt_discrepancy: "Receipt discrepancy",
  unbooked_arrival: "Unbooked arrival",
  three_way_mismatch: "Booking, invoice and count differ",
  unresolved_identity: "Unresolved identity",
  transfer_stalled: "Transfer stalled",
  transfer_discrepancy: "Transfer discrepancy",
  acceptance_discrepancy: "Acceptance discrepancy",
  rtv_not_dispatched: "RTV not dispatched",
  rtv_receipt_pending: "RTV awaiting vendor receipt",
  rtv_acknowledgement_discrepancy: "RTV vendor acknowledgement short",
  eway_missing: "E-way bill missing",
  count_session_stale: "Count session stale",
  scheduled_count_missed: "Scheduled count missed",
  setup_item_missing: "Setup item missing",
  series_hole: "Numbering series hole",
  cash_variance: "Cash variance",
  petty_cash_no_bill: "Petty cash bill missing",
  post_restore_recovery: "Post-restore recovery",
  site_readiness_failed: "Site readiness failed",
  site_closure_residual: "Site closure residual",
  evidence_protection: "Evidence protection",
  acceptance_remaining: "Remaining to accept",
  label_print_failed: "Label print failed",
  export_failed: "Export failed",
  opening_variance: "Opening stock variance",
  opening_origin_unavailable: "Opening origin unavailable",
  stock_hold_active: "Stock on hold",
  privileged_change_review: "Privileged change to review",
  setup_configuration_invalid: "Store setup missing or invalid",
  master_resolution_required: "Source key not mapped yet",
  identity_ambiguity: "Scanned code means two products",
};

/** Turn a `snake_case` or `SCREAMING_SNAKE` code into a sentence: words, with
 *  only the first letter capitalised. The shared fallback behind every label
 *  below — a code this build has never seen still reads as words rather than
 *  as a lowercase fragment dropped mid-sentence. */
function sentence(code: string): string {
  const words = code.replace(/_/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A kind's plain-English name — a fixed label where one is known, else the
 *  kind as a sentence, so a later ticket's new kind (registered only in the
 *  backend's `EXCEPTION_DEFAULTS`) still reads as words rather than a blank
 *  cell. */
export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? sentence(kind);
}

const EVENT_LABEL: Record<string, string> = {
  opened: "Raised",
  assigned: "Assigned",
  note: "Note added",
  resolved: "Resolved",
  reopened: "Reopened",
};

export function eventLabel(kind: string): string {
  return EVENT_LABEL[kind] ?? kind;
}

/** Every role's plain-English name, copied from the user-group table in
 *  `docs/product/overall-prd.md` §4.2 — the authoritative list. An exception
 *  nobody has been assigned yet is owned by a *role*, and the raw code
 *  ("C-WHO") is an engineering fact nobody on a shop floor reads. */
const ROLE_LABEL: Record<string, string> = {
  "X-PLT": "Platform administrator",
  "X-SVC": "Support / service identity",
  "X-FRN": "Franchise partner",
  "C-OWN": "Company owner",
  "C-FIN": "Finance",
  "C-WHO": "Warehouse operator",
  "C-PMO": "Product master owner",
  "C-INV": "Inventory controller",
  "C-BUY": "Buyer / merchandiser",
  "C-HRA": "HR administrator",
  "C-EMP": "Employee",
  "C-PAY": "Payroll reviewer",
  "C-CAO": "Commercial agreement owner",
  "C-STO": "Site transition owner",
  "C-ANL": "Analytics user",
  "M-STR": "Store manager",
  "M-CSH": "Cashier / counter staff",
  "E-RPT": "EBO reporter",
};

/** A role code's plain-English name, or the code itself where a tenant has a
 *  role §4.2 does not list — a code is still more use than a blank cell. */
export function roleLabel(code: string): string {
  return ROLE_LABEL[code] ?? code;
}

/** Why the exception was raised. The codes are the backend's own
 *  (`alerts.goods_services.open_exception`'s `reason_code`); several callers
 *  pass a code computed at runtime (an export failure, a crosswalk key), so
 *  the fallback below matters as much as the map. */
const REASON_LABEL: Record<string, string> = {
  GRN_ISSUED: "GRN issued",
  AWAITING_ACCEPTANCE: "Awaiting acceptance",
  DAMAGED_AT_ACCEPTANCE: "Damaged at acceptance",
  COUNTER_GRN_ADDED: "Counter-GRN added",
  CHAIN_VERIFICATION_FAILED: "Chain verification failed",
  CROSSWALK_UNMAPPED: "Source key not mapped",
  IDENTITY_AMBIGUOUS: "Scanned code means two products",
  NO_BOOKING: "No booking",
  OLDER_ORIGIN_UNKNOWN: "Older origin unknown",
  PRIVILEGED_CHANGE_UNREVIEWED: "Privileged change not reviewed",
  RELEASE_APPROVAL: "Release awaiting approval",
  RESERVED_STOCK_HELD: "Reserved stock held",
  VALUE_DAMAGE_APPROVAL: "Damage value awaiting approval",
  VERIFICATION_MISMATCH: "Verification mismatch",
  WRONG_GOODS: "Wrong goods",
  EXCESS: "Excess received",
  SHORTAGE: "Short received",
  DAMAGED: "Damaged",
  TRANSIT_SHORTAGE: "Arrived short",
  PRINT_FAILED: "Print failed",
  COUNT_NOT_DONE: "Not counted by its day",
};

/** A reason code as words — the fixed label where one is known, else the code
 *  with underscores turned to spaces and only its first letter capitalised, so
 *  a code this build has never seen still reads as a sentence. */
export function causeLabel(reasonCode: string | null): string {
  if (!reasonCode) return "—";
  // A receipt discrepancy carries every reason it found, joined with "+"
  // (`inbound.goods_services`): "EXCESS+SHORTAGE" reads "Excess received, short received".
  return reasonCode
    .split("+")
    .filter(Boolean)
    .map((code, i) => {
      const words = REASON_LABEL[code] ?? sentence(code);
      return i === 0 ? words : words.charAt(0).toLowerCase() + words.slice(1);
    })
    .join(", ");
}

/** Acronyms that must not be sentence-cased into "Grn" / "Sku". */
const SUBJECT_ACRONYM: Record<string, string> = {
  grn: "GRN",
  sku: "SKU",
  sbu: "SBU",
  rtv: "RTV",
  pt: "PT",
  // A privileged change's subject is its audit row; people call it a change.
  audit: "Change",
  transfer_dispatch: "Transfer",
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** What an exception's `subject_id` should read as in a table cell.
 *
 *  A subject key is `"<thing>:<id>"`, and the id is usually a 36-character
 *  UUID — printed whole it wraps onto two lines and makes the Document column
 *  the widest one on the screen while telling the reader nothing. This gives
 *  the thing's name and the first block of the UUID, which is enough to tell
 *  two rows apart; the screen keeps the whole key on the cell's `title` so it
 *  is still there to read and copy. A non-UUID id (a document number) is never
 *  shortened — that one *is* the thing a person would quote. */
export function docRef(subjectId: string): { label: string; id: string } {
  const at = subjectId.indexOf(":");
  if (at < 0) return { label: "", id: subjectId };
  const rawKind = subjectId.slice(0, at);
  const id = subjectId.slice(at + 1);
  const spaced = rawKind.replace(/_/g, " ");
  const label = SUBJECT_ACRONYM[rawKind] ?? spaced.charAt(0).toUpperCase() + spaced.slice(1);
  return { label, id: UUID_RE.test(id) ? id.slice(0, 8) : id };
}

/** Overdue first, then soonest due date, then the undated — the UX brief's sort
 *  order, with GSA-T08's "no approved calendar, so no deadline" rows last. */
export function sortExceptions(rows: ExceptionRow[]): ExceptionRow[] {
  return [...rows].sort((a, b) => {
    if (a.overdue !== b.overdue) return a.overdue ? -1 : 1;
    if (a.due_at === null || b.due_at === null) {
      if (a.due_at === b.due_at) return 0;
      return a.due_at === null ? 1 : -1;
    }
    return new Date(a.due_at).getTime() - new Date(b.due_at).getTime();
  });
}

export interface ExceptionCounts {
  overdue: number;
  dueToday: number;
  open: number;
}

/** The three top counts — overdue, due today, open — over whatever scope the
 *  caller already narrowed `rows` to (the server's site scope, GSA-T08). */
export function countsFor(rows: ExceptionRow[], nowMs: number = Date.now()): ExceptionCounts {
  let overdue = 0;
  let dueToday = 0;
  let open = 0;
  for (const row of rows) {
    if (row.state !== "open") continue;
    open += 1;
    if (row.overdue) {
      overdue += 1;
      continue;
    }
    // No deadline is not "due today": an undated row counts only as open.
    if (row.due_at === null) continue;
    const days = Math.round((new Date(row.due_at).getTime() - nowMs) / DAY_MS);
    if (days <= 0) dueToday += 1;
  }
  return { overdue, dueToday, open };
}

export interface ExceptionFilters {
  kind: string;
  ownerHumanId: string;
  mineOnly: boolean;
}

/** Is this exception mine? Assigned to me by name, or nobody's by name yet and
 *  owned by a role I hold. Nothing assigns an exception by itself, so "assigned
 *  to me" alone left "Mine only" empty for everyone who had not self-assigned. */
export function isMine(
  row: Pick<ExceptionRow, "owner_human_id" | "owner_role">,
  myHumanId: string | null,
  myRoles: readonly string[] = [],
): boolean {
  if (row.owner_human_id) return Boolean(myHumanId) && row.owner_human_id === myHumanId;
  return Boolean(row.owner_role) && myRoles.includes(row.owner_role as string);
}

/** Kind/owner/"mine" — filtered client-side over the (already site-scoped by
 *  the server) fetched page. Site itself is a server query param
 *  (`ExceptionListView` filters by it), not filtered again here. */
export function applyFilters(
  rows: ExceptionRow[],
  filters: ExceptionFilters,
  myHumanId: string | null,
  myRoles: readonly string[] = [],
): ExceptionRow[] {
  return rows.filter((row) => {
    if (filters.kind && row.kind !== filters.kind) return false;
    if (filters.ownerHumanId && row.owner_human_id !== filters.ownerHumanId) return false;
    if (filters.mineOnly && !isMine(row, myHumanId, myRoles)) return false;
    return true;
  });
}

export interface Resolution {
  label: string;
  /** A URL to the screen that runs the resolving command, or `null` when no
   *  known screen maps to this exception's `allowed_resolution_actions` yet
   *  (a later ticket's kind, not built here) — `label` still names the real
   *  action so the drawer never shows a dead or invented link. */
  to: string | null;
}

/** One entry per resolving screen this ticket (or an earlier one) actually
 *  built. `match` is a substring of the server's own `allowed_resolution_actions`
 *  path (design's own API path, e.g. `inbound/grns/{id}/dispositions`) — the
 *  same string the resolving screen's endpoint already answers to, so this
 *  table can only ever point at a route that exists. See the registration
 *  rule in `tests/test_goods_exceptions_calendar.py` for how a later ticket
 *  adds its own entry here alongside its own exception kind. */
const RESOLUTION_ROUTES: {
  match: string;
  label: string;
  /** `null` when this subject has no screen for the matched action. */
  build: (subjectId: string) => string | null;
}[] = [
  {
    match: "dispositions",
    label: "Decide disposition",
    // A receipt's disputed goods are decided on its delivery's Discrepancies
    // step (OPS-17: there is no receipts screen of its own any more).
    build: (subjectId) => deliveryStepPath("grn", subjectDocumentId(subjectId), "discrepancies"),
  },
  {
    match: "ptmapper/files/from-grn",
    label: "Prepare the PT",
    build: (subjectId) => deliveryStepPath("grn", subjectDocumentId(subjectId), "pt_prepare"),
  },
  {
    match: "receipt-links",
    label: "Link the booking",
    // The subject is an *arrival*, not a booking, so its id opens no booking.
    // The Bookings list is where the person finds the one it answers.
    build: () => "/booking",
  },
  {
    match: "acceptance-sessions",
    label: "Continue acceptance",
    // The subject is the official PT. A receipt PT opens its delivery at the
    // Accept step; an opening PT, which belongs to no delivery, opens on the
    // same address with its acceptance panel alone (`ReceiveDelivery.tsx`).
    build: (subjectId) =>
      subjectId.startsWith("document:")
        ? deliveryStepPath("pt", subjectDocumentId(subjectId), "accept")
        : "/goods/receive",
  },
  {
    match: "opening-manifests",
    label: "Review the opening manifest",
    build: () => "/goods/opening",
  },
  {
    match: "confirm-origin-unavailable",
    label: "Review the opening manifest",
    build: () => "/goods/opening",
  },
  {
    // GSA-T03 (ticket 03D): a privileged change waiting for a different
    // person's review. Its subject is `audit:<change id>`; only that review
    // closes it.
    match: "privileged-changes",
    label: "Review the privileged change",
    build: (subjectId) => privilegedChangePath(subjectDocumentId(subjectId)),
  },
  {
    // A hold is lifted, and a submitted release is decided, on the movements
    // screen (ticket 12). Both exception kinds name the same command. An
    // adjustment's own work (goods ticket 15A: its approval, and found goods
    // nobody valued) opens that movement there.
    match: "outbound/movements",
    label: "Open on the movements screen",
    build: (subjectId) =>
      subjectId.startsWith("movement:")
        ? `/goods/movements?movement=${encodeURIComponent(subjectDocumentId(subjectId))}`
        : "/goods/movements",
  },
  {
    // A count pass left idle for a day (goods ticket 17) is resumed, or the
    // count cancelled, on the count's own screen. Its subject is the count.
    match: "outbound/count-sessions",
    label: "Open the count",
    build: (subjectId) =>
      subjectId.startsWith("stocktake:")
        ? `/goods/counts/${encodeURIComponent(subjectDocumentId(subjectId))}`
        : "/goods/counts",
  },
  {
    // A scheduled count not done by its day (store operations ticket 35) is
    // counted from the schedule's "Count now" link, or the Owner stops it there.
    match: "outbound/count-schedules",
    label: "Open the count schedule",
    build: () => "/stock-count/schedule",
  },
  {
    // A transfer that arrived short is settled on the goods transfers screen.
    match: "outbound/transfers",
    label: "Open the transfer",
    build: () => "/goods/transfers",
  },
  {
    // A failed label print job is printed again on the labels screen, which
    // owns the print jobs (`/goods-v1/ptmapper/print-jobs`).
    match: "ptmapper/print-jobs",
    label: "Print the labels again",
    build: () => "/goods/labels",
  },
  {
    // A damaged-goods value on a GRN waits for its second person on that
    // GRN's own receipt screen, where its approvals panel decides it.
    match: "approvals/{id}/decide",
    label: "Decide the approval",
    build: (subjectId) =>
      subjectId.startsWith("grn:")
        ? deliveryStepPath("grn", subjectDocumentId(subjectId), "discrepancies")
        : null,
  },
  // GSA-T04 (ticket 04B). Each of the three below leads back to the governed
  // command that fixes the cause, never to a place where the item could simply
  // be closed.
  {
    // A counted code that matched more than one product. The product master
    // owner's own lookup panel takes the scan id in its URL and records the
    // choice against that exact scan (E091).
    match: "masters/identity-picks",
    label: "Choose which product this is",
    build: (subjectId) =>
      `/setup/products-parties?type=lookup&scan_event_id=${encodeURIComponent(
        subjectDocumentId(subjectId),
      )}`,
  },
  {
    // Ticket 42: a petty cash spend saved with no bill photo. The subject reads
    // `petty_cash_spend:<id>`, and the spend's own page takes the photo.
    match: "sell/petty-cash/spends",
    label: "Add the bill photo",
    build: (subjectId) => `/money/petty-cash/${encodeURIComponent(subjectDocumentId(subjectId))}`,
  },
  {
    // A source key mapped to nothing yet, corrected on the crosswalks panel.
    match: "masters/crosswalks",
    label: "Map the source key",
    build: () => "/setup/products-parties?type=crosswalks",
  },
  // Required store setup that is missing or invalid. Each of these subjects
  // reads `setup:<site id>:<check key>`, and the site's own readiness tab is
  // where every one of them is put right and re-checked. One entry per
  // governed setup command rather than a `masters/` catch-all, so a later
  // kind naming a different masters route cannot land here by accident.
  ...(
    [
      ["masters/entities", "Put the site's legal entity right"],
      ["masters/gstins", "Put the site's registration right"],
      ["masters/stores", "Put the site's setup right"],
      ["masters/configurations", "Approve the configuration this site needs"],
      ["accounts/admin/staff", "Assign someone to this site"],
    ] as const
  ).map(([match, label]) => ({
    match,
    label,
    build: (subjectId: string) =>
      `/setup/organisation?panel=sites&site=${encodeURIComponent(
        subjectSiteId(subjectId),
      )}&tab=readiness`,
  })),
];

/** The site in a `setup:<site id>:<check key>` subject. Its own parser rather
 *  than `subjectDocumentId`, because this subject names two things: which site,
 *  and which of its setup items. Only the site opens a screen. */
function subjectSiteId(subjectId: string): string {
  return subjectDocumentId(subjectId).split(":")[0] ?? "";
}

function subjectDocumentId(subjectId: string): string {
  const colon = subjectId.indexOf(":");
  return colon === -1 ? subjectId : subjectId.slice(colon + 1);
}

/** A readable fallback for an action string this table has no route for yet:
 *  "inbound/grns/{id}/dispositions" reads as "inbound grns dispositions". */
function humanizeAction(action: string): string {
  return action
    .replace(/\{[^}]*\}/g, "")
    .split("/")
    .filter(Boolean)
    .join(" ");
}

/** The resolving screen for one exception, named by the server's own
 *  `allowed_resolution_actions` rather than guessed from `kind` — so a kind
 *  this table has no entry for yet still shows the real action, never a
 *  close button (design E185 step 5, the UX brief's own rule). */
export function resolutionFor(
  row: Pick<ExceptionRow, "allowed_resolution_actions" | "subject_id">,
): Resolution {
  const actions = row.allowed_resolution_actions ?? [];
  for (const route of RESOLUTION_ROUTES) {
    if (actions.some((action) => action.includes(route.match))) {
      const to = route.build(row.subject_id);
      if (to) return { label: route.label, to };
    }
  }
  const firstAction = actions[0];
  if (firstAction) return { label: humanizeAction(firstAction), to: null };
  return { label: "No resolving command is registered yet", to: null };
}

/** Open exceptions of one kind, as one line in the top-bar popup. */
export interface ExceptionGroup {
  kind: string;
  count: number;
  /** How many of them are past their due date. */
  overdue: number;
  /** The most urgent one - the group's due date, and where a group of one opens. */
  first: ExceptionRow;
  /** What to do, in the resolving screen's own words (`resolutionFor`), or
   *  the kind's name where no fixing screen is known yet. */
  action: string;
  /** Why it was raised, in words (`causeLabel`), or "" when there is no code. */
  cause: string;
}

/** One group per kind, so eighty of the same thing read as one line rather
 *  than a page of identical rows. Groups keep `sortExceptions`' order: any
 *  overdue first, then the soonest due. */
export function groupExceptionsByKind(rows: ExceptionRow[]): ExceptionGroup[] {
  const groups = new Map<string, ExceptionGroup>();
  for (const row of sortExceptions(rows)) {
    const g = groups.get(row.kind);
    if (g) {
      g.count += 1;
      if (row.overdue) g.overdue += 1;
      continue;
    }
    groups.set(row.kind, {
      kind: row.kind,
      count: 1,
      overdue: row.overdue ? 1 : 0,
      first: row,
      // A kind with no known fixing screen has no plain "what to do" yet, so
      // it is named for what it is rather than "no command is registered".
      action: resolutionFor(row).to ? resolutionFor(row).label : kindLabel(row.kind),
      cause: row.reason_code ? causeLabel(row.reason_code) : "",
    });
  }
  return [...groups.values()];
}

/** Does this session hold a grant that shows exceptions and goods
 *  notifications? The one gate Action Needed and its top-bar button ask. */
export function canSeeExceptions(session: { display_actions: string[] } | null): boolean {
  return hold(session, "exception.view") || hold(session, "exception.manage");
}
