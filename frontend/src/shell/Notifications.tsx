// Alerts and Action Needed: two buttons, each with its own count and its own
// popup (#226, reshaped 1 Aug 2026). Action Needed was Approvals until 23 Sep
// 2026; it now carries both blocks of the Action Needed page - approvals to
// decide and exceptions to fix - with one count across both.
//
// They began as one bell with one combined count, which could not say which of
// the two feeds wanted you - and opening it to read approvals put the alerts
// feed on screen too, entangling "I read my approvals" with "I read my
// alerts". Split, each button owns its feed, its count and its popup, and
// opening one cannot touch the other's state: `ActionNeededButton` simply has no
// read-stamp to call.
//
// Since 23 Sep 2026 Action Needed also counts the goods approvals (receipt and
// opening PTs, opening stock, GRN decisions, stock release, settings, product
// list), and the bell also counts unread goods notifications, so each badge
// agrees with the page it opens (Anand: "yes" to both).
//
// Two things the popups deliberately do *not* do:
//
// * **They do not decide.** A row links into `/approvals`, where the decision
//   sits beside its step trail. A popup that approved things would be a second
//   place maker-checker happens, and the trail would not be on screen.
// * **They do not replace the dashboard cards.** The buttons are a second path
//   to the same two places (grill s3), so Home is untouched.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  History,
  Inbox,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { api } from "../lib/api";
import type { Page } from "../lib/goodsScreen";
import {
  ageWords,
  canSeeExceptions as holdsExceptions,
  docRef,
  groupExceptionsByKind,
  kindLabel,
  sortExceptions,
  type ExceptionRow,
  type NotificationRow,
} from "../lib/goodsExceptions";
import {
  goodsApprovalView,
  holdsGoodsApprovals,
  notificationKind,
  notificationSentence,
} from "../lib/actionCatalogue";
import type { ApprovalDTO } from "../lib/goodsPt";
import { Money } from "../lib/format";
import { fmtApprovalWhenShort, type ApprovalT } from "../components/approval";
import {
  alertWhere,
  daysLeftLabel,
  daysLeftTone,
  fmtAlertWhen,
  type AlertT,
} from "../components/alert";
import {
  DEFAULT_RANGE,
  actionNeededCount,
  RANGE_KEYS,
  RANGE_LABELS,
  badgeLabel,
  dayHeading,
  groupResolvedByDay,
  sinceFor,
  unreadAlerts,
  unseenCount,
  type RangeKey,
} from "./bellModel";
import { useMobileNavExclusion } from "./MobileNavContext";

/** Anyone who decides an approval says so here, so the bell can recount.
 *
 *  A DOM event rather than a store: the bell and the inbox are the only two
 *  things that care, they are never mounted together outside the shell, and one
 *  line beats a context for a fact this small. */
export const APPROVALS_CHANGED = "kdps:approvals-changed";

export function announceApprovalsChanged() {
  window.dispatchEvent(new Event(APPROVALS_CHANGED));
}

/** What a fetch said. `null` on either feed means the request failed and the
 *  popup shows a quiet line rather than an empty list, which would read as "all
 *  clear" - the most dangerous thing an alerts panel can say wrongly. */
type Feed<T> = T[] | null;

/** A feed before its first answer. Its own value, so a popup opened while the
 *  request is in flight says "Loading…" rather than "nothing waiting". */
type Loadable<T> = Feed<T> | "loading";

const LOADING = (
  <p className="bell-quiet" data-testid="bell-loading">
    Loading…
  </p>
);

function loaded<T>(feed: Loadable<T> | undefined): Feed<T> {
  return Array.isArray(feed) ? feed : null;
}

// ---------------------------------------------------------------------------
// History, shared by both feeds
// ---------------------------------------------------------------------------

function RangePicker({ value, onChange }: { value: RangeKey; onChange: (r: RangeKey) => void }) {
  return (
    <div className="bell-ranges" role="group" aria-label="History range">
      {RANGE_KEYS.map((key) => (
        <button
          key={key}
          type="button"
          className={`bell-range ${value === key ? "active" : ""}`}
          onClick={() => onChange(key)}
          aria-pressed={value === key}
          data-testid={`bell-range-${key}`}
        >
          {RANGE_LABELS[key]}
        </button>
      ))}
    </div>
  );
}

