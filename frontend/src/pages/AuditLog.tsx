import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { Note } from "../components/Note";
import { RefreshFailed } from "../components/RefreshFailed";
import { UpdatedAgo } from "../components/UpdatedAgo";
import { Status } from "../components/Status";
import { api, apiErrorMessage } from "../lib/api";
import { useCachedGet } from "../lib/cachedQuery";
import type { ApiRead, ApiSchemas } from "../lib/api";
import {
  NO_FILTERS,
  actionText,
  filterParams,
  isConnectionLost,
  valueLines,
  versionNote,
  whereText,
  whoText,
} from "../lib/auditLog";
import type { AuditFilters } from "../lib/auditLog";
import { formatDateTime } from "../lib/format";

type Payload = ApiRead<ApiSchemas["AuditLog"]>;
type Entry = Payload["items"][number];

const OFFLINE = "The audit log needs a connection. Reconnect to see or export it.";

/** Setup > Audit Log (store operations PRD ST-OPS-3).
 *
 *  Every recorded write at the stores this person may read, newest first: who,
 *  what, when, where, and the values before and after. Read-only: there is no
 *  control that changes an entry, and the server has no route that would. The
 *  server decides which stores, people and values this reader sees - cost and
 *  margin never reach a store role's screen at all.
 *
 *  The newest page for the filters is read through the shared cache (memory
 *  only, keyed by person, store and role; never saved on the device): a log
 *  seen before shows at once, says how old it is, and is read again behind it.
 *  Offline it still says it needs a connection. Older pages are fetched on
 *  demand and kept only while that newest page stands. */
