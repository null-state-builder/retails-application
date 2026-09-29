import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
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
 *  margin never reach a store role's screen at all. It reads live data only, so
 *  offline it says it needs a connection instead of showing a stale copy as
 *  current. */
export function AuditLogPage() {
  const [filters, setFilters] = useState<AuditFilters>(NO_FILTERS);
  const [data, setData] = useState<Payload | null>(null);
  const [items, setItems] = useState<Entry[]>([]);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: AuditFilters, cursor?: string) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    setBusy(true);
    setError("");
    try {
      const response = await api.get<Payload>("/goods-v1/masters/audit-log", {
        params: { ...filterParams(wanted), ...(cursor ? { cursor } : {}) },
      });
      if (mine !== request.current) return;
      setLost(false);
      // A cursor page carries no filter choices: keep the first page's.
      setData((current) =>
        cursor && current
          ? {
              ...response.data,
              stores: current.stores,
              people: current.people,
              record_types: current.record_types,
            }
          : response.data,
      );
      setItems((current) => (cursor ? [...current, ...response.data.items] : response.data.items));
    } catch (reason) {
      if (mine !== request.current) return;
      if (isConnectionLost(reason)) setLost(true);
      else setError(apiErrorMessage(reason));
    } finally {
      if (mine === request.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load(filters);
  }, [filters, load]);

  useEffect(() => {
    const up = () => {
      setOnline(true);
      void load(filters);
    };
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, [filters, load]);

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
    <p className="warn-note" data-testid="audit-log-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="audit-log-retry"
            disabled={busy}
            onClick={() => void load(filters)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Audit Log" />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="audit-log-error">
            {error}
          </p>
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
      />
      {offlineNote}
      <section className="card section-card">
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
          <p className="warn-note" data-testid="audit-log-error">
            {error}
          </p>
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
            onClick={() => void load(filters, data.next_cursor ?? undefined)}
          >
            Show older entries
          </button>
        )}
      </section>
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
          <span className="chip chip-amber" data-testid="audit-row-version">
            {note}
          </span>
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
