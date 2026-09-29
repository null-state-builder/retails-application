// Exceptions and notification centre (ticket 08): the one shared place every
// business exception is found with its owner, due date, age and original
// cause, can be assigned or annotated, and resolves only through the command
// that actually fixes it (design §8.2, change PRD §5.10, §6.3 invariant 11).
//
// Two pieces, each drawn on the Home screen it belongs to (Anand, 23 Sep 2026:
// split by "do I have to do something?"). `ExceptionsPanel` sits on Action
// Needed: counts, a topic/site/mine filter row, and one card per kind of
// problem - what happened, what to do, and its rows most urgent first - with a
// side panel holding the event log, an append-only assign/note pair and one
// "Resolve via …" link that names the real command. `GoodsNotificationsFeed`
// sits on Alerts: the recipient-scoped feed (E179/E180) grouped by day, with
// "Mark read" (E187) - reading a notification never resolves the exception it
// is about, so there is no close button there either. The words and topics for
// both come from `lib/actionCatalogue.ts`. The old `/goods/exceptions` address
// redirects.
//
// There is no generic close anywhere in either piece. See the registration
// rule in `tests/test_goods_exceptions_calendar.py` (backend) for how a later
// ticket wires its own exception kind into `lib/goodsExceptions.ts`'s
// `resolutionFor`.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowRight, CheckCheck, CheckCircle2, UserCheck, X } from "lucide-react";

import { api, apiErrorMessage, apiUrl, goodsMeta } from "../lib/api";
import { Denied, Feedback, listState, useGoodsFetch, type Page } from "../lib/goodsScreen";
import {
  ageWords,
  applyFilters,
  causeLabel,
  countsFor,
  docRef,
  eventLabel,
  kindLabel,
  NO_DUE_DATE,
  raisedAgo,
  resolutionFor,
  roleLabel,
  type ExceptionEventRow,
  type ExceptionRow,
  type NotificationRow,
} from "../lib/goodsExceptions";
import {
  exceptionTopic,
  groupForPage,
  kindGuide,
  notificationBundleSentence,
  notificationDays,
  notificationPath,
  notificationSentence,
  notificationTopic,
  type ExceptionCard,
  type Topic,
} from "../lib/actionCatalogue";
import {
  countTopics,
  parseTopic,
  TopicChip,
  TopicFilter,
  UrgencyBadge,
  type Urgency,
} from "../components/ActionBits";
import { useAuth, type GoodsSession } from "../auth/AuthContext";
import { formatDateTime } from "../lib/format";
import "./GoodsExceptions.css";

type LiveStatus = "connecting" | "live" | "reconnecting" | "stopped";

/** How close together two reconnects may reload the list. The server closes the
 *  response every ~20s by design, so a healthy stream reconnects on a clock and
 *  one reload per cycle is right; anything faster than this is a stream failing
 *  and retrying, not news. */
const RELOAD_EVERY_MS = 2_000;
/** Consecutive failures *without a successful reopen* before this stops trying.
 *  A stream that cannot stay up reconnects as fast as the browser allows, and
 *  each reopen reloaded the list - a render loop React eventually refuses
 *  ("Maximum update depth exceeded"), with a screen that never settles. Bounded,
 *  and said out loud when it stops. */
const MAX_FAILURES = 5;

/** How many rows a card shows before "Show all". */
const ROWS_SHOWN = 3;

/** E188's change stream: a "Live" dot while connected, "Reconnecting…" while
 *  not, and exactly one reload the moment a reconnect succeeds — never a
 *  reload on every keep-alive or every dropped poll. The server itself closes
 *  the response every ~20s (`alerts.goods_views.STREAM_SECONDS`) so a
 *  perfectly healthy connection cycles through "reconnecting" on a clock; the
 *  browser's own `EventSource` retry is what makes that invisible here.
 *
 *  Both ends of that are bounded: a reload no more often than `RELOAD_EVERY_MS`,
 *  and after `MAX_FAILURES` reopens that never carried an event the stream is
 *  closed and the screen says so, instead of spinning. */
