import { useCallback, useEffect, useRef, useState } from "react";

import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { alignsRight, gstCell } from "../lib/gstReport";
import type { GstColumn, GstRow } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import {
  EXCEPTIONS_GROUPINGS,
  defaultExceptionsFilters,
  exceptionsParams,
} from "../lib/exceptionsReport";
import type { ExceptionsFilters, ExceptionsGroupBy } from "../lib/exceptionsReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["ExceptionsReport"]>;

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Exceptions (store operations PRD ST-RPT-6, ticket 48).
 *
 *  The counter's exceptions - cancelled bills, manual discounts, manager PIN
 *  uses by manager, cash variances, bill-number holes, returns without a bill
 *  (and bills over their monthly cap) and late syncs - by store, by staff
 *  member, or one by one. The figures come from the reporting copy the server
 *  keeps apart from billing; the server decides which stores. It reads live
 *  data only: offline it says it needs a connection, and comes back by itself
 *  when the line does. */
export function ExceptionsReportPage() {
  const [filters, setFilters] = useState<ExceptionsFilters>(() => defaultExceptionsFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: ExceptionsFilters) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    if (wanted.date_from && wanted.date_to && wanted.date_to < wanted.date_from) {
      setError("The period ends before it starts. Choose a To date on or after the From date.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await api.get<Payload>("/reports/exceptions", {
        params: exceptionsParams(wanted),
      });
      if (mine !== request.current) return;
      setLost(false);
      setData(response.data);
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

  function set<K extends keyof ExceptionsFilters>(key: K, value: ExceptionsFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/exceptions/export.xlsx", {
        params: exceptionsParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `exceptions-report-${filters.group_by}-${filters.date_from}-${filters.date_to}.xlsx`;
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
    <p className="warn-note" data-testid="exceptions-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="exceptions-retry"
            disabled={busy}
            onClick={() => void load(filters)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const lead =
    "Cancelled bills, manual discounts, manager PIN uses, cash variances, missing bill numbers, returns without a bill and late syncs, by store and staff member.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Exceptions" lead={lead} />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="exceptions-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the exceptions report…</p>
        )}
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = data.rows as unknown as GstRow[];
  const total = data.total as unknown as GstRow;

  return (
    <OperationsPage>
      <PageHeader title="Exceptions" lead={lead} />
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="exceptions-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="exceptions-filter-store"
              value={filters.store}
              disabled={disabled}
              onChange={(event) => set("store", event.target.value)}
            >
              <option value="">All my stores</option>
              {data.store_options.map((s) => (
                <option key={s.id} value={s.code}>
                  {s.name} ({s.code})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>From</span>
            <input
              className="input"
              type="date"
              data-testid="exceptions-filter-from"
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
              data-testid="exceptions-filter-to"
              value={filters.date_to}
              disabled={disabled}
              onChange={(event) => set("date_to", event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="exceptions-export"
            disabled={disabled}
            onClick={() => void exportWorkbook()}
          >
            Export to Excel
          </button>
        </div>
        <div
          className="seg"
          role="tablist"
          aria-label="Group by"
          data-testid="exceptions-groupings"
          style={{ flexWrap: "wrap" }}
        >
          {EXCEPTIONS_GROUPINGS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={filters.group_by === v.key}
              className={`seg-btn ${filters.group_by === v.key ? "active" : ""}`}
              data-testid={`exceptions-group-${v.key}`}
              disabled={disabled}
              onClick={() => set("group_by", v.key as ExceptionsGroupBy)}
            >
              {v.label}
            </button>
          ))}
        </div>
        <p className="muted-cell" data-testid="exceptions-as-of">
          {asOfText(data.as_of)}
          {data.as_of &&
            ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {error && (
          <p className="warn-note" data-testid="exceptions-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="exceptions-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`exceptions-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {rows.length === 0 ? (
          <p className="muted-cell" data-testid="exceptions-empty">
            {busy ? "Loading…" : "No exceptions in this period at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="exceptions-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={alignsRight(c.kind) ? "num" : undefined}
                      data-testid={`exceptions-col-${c.key}`}
                    >
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <ExceptionsTableRow key={String(row.key ?? index)} row={row} columns={columns} />
                ))}
              </tbody>
              <tfoot>
                <ExceptionsTableRow row={total} columns={columns} total />
              </tfoot>
            </table>
          </div>
        )}
        <details className="muted-cell" data-testid="exceptions-basis">
          <summary>How these figures are worked out (formula {data.formula_version})</summary>
          <ul>
            {data.basis.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      </section>
    </OperationsPage>
  );
}

function ExceptionsTableRow({
  row,
  columns,
  total,
}: {
  row: GstRow;
  columns: GstColumn[];
  total?: boolean;
}) {
  return (
    <tr
      data-testid={total ? "exceptions-total" : "exceptions-row"}
      data-key={String(row.key ?? "")}
    >
      {columns.map((c, index) => {
        const text = gstCell(row[c.key], c.kind);
        return (
          <td
            key={c.key}
            className={alignsRight(c.kind) ? "num tabular" : undefined}
            data-testid={`exceptions-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
    </tr>
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
