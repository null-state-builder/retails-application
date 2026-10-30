// E247 administrative history (tickets 02C and 03D): one subject's recorded
// changes, newest first, a page at a time. The server applies scope and field
// masking; this only renders what it was given and never names a hidden actor.
import { useEffect, useRef, useState } from "react";
import { History } from "lucide-react";
import { Link } from "react-router-dom";

import { api, apiErrorMessage, type ApiRead } from "../lib/api";
import type { paths } from "../lib/api-schema";
import {
  historyActionLabel,
  historyActorLabel,
  historyPath,
  type HistorySubjectKind,
} from "../lib/administrativeHistory";
import { useCachedGet } from "../lib/cachedQuery";
import { Note } from "./Note";
import { RefreshFailed } from "./RefreshFailed";
import { Section } from "./Section";

type AdministrativeHistoryOperation =
  paths["/api/goods-v1/history/{subject_kind}/{subject_id}"]["get"];
type AdministrativeHistoryPage = ApiRead<
  AdministrativeHistoryOperation["responses"][200]["content"]["application/json"]
>;
export type AdministrativeHistoryEvent = ApiRead<AdministrativeHistoryPage["items"][number]>;
type AdministrativeAuditValue = ApiRead<NonNullable<AdministrativeHistoryEvent["before"]>[number]>;

function auditValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean")
    return String(value);
  return JSON.stringify(value);
}

function AuditValues({
  label,
  values,
}: {
  label: string;
  values: AdministrativeAuditValue[] | null;
}) {
  if (!values?.length) return null;
  return (
    <div>
      <b>{label}</b>
      <ul className="muted-cell" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
        {values.map((value) => (
          <li key={value.field}>
            <span className="mono">{value.field}</span>:{" "}
            {value.redacted ? "Hidden" : auditValue(value.value)}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AdministrativeHistory({
  subjectKind,
  subjectId,
  refreshKey,
  testId,
  title = "History",
  eventLink,
}: {
  subjectKind: HistorySubjectKind;
  subjectId: string;
  refreshKey?: string | null;
  testId: string;
  title?: string;
  /** A link shown under one event, e.g. to the privileged change a review acknowledges. */
  eventLink?: (event: AdministrativeHistoryEvent) => { to: string; label: string } | null;
}) {
  // The newest page through the shared cache (keyed by person, store and
  // role): a history opened before shows at once and is read again behind it.
  // Older pages are fetched on demand and kept only while that first page
  // stands; a new first page (a refresh, another subject) starts them over.
  const read = useCachedGet<AdministrativeHistoryPage>(historyPath(subjectKind, subjectId), {
    keepPrevious: false,
  });
  const first = read.data;
  const [older, setOlder] = useState<{
    basis: AdministrativeHistoryPage;
    items: AdministrativeHistoryEvent[];
    next: string | null;
  } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState("");
  const requestGeneration = useRef(0);
  const extra = first && older?.basis === first ? older : null;
  const items = first ? [...first.items, ...(extra?.items ?? [])] : [];
  const next = extra ? extra.next : (first?.next_cursor ?? null);
  const loading = read.loading;
  const error = !first && read.error != null ? apiErrorMessage(read.error) : "";

  async function loadMore(cursor: string) {
    if (!first) return;
    const generation = ++requestGeneration.current;
    setLoadingMore(true);
    setMoreError("");
    try {
      const response = await api.get<AdministrativeHistoryPage>(
        historyPath(subjectKind, subjectId),
        { params: { cursor } },
      );
      if (generation !== requestGeneration.current) return;
      setOlder({
        basis: first,
        items: [...(extra?.items ?? []), ...response.data.items],
        next: response.data.next_cursor,
      });
    } catch (e) {
      if (generation !== requestGeneration.current) return;
      setMoreError(apiErrorMessage(e));
    } finally {
      if (generation === requestGeneration.current) setLoadingMore(false);
    }
  }

  // A changed subject or refresh key: an older page still in flight must not
  // paint over the replacement history.
  useEffect(
    () => () => {
      requestGeneration.current += 1;
    },
    [subjectKind, subjectId, refreshKey],
  );

  // The caller says the subject changed (a save, a retire): read it again now,
  // with what is shown kept on screen meanwhile. Not on first sight: that read
  // is the cache's own.
  const { reload } = read;
  const lastRefresh = useRef(refreshKey);
  useEffect(() => {
    if (lastRefresh.current === refreshKey) return;
    lastRefresh.current = refreshKey;
    reload();
  }, [refreshKey, reload]);

  return (
    <Section
      level={3}
      heading={
        <>
          <History size={16} aria-hidden="true" /> {title}
        </>
      }
      testId={testId}
    >
      <RefreshFailed
        error={read.error}
        shown={Boolean(first)}
        updatedAt={read.updatedAt}
        onRetry={reload}
        testId={`${testId}-refresh-failed`}
      />
      {loading ? (
        <p className="lead">Loading history…</p>
      ) : error ? (
        <Note tone="error">{error}</Note>
      ) : items.length === 0 ? (
        <p className="lead">No recorded changes yet.</p>
      ) : (
        <ol className="gr-history" data-testid={`${testId}-events`}>
          {items.map((event) => (
            <li key={event.id}>
              <div>
                <b>{historyActionLabel(event.event_kind, event.outcome)}</b>
                {event.revision !== null && ` · revision ${event.revision}`}
              </div>
              <div className="muted-cell">
                {new Date(event.recorded_at).toLocaleString("en-IN")} ·{" "}
                {historyActorLabel(event.actor_name)}
                {event.reason_code && ` · ${event.reason_code}`}
              </div>
              <AuditValues label="Before" values={event.before} />
              <AuditValues label="After" values={event.after} />
              {(() => {
                const link = eventLink?.(event);
                return link ? (
                  <Link to={link.to} data-testid={`${testId}-link-${event.id}`}>
                    {link.label}
                  </Link>
                ) : null;
              })()}
            </li>
          ))}
        </ol>
      )}
      {moreError && <Note tone="error">{moreError}</Note>}
      {next && !loading && !error && (
        <button
          className="btn btn-sm"
          onClick={() => void loadMore(next)}
          disabled={loadingMore}
          data-testid={`${testId}-more`}
        >
          {loadingMore ? "Loading…" : "Show older history"}
        </button>
      )}
    </Section>
  );
}