function useEventStream(onChange: () => void, enabled: boolean): LiveStatus {
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const everConnected = useRef(false);

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const source = new EventSource(apiUrl("/goods-v1/events"), { withCredentials: true });
    let lastReloadAt = 0;
    let failures = 0;
    const reload = () => {
      const now = Date.now();
      if (now - lastReloadAt < RELOAD_EVERY_MS) return;
      lastReloadAt = now;
      onChangeRef.current();
    };
    source.addEventListener("change", () => {
      failures = 0;
      reload();
    });
    source.onopen = () => {
      // A healthy stream is *expected* to close every ~20s and reopen, so a
      // reopen is what says the last error was an ordinary cycle rather than a
      // stream that cannot stay up. Without this reset an idle screen counted
      // those cycles and shut itself off after a couple of quiet minutes.
      failures = 0;
      setStatus("live");
      if (everConnected.current) reload();
      everConnected.current = true;
    };
    source.onerror = () => {
      failures += 1;
      if (failures > MAX_FAILURES) {
        source.close();
        setStatus("stopped");
        return;
      }
      setStatus(everConnected.current ? "reconnecting" : "connecting");
    };
    return () => source.close();
  }, [enabled]);

  return status;
}

function LiveDot({ status }: { status: LiveStatus }) {
  if (status === "stopped") {
    return (
      <span className="exc-live exc-live-reconnecting" data-testid="exc-live">
        <span className="exc-live-dot" /> Live updates stopped. Reload the page.
      </span>
    );
  }
  if (status === "reconnecting") {
    return (
      <span className="exc-live exc-live-reconnecting" data-testid="exc-live">
        <span className="exc-live-dot" /> Reconnecting…
      </span>
    );
  }
  return (
    <span className={`exc-live${status === "live" ? " exc-live-on" : ""}`} data-testid="exc-live">
      <span className="exc-live-dot" /> Live
    </span>
  );
}

/** An exception's subject as a person reads it: what the document is, then the
 *  first block of its id in monospace. The whole key stays on the cell's
 *  `title` (set by the caller), so nothing is lost — it just stops a 36-character
 *  UUID from being the widest thing on the screen. */
function DocRef({ subjectId }: { subjectId: string }) {
  const { label, id } = docRef(subjectId);
  return (
    <span className="exc-doc">
      {label && <span className="exc-doc-kind">{label}</span>}
      <span className="mono">{id}</span>
    </span>
  );
}

type Tone = "late" | "today";

/** The house stat tiles (`.stat-grid` / `.card.stat-card`), drawn small. The
 *  design language asks every tile for a label, a number and a "so what" note
 *  — a bare number is unfinished. A tone colours the number only when it is
 *  not zero: a red 0 shouts about nothing. */
function StatTiles({
  testId,
  tiles,
}: {
  testId: string;
  tiles: { testId: string; n: number; label: string; note: string; tone?: Tone }[];
}) {
  return (
    <div className="stat-grid an-tiles" data-testid={testId}>
      {tiles.map((tile) => {
        const toned = tile.n > 0 ? tile.tone : undefined;
        const tone =
          toned === "late" ? " exc-count-overdue" : toned === "today" ? " an-tile-today" : "";
        return (
          <div key={tile.testId} className={`card stat-card${tone}`} data-testid={tile.testId}>
            <div className="stat-label">{tile.label}</div>
            <div className="stat-value exc-count-n">{tile.n}</div>
            <div className="exc-stat-note">{tile.note}</div>
          </div>
        );
      })}
    </div>
  );
}

function Counts({ counts, testId }: { counts: { overdue: number; dueToday: number; open: number }; testId: string }) {
  return (
    <StatTiles
      testId={testId}
      tiles={[
        {
          testId: "exc-count-overdue",
          n: counts.overdue,
          label: "Overdue",
          note: "past the promised date",
          tone: "late",
        },
        {
          testId: "exc-count-due-today",
          n: counts.dueToday,
          label: "Due today",
          note: "settle before you leave",
          tone: "today",
        },
        {
          testId: "exc-count-open",
          n: counts.open,
          label: "Open",
          note: "still to fix",
        },
      ]}
    />
  );
}