export function AuditLogPage() {
  const [filters, setFilters] = useState<AuditFilters>(NO_FILTERS);
  const [commandError, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but a request (an export, an older page)
  // got no answer at all.
  const [commandLost, setLost] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [older, setOlder] = useState<{
    basis: Payload;
    items: Entry[];
    next: string | null;
  } | null>(null);
  const request = useRef(0);

  const read = useCachedGet<Payload>("/goods-v1/masters/audit-log", {
    params: filterParams(filters),
  });
  const first = read.data ?? null;
  const extra = first && !read.loading && older?.basis === first ? older : null;
  const data: Payload | null = first
    ? { ...first, next_cursor: extra ? extra.next : first.next_cursor }
    : null;
  const items: Entry[] = first ? [...first.items, ...(extra?.items ?? [])] : [];
  const readLost = read.error != null && isConnectionLost(read.error);
  const lost = commandLost || readLost;
  const readFailure = read.error != null && !readLost && !first ? apiErrorMessage(read.error) : "";
  const error = commandError || readFailure;
  const busy = read.loading || loadingMore;
  const { reload } = read;

  useEffect(() => {
    if (read.updatedAt) setLost(false);
  }, [read.updatedAt]);

  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    setLost(false);
    reload();
  }, [reload]);

  async function loadOlder(cursor: string) {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    if (!first) return;
    const mine = ++request.current;
    setLoadingMore(true);
    setError("");
    try {
      const response = await api.get<Payload>("/goods-v1/masters/audit-log", {
        params: { ...filterParams(filters), cursor },
      });
      if (mine !== request.current) return;
      setLost(false);
      setOlder({
        basis: first,
        items: [...(extra?.items ?? []), ...response.data.items],
        next: response.data.next_cursor,
      });
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      if (mine === request.current) setLoadingMore(false);
    }
  }

  // New filters: an older page still in flight belongs to the old ones.
  useEffect(() => {
    request.current += 1;
    setLoadingMore(false);
  }, [filters]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      load();
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [load]);

  function set(key: keyof AuditFilters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/goods-v1/masters/audit-log/export.xlsx", {
        params: filterParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `audit-log-${new Date().toISOString().slice(0, 10)}.xlsx`;
      link.click();
      URL.revokeObjectURL(href);
    } catch (reason) {
      if (isConnectionLost(reason)) {
        setLost(true);
        return;
      }
      setError(await blobErrorMessage(reason));
    }
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <Note
      tone="warn"
      role="status"
      testId="audit-log-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="audit-log-retry"
            disabled={busy}
            onClick={() => load()}
          >
            Try again
          </button>
        )
      }
    >
      {OFFLINE}
    </Note>
  );

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Audit Log" />
        {offlineNote}
        {error ? (
          <Note tone="error" testId="audit-log-error">
            {error}
          </Note>
        ) : (
          online && !lost && <p>Loading the audit log…</p>
        )}
      </div>
    );
  }

  return (
    <div className="page-pad">
      <PageHeader
        title="Audit Log"
        lead="Every recorded change at your stores: who, what, when, where, and the values before and after. Entries cannot be changed or deleted."
        actions={
          <UpdatedAgo
            updatedAt={read.updatedAt}
            fetching={read.fetching}
            testId="audit-log-updated"
          />
        }
      />
      {offlineNote}
      <RefreshFailed
        error={readLost ? null : read.error}
        shown
        updatedAt={read.updatedAt}
        onRetry={load}
        testId="audit-log-refresh-failed"
      />
      <div className="toolbar" data-testid="audit-log-filters">
        <label className="field">
          <span>Person</span>
          <select
            className="input"
            data-testid="audit-filter-person"
            value={filters.person}
            disabled={disabled}
            onChange={(event) => set("person", event.target.value)}
          >
            <option value="">Everyone</option>
            {data.people.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Store</span>
          <select
            className="input"
            data-testid="audit-filter-store"
            value={filters.store}
            disabled={disabled}
            onChange={(event) => set("store", event.target.value)}
          >
            <option value="">All my stores</option>
            {data.stores.map((s) => (
              <option key={s.id} value={String(s.id)}>
                {s.name} ({s.code})
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Record type</span>
          <select
            className="input"
            data-testid="audit-filter-record-type"
            value={filters.record_type}
            disabled={disabled}
            onChange={(event) => set("record_type", event.target.value)}
          >
            <option value="">All</option>
            {data.record_types.map((t) => (
              <option key={t} value={t}>
                {t.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>From</span>
          <input
            className="input"
            type="date"
            data-testid="audit-filter-from"
            value={filters.date_from}
            disabled={disabled}
            onChange={(event) => set("date_from", event.target.value)}
          />
        </label>
        <label className="field">
          <span>To</span>
          <input
            className="input"
            type="date"
            data-testid="audit-filter-to"
            value={filters.date_to}
            disabled={disabled}
            onChange={(event) => set("date_to", event.target.value)}
          />
        </label>
        <button
          type="button"
          className="btn"
          data-testid="audit-log-clear"
          disabled={disabled}
          onClick={() => setFilters(NO_FILTERS)}
        >
          Clear
        </button>
        <button
          type="button"
          className="btn btn-primary"
          data-testid="audit-log-export"
          disabled={disabled}
          onClick={() => void exportWorkbook()}
        >
          Export to Excel
        </button>
      </div>
      {error && (
        <Note tone="error" testId="audit-log-error">
          {error}
        </Note>
      )}
      {items.length === 0 ? (
        <p className="muted-cell" data-testid="audit-log-empty">
          {busy ? "Loading…" : "Nothing recorded matches these filters."}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="data" data-testid="audit-log-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Who</th>
                <th>Where</th>
                <th>What</th>
                <th>Before</th>
                <th>After</th>
              </tr>
            </thead>
            <tbody>
              {items.map((entry) => (
                <AuditRow key={entry.id} entry={entry} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.next_cursor && (
        <button
          type="button"
          className="btn"
          data-testid="audit-log-more"
          disabled={disabled}
          onClick={() => data.next_cursor && void loadOlder(data.next_cursor)}
        >
          Show older entries
        </button>
      )}
    </div>
  );
}

function AuditRow({ entry }: { entry: Entry }) {
  const note = versionNote(entry.after);
  return (
    <tr data-testid="audit-log-row" data-record={entry.record_key ?? ""}>
      <td>{formatDateTime(entry.recorded_at)}</td>
      <td data-testid="audit-row-who">{whoText(entry)}</td>
      <td data-testid="audit-row-where">{whereText(entry)}</td>
      <td data-testid="audit-row-what">
        <strong title={entry.action}>{actionText(entry.action)}</strong>
        <div className="muted-cell">
          {entry.record_key ?? "No record"}
          {entry.outcome !== "succeeded" && ` · ${entry.outcome}`}
          {entry.reason_code && ` · ${entry.reason_code}`}
        </div>
        {note && (
          <div>
            <Status tone="amber" testId="audit-row-version">
              {note}
            </Status>
          </div>
        )}
      </td>
      <td data-testid="audit-row-before">
        <Values lines={valueLines(entry.before)} />
      </td>
      <td data-testid="audit-row-after">
        <Values lines={valueLines(entry.after)} />
      </td>
    </tr>
  );
}

function Values({ lines }: { lines: string[] }) {
  if (lines.length === 0) return <span className="muted-cell">Nothing recorded</span>;
  return (
    <div>
      {lines.map((line, index) => (
        <div key={index}>{line}</div>
      ))}
    </div>
  );
}

/** A refused download arrives as a Blob; read the server's sentence out of it. */
async function blobErrorMessage(reason: unknown): Promise<string> {
  const data = (reason as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = JSON.parse(await data.text()) as unknown;
      return apiErrorMessage({ response: { data: parsed } });
    } catch {
      return "The export could not be made. Please try again.";
    }
  }
  return apiErrorMessage(reason);
}
