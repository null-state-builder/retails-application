import { useCallback, useEffect, useState } from "react";

import { Note } from "../components/Note";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { UpdatedAgo } from "../components/UpdatedAgo";
import { useCachedGet } from "../lib/cachedQuery";
import { SegmentedControl } from "../components/SegmentedControl";
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
import {
  alignsRight,
  blobErrorMessage,
  columnHeading,
  gstCell,
  maskUnknownFigures,
} from "../lib/gstReport";
import type { FigureSources, GstColumn, GstRow } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["BrandPerformance"]>;

/** The columns worked out from the second copies this report reads: unknown,
 *  not nought, while that copy is missing (`STOCK_NOT_BUILT`,
 *  `COMMISSION_NOT_BUILT`). Sales figures are the main copy's. */
const BRAND_SOURCES: FigureSources = {
  copies: {
    STOCK: [
      "received",
      "sell_through_pct",
      "on_hand",
      "avg_age_days",
      "dead_pieces",
      "ended_pieces",
      "aged_pct",
      "stock_cost_paise",
      "gmroi",
    ],
    COMMISSION: ["commission_paise", "earned_pct"],
  },
};

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
  const [filters, setFilters] = useState<BrandPerformanceFilters>(() =>
    defaultBrandPerformanceFilters(),
  );
  const [exportError, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last export got no answer at all.
  const [exportLost, setLost] = useState(false);
  // Half-way through changing both dates the period can run backwards; say
  // so rather than ask the server a question it will refuse. The last good
  // period stays on screen.
  const backwards = Boolean(
    filters.date_from && filters.date_to && filters.date_to < filters.date_from,
  );
  const [asked, setAsked] = useState(filters);
  if (!backwards && asked !== filters) setAsked(filters);
  // Through the shared cache: a report seen before shows at once and is always
  // read again behind it. Offline, the request waits for the connection.
  const read = useCachedGet<Payload>("/reports/brand-performance", {
    params: brandPerformanceParams(asked),
  });
  useEffect(() => setError(""), [filters]);
  const data = read.data ?? null;
  const busy = read.fetching;
  const lost = exportLost || (read.error != null && isConnectionLost(read.error));
  const readError =
    read.error != null && !isConnectionLost(read.error) ? apiErrorMessage(read.error) : "";
  const error = backwards
    ? "The period ends before it starts. Choose a To date on or after the From date."
    : exportError || readError;
  useEffect(() => {
    if (read.updatedAt) setLost(false);
  }, [read.updatedAt]);

  const { reload } = read;
  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    reload();
  }, [reload]);

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
    <Note
      tone="warn"
      testId="brand-performance-offline"
      role="status"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="brand-performance-retry"
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
  const lead = "How each brand sells, what it earns and how old its stock is, at each store.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Brand Performance" lead={lead} />
        {offlineNote}
        {error ? (
          <Note tone="error" testId="brand-performance-error">
            {error}
          </Note>
        ) : (
          online && !lost && <p>Loading the brand performance report…</p>
        )}
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = (data.rows as unknown as GstRow[]).map((row) =>
    maskUnknownFigures(row, columns, data.missing, BRAND_SOURCES),
  );
  const total = maskUnknownFigures(
    data.total as unknown as GstRow,
    columns,
    data.missing,
    BRAND_SOURCES,
  );
  const estimates = data.estimate_fields;

  return (
    <OperationsPage>
      <PageHeader
        title="Brand Performance"
        lead={lead}
        actions={<UpdatedAgo updatedAt={read.updatedAt} fetching={read.fetching} />}
      />
      {offlineNote}
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
      <SegmentedControl
        label="Group by"
        testId="brand-performance-groupings"
        disabled={disabled}
        value={filters.group_by as BrandPerformanceGroupBy}
        options={BRAND_PERFORMANCE_GROUPINGS.map((v) => ({
          value: v.key as BrandPerformanceGroupBy,
          label: v.label,
          testId: `brand-performance-group-${v.key}`,
        }))}
        onChange={(next) => set("group_by", next)}
      />
      <p className="muted-cell" data-testid="brand-performance-as-of">
        {asOfText(data.as_of)}
        {data.as_of &&
          ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
        {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
      </p>
      {data.estimate_note && (
        <div role="note">
          <Note tone="info" testId="brand-performance-estimate">
            <strong>Estimates.</strong> {data.estimate_note}
          </Note>
        </div>
      )}
      {error && (
        <Note tone="error" testId="brand-performance-error">
          {error}
        </Note>
      )}
      {data.missing.length > 0 && (
        <Note tone="warn" testId="brand-performance-missing">
          <strong>Not included, or to check</strong>
          <ul>
            {data.missing.map((m) => (
              <li key={m.code} data-testid={`brand-performance-missing-${m.code}`}>
                {m.text}
              </li>
            ))}
          </ul>
        </Note>
      )}
      {rows.length === 0 ? (
        <p className="muted-cell" data-testid="brand-performance-empty">
          {busy ? "Loading…" : "Nothing sold, received or in stock in this period at these stores."}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="data freeze-first" data-testid="brand-performance-table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th
                    key={c.key}
                    className={alignsRight(c.kind) ? "num" : undefined}
                    data-testid={`brand-performance-col-${c.key}`}
                    data-estimate={isEstimate(c.key, estimates) ? "true" : undefined}
                  >
                    {columnHeading(c.label)}
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
    </OperationsPage>
  );
}

function BrandPerformanceRow({
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
      data-testid={total ? "brand-performance-total" : "brand-performance-row"}
      data-key={String(row.key ?? "")}
    >
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
