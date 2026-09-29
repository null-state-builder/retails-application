import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  BRAND_PERFORMANCE_GROUPINGS,
  brandPerformanceParams,
  defaultBrandPerformanceFilters,
  isEstimate,
} from "../lib/brandPerformance";
import type { BrandPerformanceFilters, BrandPerformanceGroupBy } from "../lib/brandPerformance";
import { alignsRight, blobErrorMessage, gstCell } from "../lib/gstReport";
import type { GstColumn, GstRow } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["BrandPerformance"]>;

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Brand Performance (store operations PRD ST-RPT-3, ticket 45).
 *
 *  Per brand and store - or per brand over every store - sales, discount depth,
 *  returns, sell-through and stock age; for those who may see cost, the margin
 *  (outright) or commission (SOR and concession) and GMROI (outright only),
 *  each labelled an estimate until OQ-50 is decided. The figures come from the
 *  reporting copies the server keeps apart from billing; the server decides which
 *  stores, and sends margin, commission, GMROI and a brand's model only to those
 *  who may see them, so a store role's page and spreadsheet never carry them.
 *  It reads live data only: offline it says it needs a connection, and comes
 *  back by itself when the line does. */
export function BrandPerformanceReportPage() {
  const [filters, setFilters] = useState<BrandPerformanceFilters>(() => defaultBrandPerformanceFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: BrandPerformanceFilters) => {
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
      const response = await api.get<Payload>("/reports/brand-performance", {
        params: brandPerformanceParams(wanted),
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

  function set<K extends keyof BrandPerformanceFilters>(key: K, value: BrandPerformanceFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/brand-performance/export.xlsx", {
        params: brandPerformanceParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `brand-performance-${filters.group_by}-${filters.date_from}-${filters.date_to}.xlsx`;
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
    <p className="warn-note" data-testid="brand-performance-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="brand-performance-retry"
            disabled={busy}
            onClick={() => void load(filters)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const lead = "How each brand sells, what it earns and how old its stock is, at each store.";

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Brand Performance" lead={lead} />
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="brand-performance-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the brand performance report…</p>
        )}
      </div>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = data.rows as unknown as GstRow[];
  const total = data.total as unknown as GstRow;
  const estimates = data.estimate_fields;

  return (
    <div className="page-pad">
      <PageHeader title="Brand Performance" lead={lead} />
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="brand-performance-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="brand-performance-filter-store"
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
              data-testid="brand-performance-filter-from"
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
              data-testid="brand-performance-filter-to"
              value={filters.date_to}
              disabled={disabled}
              onChange={(event) => set("date_to", event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="brand-performance-export"
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
          data-testid="brand-performance-groupings"
          style={{ flexWrap: "wrap" }}
        >
          {BRAND_PERFORMANCE_GROUPINGS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={filters.group_by === v.key}
              className={`seg-btn ${filters.group_by === v.key ? "active" : ""}`}
              data-testid={`brand-performance-group-${v.key}`}
              disabled={disabled}
              onClick={() => set("group_by", v.key as BrandPerformanceGroupBy)}
            >
              {v.label}
            </button>
          ))}
        </div>
        <p className="muted-cell" data-testid="brand-performance-as-of">
          {asOfText(data.as_of)}
          {data.as_of && ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {data.estimate_note && (
          <p className="warn-note" data-testid="brand-performance-estimate" role="note">
            <strong>Estimates.</strong> {data.estimate_note}
          </p>
        )}
        {error && (
          <p className="warn-note" data-testid="brand-performance-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="brand-performance-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`brand-performance-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {rows.length === 0 ? (
          <p className="muted-cell" data-testid="brand-performance-empty">
            {busy ? "Loading…" : "Nothing sold, received or in stock in this period at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="brand-performance-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={alignsRight(c.kind) ? "num" : undefined}
                      data-testid={`brand-performance-col-${c.key}`}
                      data-estimate={isEstimate(c.key, estimates) ? "true" : undefined}
                    >
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <BrandPerformanceRow key={String(row.key ?? index)} row={row} columns={columns} />
                ))}
              </tbody>
              <tfoot>
                <BrandPerformanceRow row={total} columns={columns} total />
              </tfoot>
            </table>
          </div>
        )}
        <details className="muted-cell" data-testid="brand-performance-basis">
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

function BrandPerformanceRow({ row, columns, total }: { row: GstRow; columns: GstColumn[]; total?: boolean }) {
  return (
    <tr data-testid={total ? "brand-performance-total" : "brand-performance-row"} data-key={String(row.key ?? "")}>
      {columns.map((c, index) => {
        const text = gstCell(row[c.key], c.kind);
        return (
          <td
            key={c.key}
            className={alignsRight(c.kind) ? "num tabular" : undefined}
            data-testid={`brand-performance-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
    </tr>
  );
}
