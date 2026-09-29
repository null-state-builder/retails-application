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
  const [items, setItems] = useState<AdministrativeHistoryEvent[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const requestGeneration = useRef(0);

  async function load(cursor: string | null, append: boolean) {
    const generation = ++requestGeneration.current;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setError("");
    try {
      const response = await api.get<AdministrativeHistoryPage>(
        historyPath(subjectKind, subjectId),
        { params: cursor ? { cursor } : undefined },
      );
      if (generation !== requestGeneration.current) return;
      setItems((current) => (append ? [...current, ...response.data.items] : response.data.items));
      setNext(response.data.next_cursor);
    } catch (e) {
      if (generation !== requestGeneration.current) return;
      setError(apiErrorMessage(e));
    } finally {
      if (generation === requestGeneration.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }

  useEffect(() => {
    void load(null, false);
    return () => {
      // Cleanup runs before the next subject effect: an old response can no
      // longer paint over the replacement history while React changes props.
      requestGeneration.current += 1;
    };
  }, [subjectKind, subjectId, refreshKey]);

  return (
    <div className="card section-card" data-testid={testId}>
      <div className="toolbar" style={{ marginBottom: 8 }}>
        <h4 className="h3">
          <History size={16} /> {title}
        </h4>
      </div>
      {loading ? (
        <p className="lead">Loading history…</p>
      ) : error ? (
        <p className="warn-note">{error}</p>
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
      {next && (
        <button
          className="btn btn-sm"
          onClick={() => void load(next, true)}
          disabled={loadingMore}
          data-testid={`${testId}-more`}
        >
          {loadingMore ? "Loading…" : "Show older history"}
        </button>
      )}
    </div>
  );
}
