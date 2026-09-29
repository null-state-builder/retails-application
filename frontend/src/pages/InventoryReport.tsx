import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { alignsRight, gstCell } from "../lib/gstReport";
import type { GstColumn, GstRow } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import {
  INVENTORY_GROUPINGS,
  INVENTORY_SPANS,
  defaultInventoryFilters,
  inventoryParams,
} from "../lib/inventoryReport";
import type { InventoryFilters, InventoryGroupBy, InventorySpan } from "../lib/inventoryReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["InventoryReport"]>;

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Inventory (store operations PRD ST-RPT-2, ticket 43).
 *
 *  Sell-through, weeks of cover, GMROI (outright brands only) and in-season
 *  dead stock, by store, brand, category or season; sell-through over the
 *  chosen dates or each season to date. The figures come from the reporting
 *  copies the server keeps apart from billing and receiving; the server decides
 *  which stores, and sends cost, margin, GMROI and a brand's model only to those
 *  who may see them, so a store role's page and spreadsheet show pieces alone.
 *  It reads live data only: offline it says it needs a connection, and comes
 *  back by itself when the line does. */
export function InventoryReportPage() {
  const [filters, setFilters] = useState<InventoryFilters>(() => defaultInventoryFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: InventoryFilters) => {
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
      const response = await api.get<Payload>("/reports/inventory", {
        params: inventoryParams(wanted),
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

  function set<K extends keyof InventoryFilters>(key: K, value: InventoryFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/inventory/export.xlsx", {
        params: inventoryParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `inventory-report-${filters.group_by}-${filters.date_from}-${filters.date_to}.xlsx`;
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
    <p className="warn-note" data-testid="inventory-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="inventory-retry"
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
    "Sell-through, weeks of cover, GMROI and dead stock, by store, brand, category or season.";

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Inventory" lead={lead} />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="inventory-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the inventory report…</p>
        )}
      </div>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = data.rows as unknown as GstRow[];
  const total = data.total as unknown as GstRow;

  return (
    <div className="page-pad">
      <PageHeader title="Inventory" lead={lead} />
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="inventory-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="inventory-filter-store"
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
              data-testid="inventory-filter-from"
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
              data-testid="inventory-filter-to"
              value={filters.date_to}
              disabled={disabled}
              onChange={(event) => set("date_to", event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="inventory-export"
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
          data-testid="inventory-groupings"
          style={{ flexWrap: "wrap" }}
        >
          {INVENTORY_GROUPINGS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={filters.group_by === v.key}
              className={`seg-btn ${filters.group_by === v.key ? "active" : ""}`}
              data-testid={`inventory-group-${v.key}`}
              disabled={disabled}
              onClick={() => set("group_by", v.key as InventoryGroupBy)}
            >
              {v.label}
            </button>
          ))}
        </div>
        <div
          className="seg"
          role="tablist"
          aria-label="Sell-through over"
          data-testid="inventory-spans"
          style={{ flexWrap: "wrap" }}
        >
          <span className="muted-cell">Sell-through over:</span>
          {INVENTORY_SPANS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={filters.span === v.key}
              className={`seg-btn ${filters.span === v.key ? "active" : ""}`}
              data-testid={`inventory-span-${v.key}`}
              disabled={disabled}
              onClick={() => set("span", v.key as InventorySpan)}
            >
              {v.label}
            </button>
          ))}
        </div>
        <p className="muted-cell" data-testid="inventory-as-of">
          {asOfText(data.as_of)}
          {data.as_of && ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {error && (
          <p className="warn-note" data-testid="inventory-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="inventory-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`inventory-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {rows.length === 0 ? (
          <p className="muted-cell" data-testid="inventory-empty">
            {busy ? "Loading…" : "Nothing sold, received or in stock in this period at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="inventory-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={alignsRight(c.kind) ? "num" : undefined}
                      data-testid={`inventory-col-${c.key}`}
                    >
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <InventoryTableRow key={String(row.key ?? index)} row={row} columns={columns} />
                ))}
              </tbody>
              <tfoot>
                <InventoryTableRow row={total} columns={columns} total />
              </tfoot>
            </table>
          </div>
        )}
        <details className="muted-cell" data-testid="inventory-basis">
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

function InventoryTableRow({
  row,
  columns,
  total,
}: {
  row: GstRow;
  columns: GstColumn[];
  total?: boolean;
}) {
  return (
    <tr data-testid={total ? "inventory-total" : "inventory-row"} data-key={String(row.key ?? "")}>
      {columns.map((c, index) => {
        const text = gstCell(row[c.key], c.kind);
        return (
          <td
            key={c.key}
            className={alignsRight(c.kind) ? "num tabular" : undefined}
            data-testid={`inventory-cell-${c.key}`}
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
