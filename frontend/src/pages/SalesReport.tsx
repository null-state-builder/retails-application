import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  GROUPINGS,
  asOfText,
  cellText,
  columnsFor,
  defaultFilters,
  filterParams,
} from "../lib/salesReport";
import type { GroupBy, Row, SalesFilters } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["SalesReport"]>;

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Sales (store operations PRD ST-RPT-1, ticket 10).
 *
 *  Bills, pieces, value, average selling price, average bill value, units per
 *  bill, discount and target achievement, by day, store, brand, category,
 *  salesperson or tender. The figures come from the reporting copy the server
 *  keeps apart from billing, so the page says how fresh that copy is, on what
 *  basis every figure is worked out, and what it could not include. The server
 *  decides which stores, and whether cost, margin and targets are sent at all.
 *  It reads live data only: offline it says it needs a connection. */
export function SalesReportPage() {
  const [filters, setFilters] = useState<SalesFilters>(() => defaultFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: SalesFilters) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    // Half-way through changing both dates the period can run backwards; say
    // so rather than ask the server a question it will refuse.
    if (wanted.date_from && wanted.date_to && wanted.date_to < wanted.date_from) {
      setError("The period ends before it starts. Choose a To date on or after the From date.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await api.get<Payload>("/reports/sales", { params: filterParams(wanted) });
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

  function set<K extends keyof SalesFilters>(key: K, value: SalesFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/sales/export.xlsx", {
        params: filterParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `sales-report-${filters.group_by}-${filters.date_from}-${filters.date_to}.xlsx`;
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
    <p className="warn-note" data-testid="sales-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="sales-retry"
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
    "Bills, pieces, value, average selling price, average bill, units per bill, discount and target, from a copy of the bills kept apart from billing.";

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Sales" lead={lead} />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="sales-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the sales report…</p>
        )}
      </div>
    );
  }

  const columns = columnsFor(data);
  const grouping = GROUPINGS.find((g) => g.key === data.group_by)?.label ?? "Group";

  return (
    <div className="page-pad">
      <PageHeader title="Sales" lead={lead} />
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="sales-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="sales-filter-store"
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
              data-testid="sales-filter-from"
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
              data-testid="sales-filter-to"
              value={filters.date_to}
              disabled={disabled}
              onChange={(event) => set("date_to", event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="sales-export"
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
          data-testid="sales-groupings"
          style={{ flexWrap: "wrap" }}
        >
          {GROUPINGS.map((g) => (
            <button
              key={g.key}
              type="button"
              role="tab"
              aria-selected={filters.group_by === g.key}
              className={`seg-btn ${filters.group_by === g.key ? "active" : ""}`}
              data-testid={`sales-group-${g.key}`}
              disabled={disabled}
              onClick={() => set("group_by", g.key as GroupBy)}
            >
              By {g.label.toLowerCase()}
            </button>
          ))}
        </div>
        <p className="muted-cell" data-testid="sales-as-of">
          {asOfText(data.as_of)}
          {data.as_of &&
            ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {error && (
          <p className="warn-note" data-testid="sales-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="sales-missing">
            <strong>Not included</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`sales-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {data.rows.length === 0 ? (
          <p className="muted-cell" data-testid="sales-empty">
            {busy ? "Loading…" : "No sales in this period at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="sales-table">
              <thead>
                <tr>
                  <th>{grouping}</th>
                  {columns.map((c) => (
                    <th key={c.key} className="num" data-testid={`sales-col-${c.key}`}>
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <ReportRow key={row.key} row={row as Row} columns={columns} />
                ))}
              </tbody>
              <tfoot>
                <ReportRow row={data.total as Row} columns={columns} total />
              </tfoot>
            </table>
          </div>
        )}
        <details className="muted-cell" data-testid="sales-basis">
          <summary>How these figures are worked out (formula {data.formula_version})</summary>
          <ul>
            {data.basis.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      </section>
    </div>
  );
}

function ReportRow({
  row,
  columns,
  total,
}: {
  row: Row;
  columns: ReturnType<typeof columnsFor>;
  total?: boolean;
}) {
  return (
    <tr data-testid={total ? "sales-total" : "sales-row"} data-key={row.key}>
      <td>{total ? <strong>{row.label}</strong> : row.label}</td>
      {columns.map((c) => (
        <td key={c.key} className="num tabular" data-testid={`sales-cell-${c.key}`}>
          {cellText(row[c.key], c.kind)}
        </td>
      ))}
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