/** "due tomorrow" → "Due tomorrow", for a badge that starts a line. */
function capitalised(words: string): string {
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function rowUrgency(row: ExceptionRow): Urgency {
  if (row.overdue) return "late";
  return countsFor([row]).dueToday > 0 ? "today" : "later";
}

/** Where an exception is. One with no site belongs to the whole business. */
function siteWords(siteId: string | null, siteNames: ReadonlyMap<string, string>): string {
  if (!siteId) return "Whole company";
  return siteNames.get(siteId) ?? `Site ${siteId}`;
}

/** Who is on it, as a person says it: "You" for your own work, the role's
 *  plain name for a team's, never a raw person id. */
function ownerWords(
  row: Pick<ExceptionRow, "owner_human_id" | "owner_role">,
  myHumanId: string | null,
  myRoles: readonly string[],
): string {
  if (row.owner_human_id) {
    return row.owner_human_id === myHumanId ? "You" : "Someone else";
  }
  if (!row.owner_role) return "Unassigned";
  const team = roleLabel(row.owner_role);
  return myRoles.includes(row.owner_role) ? `You (${team})` : team;
}

// --------------------------------------------------------------------------
// Exceptions
// --------------------------------------------------------------------------

export { canSeeExceptions } from "../lib/goodsExceptions";

/** The exceptions with their filters and side panel. Drawn on Action Needed,
 *  beside the approvals inbox: both are work waiting on somebody. `onCount`
 *  hears how many exceptions are open in scope, for that screen's chips. */
/** The exception a link names (`?open=<id>`) when it is not on the first page.
 *
 *  The list answers soonest deadline first, 100 at a time, so a link from
 *  another screen (ticket 19's monitoring issue link) can name a row the first
 *  page does not hold. This follows the same list's own cursor - the same
 *  scope, the same rows the reader may already see, nothing more - until it
 *  finds that row or runs out of pages (bounded). */
const LINK_PAGE_LIMIT = 20;

function useLinkedException(
  url: string,
  openId: string | null,
  firstPage: Page<ExceptionRow>,
  loading: boolean,
): ExceptionRow | null {
  const [found, setFound] = useState<ExceptionRow | null>(null);
  useEffect(() => {
    setFound(null);
    if (loading || !openId || !firstPage.next_cursor) return;
    if (firstPage.items.some((row) => row.id === openId)) return;
    let live = true;
    (async () => {
      let cursor: string | null = firstPage.next_cursor;
      for (let n = 0; cursor && n < LINK_PAGE_LIMIT && live; n += 1) {
        const response = await api.get<Page<ExceptionRow>>(
          `${url}&cursor=${encodeURIComponent(cursor)}`,
        );
        const hit = (response.data.items ?? []).find((row) => row.id === openId);
        if (hit) {
          if (live) setFound(hit);
          return;
        }
        cursor = response.data.next_cursor ?? null;
      }
    })().catch(() => undefined);
    return () => {
      live = false;
    };
  }, [url, openId, firstPage, loading]);
  return found;
}

export function ExceptionsPanel({
  session,
  onCount,
}: {
  session: GoodsSession;
  onCount?: (n: number) => void;
}) {
  const [params, setParams] = useSearchParams();
  const siteId = params.get("site_id") ?? "";
  const kind = params.get("kind") ?? "";
  const topic = parseTopic(params.get("topic"));
  const mineOnly = params.get("mine") === "1";
  const openId = params.get("open");

  function set(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  }

  const url = `/goods-v1/exceptions?limit=100${siteId ? `&site_id=${siteId}` : ""}`;
  const {
    value: firstPage,
    loading,
    denied,
    failure,
    reload,
  } = useGoodsFetch<Page<ExceptionRow>, Page<ExceptionRow>>(
    url,
    (r) => ({ items: r.items ?? [], next_cursor: r.next_cursor ?? null }),
    { items: [], next_cursor: null },
  );
  const rowsRaw = firstPage.items;
  const linked = useLinkedException(url, openId, firstPage, loading);

  const live = useEventStream(reload, true);

  // E185 answers every exception this scope may see, open or already
  // resolved (there is no server-side state filter) — but "the place a
  // manager opens first every morning" (design §8.2) is open work, so a
  // resolved row (visible proof it really did close, on its own history)
  // never occupies a row here once its command has succeeded.
  const openRows = useMemo(() => rowsRaw.filter((row) => row.state === "open"), [rowsRaw]);
  useEffect(() => {
    if (!loading) onCount?.(openRows.length);
  }, [loading, openRows.length, onCount]);

  const myHumanId = session.user.human_id;
  const myRoles = session.roles;
  // Kind and "mine" narrow first; the topic buttons then count what is left,
  // so each button's number is exactly what pressing it shows.
  const scoped = useMemo(
    () => applyFilters(openRows, { kind, ownerHumanId: "", mineOnly }, myHumanId, myRoles),
    [openRows, kind, mineOnly, myHumanId, myRoles],
  );
  const topicCounts = useMemo(() => countTopics(scoped, exceptionTopic), [scoped]);
  const shown = useMemo(
    () => (topic ? scoped.filter((row) => exceptionTopic(row) === topic) : scoped),
    [scoped, topic],
  );
  const counts = useMemo(() => countsFor(shown), [shown]);
  const groups = useMemo(() => groupForPage(shown), [shown]);
  const siteNames = useMemo(
    () => new Map(session.sites.map((s) => [s.id, s.name])),
    [session.sites],
  );

  // Found among every open row, not only the filtered ones: a link that opens
  // one exception must open it whatever the filters happen to say.
  const selected =
    openRows.find((row) => row.id === openId) ??
    (linked && linked.state === "open" ? linked : null);

  if (denied) return <Denied what="exceptions" />;

  return (
    <div className="exceptions-layout an-stack">
      <Counts counts={counts} testId="exc-counts" />

      <div className="an-toolbar">
        <TopicFilter
          counts={topicCounts}
          total={scoped.length}
          value={topic}
          onChange={(next) => set("topic", next)}
          label="Show exceptions about"
          testId="exc-filter-kind"
          itemTestId="exc-topic"
        />
        <div className="an-toolbar-end">
          <label className="an-field" htmlFor="exc-site">
            Site
            <select
              id="exc-site"
              className="an-select"
              value={siteId}
              onChange={(e) => set("site_id", e.target.value)}
              data-testid="exc-filter-site"
            >
              <option value="">All sites</option>
              {session.sites.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
          </label>
          <label className="an-check" htmlFor="exc-mine">
            <input
              id="exc-mine"
              type="checkbox"
              checked={mineOnly}
              onChange={(e) => set("mine", e.target.checked ? "1" : "")}
              data-testid="exc-filter-mine"
            />
            Only mine
          </label>
          <LiveDot status={live} />
        </div>
      </div>

      {kind && (
        <p className="an-showing" data-testid="exc-kind-filter">
          Showing only <b>{kindGuide(kind).title}</b>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => set("kind", "")}
            data-testid="exc-kind-clear"
          >
            <X size={14} aria-hidden /> Show every kind
          </button>
        </p>
      )}

      <ExceptionGroups
        loading={loading}
        failure={failure}
        groups={groups}
        siteNames={siteNames}
        myHumanId={myHumanId}
        myRoles={myRoles}
        onOpen={(id) => set("open", id)}
      />

      {selected && (
        <ExceptionDrawer
          exception={selected}
          siteNames={siteNames}
          myHumanId={myHumanId}
          myRoles={myRoles}
          onClose={() => set("open", "")}
          onChanged={reload}
        />
      )}
    </div>
  );
}

interface RowContext {
  siteNames: ReadonlyMap<string, string>;
  myHumanId: string | null;
  myRoles: readonly string[];
  onOpen: (id: string) => void;
}

function ExceptionGroups({
  loading,
  failure,
  groups,
  ...context
}: RowContext & { loading: boolean; failure: string; groups: ExceptionCard[] }) {
  // "Loading…" only before there is anything to show: the live stream reloads
  // every ~20s, and blanking the list each time would make it flicker.
  if (failure || (loading && groups.length === 0)) {
    const state = listState({ loading, failure, empty: false }, "");
    return <div data-testid="exc-list-state">{state}</div>;
  }
  if (groups.length === 0) {
    return (
      <p className="an-empty-line" data-testid="exc-list-state">
        <CheckCircle2 size={16} aria-hidden /> Nothing needs you right now.
      </p>
    );
  }
  return (
    <div className="an-stack" data-testid="exc-groups">
      {groups.map((group) => (
        <ExceptionGroupCard key={group.key} group={group} {...context} />
      ))}
    </div>
  );
}

/** One kind of problem: what happened, what to do, and its rows. */
function ExceptionGroupCard({ group, ...context }: RowContext & { group: ExceptionCard }) {
  const [expanded, setExpanded] = useState(false);
  const rows = expanded ? group.rows : group.rows.slice(0, ROWS_SHOWN);
  const urgency: Urgency = group.overdue ? "late" : group.dueToday ? "today" : "later";
  const badge = group.overdue
    ? `${group.overdue} overdue`
    : group.dueToday
      ? `${group.dueToday} due today`
      : capitalised(ageWords(group.rows[0].due_at));
  // A cause every row shares is said once, on the card; rows only carry it
  // when it tells them apart (short, extra, damaged…).
  const causes = new Set(group.rows.map((row) => row.reason_code ?? ""));
  const sharedCause = causes.size === 1 ? group.rows[0].reason_code : null;
  const testKey = group.key.replace(/[^a-zA-Z0-9_-]/g, "-");

  return (
    <section
      className={`an-group an-group-${urgency}`}
      data-testid={`exc-group-${testKey}`}
      aria-label={group.guide.title}
    >
      <div className="an-group-head">
        <UrgencyBadge urgency={urgency}>{badge}</UrgencyBadge>
        <TopicChip topic={group.guide.topic} />
        <h3 className="an-group-title">{group.guide.title}</h3>
        <span className="an-group-count">{group.rows.length} open</span>
      </div>
      <p className="an-help">
        <b>What to do:</b> {group.guide.help}{" "}
        <span className="an-kind-name">
          ({kindLabel(group.kind)}
          {sharedCause ? (
            <>
              {" · "}
              <span data-label="Cause">{causeLabel(sharedCause)}</span>
            </>
          ) : null}
          )
        </span>
      </p>
      <ul className="an-rows">
        {rows.map((row) => (
          <ExceptionItem key={row.id} row={row} showCause={sharedCause === null} {...context} />
        ))}
      </ul>
      {group.rows.length > ROWS_SHOWN && (
        <button
          type="button"
          className="an-more"
          aria-expanded={expanded}
          onClick={() => setExpanded((open) => !open)}
          data-testid={`exc-group-more-${testKey}`}
        >
          {expanded ? "Show fewer" : `Show all ${group.rows.length}`}
        </button>
      )}
    </section>
  );
}

function ExceptionItem({
  row,
  showCause,
  siteNames,
  myHumanId,
  myRoles,
  onOpen,
}: RowContext & { row: ExceptionRow; showCause: boolean }) {
  const resolution = resolutionFor(row);
  return (
    <li className="an-row" data-testid={`exc-row-${row.id}`}>
      <div className="an-row-main">
        <span className="an-doc" data-label="Document" title={row.subject_id}>
          <DocRef subjectId={row.subject_id} />
        </span>
        <span className="an-meta">
          <span data-label="Site">{siteWords(row.site_id, siteNames)}</span>
          <span data-label="Owner">{ownerWords(row, myHumanId, myRoles)}</span>
          {showCause && <span data-label="Cause">{causeLabel(row.reason_code)}</span>}
        </span>
      </div>
      <div className="an-row-side">
        <UrgencyBadge urgency={rowUrgency(row)} testId={`exc-due-${row.id}`}>
          {capitalised(ageWords(row.due_at))}
        </UrgencyBadge>
        <span className="an-age" data-testid={`exc-age-${row.id}`}>
          {raisedAgo(row.age_seconds)}
        </span>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => onOpen(row.id)}
          data-testid={`exc-open-${row.id}`}
        >
          Details
        </button>
        {resolution.to && (
          <Link className="btn btn-sm an-fix" to={resolution.to} data-testid={`exc-fix-${row.id}`}>
            {resolution.label} <ArrowRight size={14} aria-hidden />
          </Link>
        )}
      </div>
    </li>
  );
}