/**
 * The expandable History block: a disclosure button, a range, and a body.
 *
 * Fetch-on-expand, never on open: the popup has to appear instantly, and most
 * openings are somebody glancing at the live list and closing it again. The
 * fetcher is re-run whenever the range changes, and its answer replaces the
 * previous one rather than merging with it.
 */
function HistorySection<T>({
  testId,
  label = "History",
  fetcher,
  children,
}: {
  testId: string;
  label?: string;
  fetcher: (since: string) => Promise<T[]>;
  children: (rows: T[]) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [range, setRange] = useState<RangeKey>(DEFAULT_RANGE);
  const [rows, setRows] = useState<Feed<T>>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let live = true;
    setLoading(true);
    fetcher(sinceFor(range))
      .then((r) => live && setRows(r))
      .catch(() => live && setRows(null))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [open, range, fetcher]);

  return (
    <div className="bell-history">
      <button
        type="button"
        className="bell-history-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        data-testid={`${testId}-toggle`}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <History size={14} /> {label}
      </button>
      {open && (
        <div data-testid={testId}>
          <RangePicker value={range} onChange={setRange} />
          {loading ? (
            <p className="bell-quiet">Loading…</p>
          ) : rows === null ? (
            <p className="bell-quiet" data-testid={`${testId}-error`}>
              Could not load history just now.
            </p>
          ) : rows.length === 0 ? (
            <p className="bell-quiet">Nothing in this period.</p>
          ) : (
            children(rows)
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The feeds
// ---------------------------------------------------------------------------

/** "Receipt PT" → "receipt PT", but "PT reversal" stays: an acronym keeps its capitals. */
function lowerFirst(words: string): string {
  if (words.length > 1 && words[1] === words[1].toUpperCase() && /[A-Z]/.test(words[1])) {
    return words;
  }
  return words.charAt(0).toLowerCase() + words.slice(1);
}

/** Unread goods notifications, one line per story (so sixty-five of one kind
 *  are one line), pointing at the Alerts page where they are read. */
function NotificationLines({
  notes,
  onNavigate,
}: {
  notes: Loadable<NotificationRow> | undefined;
  onNavigate: () => void;
}) {
  if (notes === undefined) return null;
  if (notes === "loading") return LOADING;
  if (notes === null) {
    return (
      <p className="bell-quiet" data-testid="bell-notifications-error">
        Could not load notifications just now.
      </p>
    );
  }
  const unseen = notes.filter((n) => !n.seen);
  if (unseen.length === 0) {
    return <AllClear testId="bell-notifications-empty">No unread notifications.</AllClear>;
  }
  const lines = new Map<string, { sample: NotificationRow; count: number }>();
  for (const note of unseen) {
    const key = notificationKind(note) ?? note.event_kind;
    const line = lines.get(key);
    if (line) line.count += 1;
    else lines.set(key, { sample: note, count: 1 });
  }
  return (
    <ul className="bell-list" data-testid="bell-notifications-list">
      {[...lines.entries()].slice(0, GROUPS_SHOWN).map(([key, { sample, count }]) => (
        <li key={key} data-testid={`bell-notification-${key}`}>
          <Link to="/alerts" className="bell-item" onClick={onNavigate}>
            <span className="bell-item-main">
              <span className="bell-item-what">
                {notificationSentence(sample)}
                {count > 1 && <span className="bell-item-times">× {count}</span>}
              </span>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function AlertsFeed({
  alerts,
  notes,
  onNavigate,
}: {
  alerts: Feed<AlertT>;
  /** `undefined` when this person may not see goods notifications. */
  notes: Loadable<NotificationRow> | undefined;
  onNavigate: () => void;
}) {
  const fetchHistory = useCallback(
    (since: string) =>
      api.get("/alerts/history", { params: { since } }).then((r) => r.data as AlertT[]),
    [],
  );

  return (
    <>
      {alerts === null ? (
        <p className="bell-quiet" data-testid="bell-alerts-error">
          Could not load alerts just now. Open the{" "}
          <Link to="/alerts" onClick={onNavigate}>
            alerts screen
          </Link>
          .
        </p>
      ) : alerts.length === 0 ? (
        <p className="bell-quiet" data-testid="bell-alerts-empty">
          Nothing is raising its hand right now.
        </p>
      ) : (
        <ul className="bell-list" data-testid="bell-alerts-list">
          {alerts.map((a) => (
            <li key={a.id} className="bell-row" data-testid={`bell-alert-${a.id}`}>
              <p className="bell-row-head">
                <AlertTriangle size={12} /> {a.kind_label}
                <span className={`chip chip-${daysLeftTone(a.days_left)}`}>
                  {daysLeftLabel(a.days_left)}
                </span>
              </p>
              {/* Into the full screen, not the document behind the alert: the
                  popup is a reader and a launcher (grill s2), and `/alerts` is
                  where the row sits beside its deadline and its own link on. */}
              <p className="bell-row-title">
                <Link to="/alerts" className="link-cell" onClick={onNavigate}>
                  {a.title}
                </Link>
              </p>
              <p className="bell-row-meta">
                {alertWhere(a)} · {fmtAlertWhen(a.created_at)}
              </p>
            </li>
          ))}
        </ul>
      )}
      {notes !== undefined && (
        <>
          <p className="bell-day-head">Notifications</p>
          <NotificationLines notes={notes} onNavigate={onNavigate} />
        </>
      )}
      <Link to="/alerts" className="bell-all" onClick={onNavigate} data-testid="bell-alerts-all">
        Open the alerts screen
      </Link>
      <HistorySection testId="bell-alerts-history" fetcher={fetchHistory}>
        {(rows: AlertT[]) => (
          <ul className="bell-list">
            {groupResolvedByDay(rows).map((group) => (
              <li key={group.day} className="bell-day">
                <p className="bell-day-head">{dayHeading(group.day)}</p>
                {group.alerts.map((a) => (
                  <div key={a.id} className="bell-row" data-testid={`bell-alert-past-${a.id}`}>
                    <p className="bell-row-title">{a.title}</p>
                    <p className="bell-row-meta">
                      {a.kind_label} · {alertWhere(a)}
                    </p>
                  </div>
                ))}
              </li>
            ))}
          </ul>
        )}
      </HistorySection>
    </>
  );
}

/** How many kinds of exception the popup lists before pointing at the page.
 *  The popup is a glance; the full table, its filters and its drawer are on
 *  Action Needed. */
const GROUPS_SHOWN = 5;
/** Likewise for approvals, which are one row each. */
const APPROVALS_SHOWN = 5;

const ACTION_NEEDED = "/action-needed";

/** One of the popup's two blocks: a card with a coloured edge, its name, a line
 *  saying what the block is for, its count, and its own way to the page. The
 *  two blocks look alike on purpose and differ only in colour and icon, so the
 *  eye learns "blue = decide, orange = fix" once. */
function BellCard({
  tone,
  icon: Icon,
  title,
  hint,
  count,
  seeAll,
  onNavigate,
  testId,
  children,
}: {
  tone: "approvals" | "exceptions";
  icon: LucideIcon;
  title: string;
  hint: string;
  count: number | null;
  seeAll: string;
  onNavigate: () => void;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section className={`bell-card bell-card-${tone}`} data-testid={testId}>
      <header className="bell-card-head">
        <span className="bell-card-icon" aria-hidden="true">
          <Icon size={15} />
        </span>
        <span className="bell-card-name">
          <h3>{title}</h3>
          <span className="bell-card-hint">{hint}</span>
        </span>
        {!!count && <span className="bell-card-count">{count}</span>}
      </header>
      {children}
      {!!count && (
        <Link to={seeAll} className="bell-card-all" onClick={onNavigate} data-testid={`${testId}-all`}>
          See all {title.toLowerCase()} <ChevronRight size={13} />
        </Link>
      )}
    </section>
  );
}

function AllClear({ testId, children }: { testId: string; children: React.ReactNode }) {
  return (
    <p className="bell-clear" data-testid={testId}>
      <CheckCircle2 size={14} /> {children}
    </p>
  );
}

function ApprovalRows({
  approvals,
  goods,
  onNavigate,
}: {
  /** `undefined` when this person may not see the older approvals inbox. */
  approvals: Loadable<ApprovalT> | undefined;
  /** `undefined` when this person decides no goods approval. */
  goods: Loadable<ApprovalDTO> | undefined;
  onNavigate: () => void;
}) {
  if (approvals === "loading" || goods === "loading") return LOADING;
  const legacy = approvals ?? [];
  const goodsRows = goods ?? [];
  if (approvals === null && (goods === null || goods === undefined)) {
    return (
      <p className="bell-quiet" data-testid="bell-approvals-error">
        Could not load approvals just now.
      </p>
    );
  }
  if (legacy.length === 0 && goodsRows.length === 0) {
    return <AllClear testId="bell-approvals-empty">Nothing waiting for your approval.</AllClear>;
  }
  const legacyShown = legacy.slice(0, APPROVALS_SHOWN);
  const goodsShown = goodsRows.slice(0, APPROVALS_SHOWN - legacyShown.length);
  return (
    <ul className="bell-list" data-testid="bell-approvals-list">
      {goodsShown.map((a) => {
        const view = goodsApprovalView(a);
        return (
          <li key={`g-${a.id}`} data-testid={`bell-goods-approval-${a.id}`}>
            <Link to={`${ACTION_NEEDED}?show=approvals`} className="bell-item" onClick={onNavigate}>
              <span className="bell-item-main">
                <span className="bell-item-what">Review {lowerFirst(view.label)}</span>
                <span className="bell-item-why">{view.number ?? a.title}</span>
                <span className="bell-item-meta">
                  Asked by {a.maker.name || "—"}
                  {a.requested_at ? ` · ${fmtApprovalWhenShort(a.requested_at)}` : ""}
                </span>
              </span>
            </Link>
          </li>
        );
      })}
      {legacyShown.map((a) => (
        <li key={a.id} data-testid={`bell-approval-${a.id}`}>
          {/* Into the inbox, never the document and never a decision here:
              the step trail and the reason box live on Action Needed, and a
              decision taken without them is the thing maker-checker exists
              to prevent (grill s2, design assumption 5). */}
          <Link to={`${ACTION_NEEDED}?show=approvals`} className="bell-item" onClick={onNavigate}>
            <span className="bell-item-main">
              <span className="bell-item-what">Approve {a.kind_label.toLowerCase()}</span>
              <span className="bell-item-why">
                {a.title}
                {a.store_name ? ` · ${a.store_name}` : ""}
              </span>
              <span className="bell-item-meta">
                Asked by {a.requested_by_name} · {fmtApprovalWhenShort(a.requested_at)}
              </span>
            </span>
            {!!a.value_paise && (
              <span className="bell-value">
                <Money paise={a.value_paise} />
              </span>
            )}
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** Open exceptions, one line per kind: what to do, why, and how urgent. Like
 *  approvals, a line only points at the fix - a group opens the page filtered
 *  to that kind, a group of one opens that exception's drawer. */
function ExceptionRows({
  exceptions,
  onNavigate,
}: {
  exceptions: Loadable<ExceptionRow>;
  onNavigate: () => void;
}) {
  if (exceptions === "loading") return LOADING;
  if (exceptions === null) {
    return (
      <p className="bell-quiet" data-testid="bell-exceptions-error">
        Could not load exceptions just now.
      </p>
    );
  }
  if (exceptions.length === 0) {
    return <AllClear testId="bell-exceptions-empty">No problems to fix.</AllClear>;
  }
  const groups = groupExceptionsByKind(exceptions);
  const shown = groups.slice(0, GROUPS_SHOWN);
  const more = groups.length - shown.length;
  return (
    <>
      <ul className="bell-list" data-testid="bell-exceptions-list">
        {shown.map((g) => {
          const one = g.count === 1;
          const to = one
            ? `${ACTION_NEEDED}?show=exceptions&open=${encodeURIComponent(g.first.id)}`
            : `${ACTION_NEEDED}?show=exceptions&kind=${encodeURIComponent(g.kind)}`;
          const doc = docRef(g.first.subject_id);
          const why = g.cause || kindLabel(g.kind);
          return (
            <li key={g.kind} data-testid={`bell-exception-group-${g.kind}`}>
              <Link to={to} className="bell-item" onClick={onNavigate}>
                <span className="bell-item-main">
                  <span className="bell-item-what">
                    {g.action}
                    {!one && <span className="bell-item-times">× {g.count}</span>}
                  </span>
                  <span className="bell-item-why">
                    {why}
                    {one && ` · ${doc.label ? `${doc.label} ` : ""}${doc.id}`}
                  </span>
                </span>
                <span
                  className={`bell-due${g.overdue ? " bell-due-late" : ""}`}
                  data-testid="bell-exception-due"
                >
                  {g.overdue ? `${g.overdue} overdue` : ageWords(g.first.due_at)}
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
      {more > 0 && (
        <p className="bell-quiet" data-testid="bell-exceptions-more">
          And {more} more kind{more === 1 ? "" : "s"}.
        </p>
      )}
    </>
  );
}

/** Everything waiting on you, in two cards: approvals to decide and
 *  exceptions to fix - the same two the Action Needed page holds, each shown
 *  only to a person who may see it there. */
function ActionNeededFeed({
  approvals,
  goods,
  exceptions,
  onNavigate,
}: {
  /** `undefined` means this person may not see the block at all. */
  approvals: Loadable<ApprovalT> | undefined;
  goods: Loadable<ApprovalDTO> | undefined;
  exceptions: Loadable<ExceptionRow> | undefined;
  onNavigate: () => void;
}) {
  const waitingApprovals =
    loaded(approvals) === null && loaded(goods) === null
      ? null
      : (loaded(approvals)?.length ?? 0) + (loaded(goods)?.length ?? 0);
  const fetchHistory = useCallback(
    (since: string) =>
      api
        .get("/approvals", { params: { decided: "1", since } })
        .then((r) => r.data as ApprovalT[]),
    [],
  );

  return (
    <>
      {(approvals !== undefined || goods !== undefined) && (
        <BellCard
          tone="approvals"
          icon={Inbox}
          title="Approvals"
          hint="Waiting for your yes or no"
          count={waitingApprovals}
          seeAll={`${ACTION_NEEDED}?show=approvals`}
          onNavigate={onNavigate}
          testId="bell-approvals"
        >
          <ApprovalRows approvals={approvals} goods={goods} onNavigate={onNavigate} />
        </BellCard>
      )}
      {exceptions !== undefined && (
        <BellCard
          tone="exceptions"
          icon={AlertTriangle}
          title="Exceptions"
          hint="Problems to fix"
          count={loaded(exceptions)?.length ?? null}
          seeAll={`${ACTION_NEEDED}?show=exceptions`}
          onNavigate={onNavigate}
          testId="bell-exceptions"
        >
          <ExceptionRows exceptions={exceptions} onNavigate={onNavigate} />
        </BellCard>
      )}
      <Link
        to={ACTION_NEEDED}
        className="bell-all"
        onClick={onNavigate}
        data-testid="bell-action-needed-all"
      >
        Open Action Needed
      </Link>
      {approvals !== undefined && (
        <HistorySection
          testId="bell-approvals-history"
          label="Past approvals"
          fetcher={fetchHistory}
        >
          {(rows: ApprovalT[]) => (
            <ul className="bell-list">
              {rows.map((a) => (
                <li key={a.id} className="bell-row" data-testid={`bell-approval-past-${a.id}`}>
                  <p className="bell-row-title">{a.title}</p>
                  <p className="bell-row-meta">
                    {a.status === "approved" ? "Approved" : "Rejected"} by {a.decided_by_name || "—"}
                    {a.decided_at ? ` · ${fmtApprovalWhenShort(a.decided_at)}` : ""}
                    {a.reason ? ` · ${a.reason}` : ""}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </HistorySection>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// The buttons
// ---------------------------------------------------------------------------

/** The shell both buttons share: an icon button wearing its count, and a popup
 *  with a title row over the feed. The popup closes on outside click and Esc,
 *  the two ways every other popup in this shell closes - and because each
 *  button owns its own instance, opening one cannot close (or stamp) the
 *  other. */
function NoticeButton({
  icon: Icon,
  title,
  label,
  count,
  testId,
  onOpen,
  children,
}: {
  icon: LucideIcon;
  title: string;
  /** The button's spoken line - title and tooltip - which says the count. */
  label: string;
  count: number;
  testId: string;
  /** Called when the popup opens, never when it closes. */
  onOpen?: () => void;
  children: (onNavigate: () => void) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  useMobileNavExclusion(open, setOpen);

  function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    onOpen?.();
  }

  useEffect(() => {
    if (!open) return;
    const off = new AbortController();
    const { signal } = off;
    document.addEventListener(
      "pointerdown",
      (e) => {
        if (!wrap.current?.contains(e.target as Node)) setOpen(false);
      },
      { signal },
    );
    document.addEventListener(
      "keydown",
      (e) => {
        if (e.key === "Escape") setOpen(false);
      },
      { signal },
    );
    return () => off.abort();
  }, [open]);

  const badge = badgeLabel(count);

  return (
    <div className="bell-wrap" ref={wrap}>
      <button
        type="button"
        className="icon-btn"
        onClick={toggle}
        aria-label={label}
        aria-expanded={open}
        title={label}
        data-testid={testId}
      >
        <Icon size={18} />
        {badge && (
          <span className="bell-count" data-testid={`${testId}-count`}>
            {badge}
          </span>
        )}
      </button>

      {open && (
        <div className="bell-popup" data-testid={`${testId}-popup`}>
          <div className="bell-title">
            {title}
            {badge && <span className="bell-title-count">{badge}</span>}
          </div>
          <div className="bell-body">{children(() => setOpen(false))}</div>
        </div>
      )}
    </div>
  );
}

export function AlertsButton() {
  const { session } = useAuth();
  const canSeeNotes = holdsExceptions(session);
  const [alerts, setAlerts] = useState<Feed<AlertT>>([]);
  const [notes, setNotes] = useState<Loadable<NotificationRow>>("loading");
  // One plain stamp, `null` meaning "no stamp" - which is what the contract
  // says an absent row means, and so also the honest answer while the read is
  // in flight or after it fails. It errs towards showing the count: a button
  // that silently says "nothing waiting" because a request failed is the one
  // wrong answer an alerting surface must not give.
  const [seenAt, setSeenAt] = useState<string | null>(null);
  const { pathname } = useLocation();

  const loadAlerts = useCallback(
    () =>
      api
        .get("/alerts")
        .then((r) => {
          const rows = (r.data ?? []) as AlertT[];
          setAlerts(rows);
          return rows;
        })
        .catch(() => {
          setAlerts(null);
          return null;
        }),
    [],
  );

  // Re-counted on every navigation, not once per session.
  useEffect(() => {
    void loadAlerts();
    api
      .get("/alerts/seen")
      .then((r) => setSeenAt(r.data?.seen_at ?? null))
      .catch(() => setSeenAt(null));
  }, [loadAlerts, pathname]);

  // Unread goods notifications: read one by one on the Alerts page (E187), so
  // opening this popup never marks them - it only stamps the deadlines.
  useEffect(() => {
    if (!canSeeNotes) return;
    api
      .get<Page<NotificationRow>>("/goods-v1/alerts?limit=100")
      .then((r) => setNotes(r.data?.items ?? []))
      .catch(() => setNotes(null));
  }, [canSeeNotes, pathname]);

  /**
   * Opening the popup is what "reading" means, so it stamps - but only over a
   * list that was actually just fetched.
   *
   * The feed otherwise refreshes on navigation, so a popup opened after an
   * hour on one screen would stamp "read as of now" across alerts raised in
   * that hour and never shown. `unreadAlerts` is strictly-after, so those
   * would never surface again: exactly the wrong direction for an alerting
   * surface. Hence refetch first, and do not stamp at all if the refetch
   * failed.
   *
   * The badge zeroes locally the moment the popup opens rather than waiting
   * for the round trip, and goes back to what it was if the stamp did not
   * land - clearing a count the server never recorded would be the same
   * silence.
   */
  const openAndStamp = useCallback(() => {
    const previous = seenAt;
    setSeenAt(new Date().toISOString());
    void loadAlerts().then((rows) => {
      if (rows === null) {
        setSeenAt(previous);
        return;
      }
      api
        .post("/alerts/seen")
        .then((r) => setSeenAt(r.data?.seen_at ?? new Date().toISOString()))
        .catch(() => setSeenAt(previous));
    });
  }, [loadAlerts, seenAt]);

  const unreadDeadlines = unreadAlerts(alerts ?? [], seenAt);
  const unreadNotes = canSeeNotes ? unseenCount(loaded(notes)) : 0;
  const unread = unreadDeadlines + unreadNotes;
  const label = unread
    ? [
        unreadDeadlines ? `${unreadDeadlines} unread deadline${unreadDeadlines === 1 ? "" : "s"}` : "",
        unreadNotes ? `${unreadNotes} unread notification${unreadNotes === 1 ? "" : "s"}` : "",
      ]
        .filter(Boolean)
        .join(", ")
    : "Alerts - nothing unread";

  return (
    <NoticeButton
      icon={Bell}
      title="Alerts"
      label={label}
      count={unread}
      testId="alerts-button"
      onOpen={openAndStamp}
    >
      {(onNavigate) => (
        <AlertsFeed
          alerts={alerts}
          notes={canSeeNotes ? notes : undefined}
          onNavigate={onNavigate}
        />
      )}
    </NoticeButton>
  );
}

export function ActionNeededButton() {
  const { user, session } = useAuth();
  // The same two gates the Action Needed page asks (pages/ActionNeeded.tsx).
  const canSeeApprovals =
    !!user?.is_superuser || (user?.capabilities?.home ?? "none") !== "none";
  const canSeeExc = holdsExceptions(session);
  // Only for someone who can open Action Needed at all: a row here that led to
  // a page they may not see would be a dead end.
  const canSeeGoods = (canSeeApprovals || canSeeExc) && holdsGoodsApprovals(session);

  const [approvals, setApprovals] = useState<Loadable<ApprovalT>>("loading");
  const [goods, setGoods] = useState<Loadable<ApprovalDTO>>("loading");
  const [exceptions, setExceptions] = useState<Loadable<ExceptionRow>>("loading");
  const { pathname } = useLocation();

  // Re-counted on every navigation *and* whenever a decision is made, not once
  // per session: clearing the last item used to leave the old bell insisting
  // one document was still waiting, right beside a page saying nothing was.
  const refresh = useCallback(() => {
    if (canSeeApprovals) {
      api
        .get("/approvals/inbox")
        .then((r) => setApprovals(r.data ?? []))
        .catch(() => setApprovals(null));
    }
    if (canSeeGoods) {
      api
        .get<Page<ApprovalDTO>>("/goods-v1/approvals/inbox?limit=100")
        .then((r) => setGoods(r.data?.items ?? []))
        .catch(() => setGoods(null));
    }
    if (canSeeExc) {
      // Open only, as the page's table shows (the endpoint also returns
      // resolved rows). No live stream here: the top bar is on every screen,
      // and navigation already recounts; the page keeps its live dot.
      api
        .get<Page<ExceptionRow>>("/goods-v1/exceptions?limit=100")
        .then((r) =>
          setExceptions(sortExceptions((r.data?.items ?? []).filter((x) => x.state === "open"))),
        )
        .catch(() => setExceptions(null));
    }
  }, [canSeeApprovals, canSeeExc, canSeeGoods]);

  useEffect(() => {
    refresh();
    window.addEventListener(APPROVALS_CHANGED, refresh);
    return () => window.removeEventListener(APPROVALS_CHANGED, refresh);
  }, [refresh, pathname]);

  if (!canSeeApprovals && !canSeeExc) return null;

  const shownApprovals = canSeeApprovals ? approvals : undefined;
  const shownGoods = canSeeGoods ? goods : undefined;
  const shownExceptions = canSeeExc ? exceptions : undefined;
  const waiting = actionNeededCount(
    loaded(shownApprovals),
    loaded(shownExceptions),
    loaded(shownGoods),
  );
  const label = waiting
    ? `${waiting} thing${waiting === 1 ? "" : "s"} need${waiting === 1 ? "s" : ""} you`
    : "Action Needed - nothing waiting";

  // No `onOpen` and no read-stamp anywhere in this component, structurally:
  // reading what waits on you says nothing about whether you have read your
  // alerts, and the one endpoint that stamps is called only by AlertsButton.
  return (
    <NoticeButton
      icon={Inbox}
      title="Action Needed"
      label={label}
      count={waiting}
      testId="action-needed-button"
    >
      {(onNavigate) => (
        <ActionNeededFeed
          approvals={shownApprovals}
          goods={shownGoods}
          exceptions={shownExceptions}
          onNavigate={onNavigate}
        />
      )}
    </NoticeButton>
  );
}