function ExceptionDrawer({
  exception,
  siteNames,
  myHumanId,
  myRoles,
  onClose,
  onChanged,
}: {
  exception: ExceptionRow;
  siteNames: ReadonlyMap<string, string>;
  myHumanId: string | null;
  myRoles: readonly string[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const events = useGoodsFetch<Page<ExceptionEventRow>, ExceptionEventRow[]>(
    `/goods-v1/exceptions/${exception.id}/events`,
    (r) => r.items ?? [],
    [],
  );
  // Tracked locally, decoupled from the row still in flight in the parent's
  // list: two writes in the same drawer visit must not race the parent's own
  // reload to learn the revision the first write already produced.
  const [revision, setRevision] = useState(exception.revision);
  useEffect(() => setRevision(exception.revision), [exception.revision]);

  const [ownerInput, setOwnerInput] = useState("");
  const [noteInput, setNoteInput] = useState("");
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);

  // A side panel over the page: focus lands on Close, Esc closes it. Keyed on
  // the exception, not on `onClose` (a new function every parent render), so a
  // live reload never pulls focus out of a half-typed note.
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [exception.id]);

  async function submit(eventKind: "assigned" | "note", body: Record<string, unknown>) {
    setError("");
    setOk("");
    setBusy(true);
    try {
      const { data } = await api.post<{ revision: number }>(
        `/goods-v1/exceptions/${exception.id}/events`,
        { event_kind: eventKind, ...body, ...goodsMeta(revision) },
      );
      setRevision(data.revision);
      setOk(eventKind === "assigned" ? "Assigned." : "Note added.");
      setOwnerInput("");
      setNoteInput("");
      events.reload();
      onChanged();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const guide = kindGuide(exception.kind, exception.reason_code);
  const resolution = resolutionFor(exception);
  const doc = docRef(exception.subject_id);
  const assignedToMe = Boolean(myHumanId) && exception.owner_human_id === myHumanId;

  return (
    <>
      <div className="an-scrim" onClick={onClose} aria-hidden />
      <aside
        className="exc-drawer an-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="exc-drawer-title"
        data-testid="exc-drawer"
      >
        <div className="exc-drawer-head">
          <div className="an-drawer-title">
            <TopicChip topic={guide.topic} />
            <h3 className="h3" id="exc-drawer-title">
              {guide.title}
            </h3>
            <span className="an-kind-name">{kindLabel(exception.kind)}</span>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="btn btn-sm"
            onClick={onClose}
            data-testid="exc-drawer-close"
          >
            Close
          </button>
        </div>

        <p className="lead" data-testid="exc-drawer-cause">
          {exception.reason_code ? causeLabel(exception.reason_code) : "No cause recorded."}
        </p>
        <p className="an-help">
          <b>What to do:</b> {guide.help}
        </p>

        <dl className="gr-facts">
          <div>
            <dt>Document</dt>
            <dd title={exception.subject_id}>
              {doc.label} <span className="mono">{doc.id}</span>
            </dd>
          </div>
          <div>
            <dt>Where</dt>
            <dd>{siteWords(exception.site_id, siteNames)}</dd>
          </div>
          <div>
            <dt>Due</dt>
            <dd data-testid="exc-drawer-due">
              {exception.due_at
                ? `${ageWords(exception.due_at)} · ${formatDateTime(exception.due_at)}`
                : NO_DUE_DATE}
            </dd>
          </div>
          <div>
            <dt>Raised</dt>
            <dd data-testid="exc-drawer-age">
              {raisedAgo(exception.age_seconds)} · {formatDateTime(exception.opened_at)}
            </dd>
          </div>
          <div>
            <dt>Owner</dt>
            <dd>{ownerWords(exception, myHumanId, myRoles)}</dd>
          </div>
        </dl>

        <div className="exc-drawer-resolve">
          {resolution.to ? (
            <Link className="btn btn-cta" to={resolution.to} data-testid="exc-resolve-link">
              Resolve via: {resolution.label}
            </Link>
          ) : (
            <p className="muted" data-testid="exc-resolve-none">
              Resolve via: {resolution.label}
            </p>
          )}
          <p className="exc-drawer-hint">This exception closes when that command succeeds.</p>
        </div>

        <Feedback error={error} ok={ok} />

        <div className="an-drawer-form">
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy || !myHumanId || assignedToMe}
            onClick={() => myHumanId && submit("assigned", { owner_human_id: myHumanId })}
            data-testid="exc-assign-me"
          >
            <UserCheck size={14} aria-hidden /> {assignedToMe ? "Assigned to you" : "Assign to me"}
          </button>
        </div>

        <div className="an-drawer-form">
          <label htmlFor="exc-assign-to">Or assign it to someone else (their person id)</label>
          <div className="an-inline">
            <input
              id="exc-assign-to"
              className="input"
              value={ownerInput}
              onChange={(e) => setOwnerInput(e.target.value)}
              data-testid="exc-assign-input"
            />
            <button
              type="button"
              className="btn btn-sm"
              disabled={busy || !ownerInput.trim()}
              onClick={() => submit("assigned", { owner_human_id: ownerInput.trim() })}
              data-testid="exc-assign-submit"
            >
              Assign
            </button>
          </div>
        </div>

        <div className="an-drawer-form">
          <label htmlFor="exc-note">Add a note</label>
          <textarea
            id="exc-note"
            className="input"
            value={noteInput}
            onChange={(e) => setNoteInput(e.target.value)}
            data-testid="exc-note-input"
          />
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy || !noteInput.trim()}
            onClick={() => submit("note", { note: noteInput.trim() })}
            data-testid="exc-note-submit"
          >
            Add note
          </button>
        </div>

        <h4 className="gr-h4">History</h4>
        {(() => {
          const state = listState(
            { loading: events.loading, failure: events.failure, empty: events.value.length === 0 },
            "Nothing recorded yet.",
          );
          if (state) return state;
          return (
            <ol className="exc-event-log" data-testid="exc-event-log">
              {events.value.map((event) => (
                <li key={event.id} data-testid={`exc-event-${event.id}`}>
                  <span className="exc-event-kind">{eventLabel(event.event_kind)}</span>
                  <span className="exc-event-meta">{formatDateTime(event.recorded_at)}</span>
                  {typeof event.payload?.note === "string" && event.payload.note && (
                    <p className="exc-event-note">{event.payload.note}</p>
                  )}
                </li>
              ))}
            </ol>
          );
        })()}
      </aside>
    </>
  );
}

// --------------------------------------------------------------------------
// Goods notifications (E179/E180/E187)
// --------------------------------------------------------------------------

/** "2:14 pm": the day is already the heading above the row. */
function timeOfDay(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
}

/** Opening a notification is reading it. Marked quietly, with no wait: the
 *  page is being left, and the feed reloads when it is next opened. */
function markReadQuietly(ids: string[]) {
  if (ids.length === 0) return;
  void api
    .post("/goods-v1/alerts/seen", { notification_ids: ids, ...goodsMeta() })
    .catch(() => undefined);
}

/** The recipient-scoped goods notification feed, with Mark read. Drawn on the
 *  Alerts screen, below the deadlines: both are heads-ups that need no
 *  decision. */
export function GoodsNotificationsFeed() {
  const { session } = useAuth();
  const siteNames = useMemo(
    () => new Map((session?.sites ?? []).map((s) => [s.id, s.name])),
    [session],
  );
  const [showHistory, setShowHistory] = useState(false);
  const [topic, setTopic] = useState<Topic | "">("");
  const url = showHistory ? "/goods-v1/alerts/history?limit=100" : "/goods-v1/alerts?limit=100";
  const { value: rows, loading, denied, failure, reload } = useGoodsFetch<
    Page<NotificationRow>,
    NotificationRow[]
  >(url, (r) => r.items ?? [], []);
  const live = useEventStream(reload, true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function markSeen(ids: string[]) {
    if (ids.length === 0) return;
    setBusy(true);
    setError("");
    try {
      await api.post("/goods-v1/alerts/seen", { notification_ids: ids, ...goodsMeta() });
      reload();
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const topicCounts = useMemo(() => countTopics(rows, notificationTopic), [rows]);
  const shown = useMemo(
    () => (topic ? rows.filter((row) => notificationTopic(row) === topic) : rows),
    [rows, topic],
  );
  const days = useMemo(() => notificationDays(shown), [shown]);

  if (denied) return <Denied what="alerts" />;

  const unseen = rows.filter((r) => !r.seen).length;
  const today = new Date().toLocaleDateString("en-CA");
  const arrivedToday = rows.filter(
    (r) => new Date(r.created_at).toLocaleDateString("en-CA") === today,
  ).length;
  const shownUnseen = shown.filter((r) => !r.seen).map((r) => r.id);

  return (
    <div className="exceptions-layout an-stack">
      <StatTiles
        testId="alerts-counts"
        tiles={[
          {
            testId: "alerts-count-unseen",
            n: unseen,
            label: "Unread",
            note: "not opened yet",
            tone: "today",
          },
          {
            testId: "alerts-count-today",
            n: arrivedToday,
            label: "Today",
            note: "arrived today",
          },
          {
            testId: "alerts-count-total",
            n: rows.length,
            label: showHistory ? "All" : "In this list",
            note: showHistory ? "read and unread" : "waiting to be read",
          },
        ]}
      />

      <div className="an-toolbar">
        <div className="an-seg" role="group" aria-label="Which notifications">
          <button
            type="button"
            className={`btn btn-sm${!showHistory ? " btn-active" : ""}`}
            aria-pressed={!showHistory}
            onClick={() => setShowHistory(false)}
            data-testid="alerts-view-open"
          >
            Unread
          </button>
          <button
            type="button"
            className={`btn btn-sm${showHistory ? " btn-active" : ""}`}
            aria-pressed={showHistory}
            onClick={() => setShowHistory(true)}
            data-testid="alerts-view-history"
          >
            All
          </button>
        </div>
        <TopicFilter
          counts={topicCounts}
          total={rows.length}
          value={topic}
          onChange={setTopic}
          label="Show notifications about"
          testId="alerts-topics"
          itemTestId="alerts-topic"
        />
        <div className="an-toolbar-end">
          {shownUnseen.length > 0 && (
            <button
              type="button"
              className="btn btn-sm"
              disabled={busy}
              onClick={() => markSeen(shownUnseen)}
              data-testid="alerts-mark-all-seen"
            >
              <CheckCheck size={14} aria-hidden /> {topic ? "Mark these as read" : "Mark all as read"}
            </button>
          )}
          <LiveDot status={live} />
        </div>
      </div>

      <Feedback error={error} ok="" />

      {(() => {
        if (failure || (loading && rows.length === 0)) {
          return (
            <div data-testid="alerts-list-state">
              {listState({ loading, failure, empty: false }, "")}
            </div>
          );
        }
        if (shown.length === 0) {
          return (
            <p className="an-empty-line" data-testid="alerts-list-state">
              <CheckCircle2 size={16} aria-hidden />{" "}
              {showHistory ? "No notifications yet." : "You're all caught up."}
            </p>
          );
        }
        return (
          <div className="an-days" data-testid="alerts-list">
            {days.map((day) => (
              <section key={day.day} className="an-day" data-testid={`alerts-day-${day.day}`}>
                <h3 className="an-day-head">{day.heading}</h3>
                <ul className="an-notes-list">
                  {day.items.map((item) =>
                    item.type === "one" ? (
                      <NoteRow
                        key={item.row.id}
                        row={item.row}
                        siteNames={siteNames}
                        busy={busy}
                        onMarkSeen={() => markSeen([item.row.id])}
                      />
                    ) : (
                      <NoteBundle
                        key={item.key}
                        bundleKey={item.key}
                        rows={item.rows}
                        siteNames={siteNames}
                        busy={busy}
                        onMarkSeen={markSeen}
                      />
                    ),
                  )}
                </ul>
              </section>
            ))}
          </div>
        );
      })()}
    </div>
  );
}

function NoteRow({
  row,
  siteNames,
  busy,
  onMarkSeen,
}: {
  row: NotificationRow;
  siteNames: ReadonlyMap<string, string>;
  busy: boolean;
  onMarkSeen: () => void;
}) {
  const path = notificationPath(row);
  const site = row.site_id ? (siteNames.get(row.site_id) ?? null) : null;
  return (
    <li className={`an-note${row.seen ? " an-note-seen" : ""}`} data-testid={`alerts-row-${row.id}`}>
      <span
        className={`an-dot${row.seen ? "" : " an-dot-unread"}`}
        role="img"
        aria-label={row.seen ? "Read" : "Unread"}
      />
      <TopicChip topic={notificationTopic(row)} />
      <span className="an-note-text" title={row.title}>
        {notificationSentence(row)}
        {site && <span className="an-note-where"> · {site}</span>}
      </span>
      <span className="an-note-time">{timeOfDay(row.created_at)}</span>
      <span className="an-note-actions">
        {path && (
          <Link
            className="btn btn-sm"
            to={path}
            onClick={() => markReadQuietly(row.seen ? [] : [row.id])}
            data-testid={`alerts-open-${row.id}`}
          >
            Open <ArrowRight size={14} aria-hidden />
          </Link>
        )}
        {!row.seen && (
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy}
            onClick={onMarkSeen}
            data-testid={`alerts-mark-seen-${row.id}`}
          >
            Mark read
          </button>
        )}
      </span>
    </li>
  );
}

/** Three or more of one story on one day, as one line: "65 new problems:
 *  Access change to review", with the single rows one click away. */
function NoteBundle({
  bundleKey,
  rows,
  siteNames,
  busy,
  onMarkSeen,
}: {
  bundleKey: string;
  rows: NotificationRow[];
  siteNames: ReadonlyMap<string, string>;
  busy: boolean;
  onMarkSeen: (ids: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const newest = rows[0];
  const unseenIds = rows.filter((r) => !r.seen).map((r) => r.id);
  const path = notificationPath(newest);
  const places = new Set(rows.map((r) => (r.site_id ? (siteNames.get(r.site_id) ?? r.site_id) : "")));
  const where =
    places.size === 1 ? [...places][0] : places.size > 1 ? `${places.size} places` : "";
  const testKey = bundleKey.replace(/[^a-zA-Z0-9_-]/g, "-");
  return (
    <li
      className={`an-note${unseenIds.length ? "" : " an-note-seen"}`}
      data-testid={`alerts-bundle-${testKey}`}
    >
      <span
        className={`an-dot${unseenIds.length ? " an-dot-unread" : ""}`}
        role="img"
        aria-label={unseenIds.length ? `${unseenIds.length} unread` : "Read"}
      />
      <TopicChip topic={notificationTopic(newest)} />
      <span className="an-note-text">
        {notificationBundleSentence(newest, rows.length)}
        {where && <span className="an-note-where"> · {where}</span>}
      </span>
      <span className="an-note-time">{timeOfDay(newest.created_at)}</span>
      <span className="an-note-actions">
        {path && (
          <Link
            className="btn btn-sm"
            to={path}
            onClick={() => markReadQuietly(unseenIds)}
            data-testid={`alerts-bundle-open-${testKey}`}
          >
            Open <ArrowRight size={14} aria-hidden />
          </Link>
        )}
        <button
          type="button"
          className="btn btn-sm"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          data-testid={`alerts-bundle-toggle-${testKey}`}
        >
          {open ? "Hide" : `Show all ${rows.length}`}
        </button>
        {unseenIds.length > 0 && (
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy}
            onClick={() => onMarkSeen(unseenIds)}
            data-testid={`alerts-bundle-seen-${testKey}`}
          >
            Mark read
          </button>
        )}
      </span>
      {open && (
        <ul className="an-bundle-rows">
          {rows.map((row) => (
            <NoteRow
              key={row.id}
              row={row}
              siteNames={siteNames}
              busy={busy}
              onMarkSeen={() => onMarkSeen([row.id])}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
