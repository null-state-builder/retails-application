import { useCallback, useEffect, useState } from "react";

import { Download } from "lucide-react";

import { KpiStrip } from "../components/KpiStrip";
import type { Kpi } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { Section } from "../components/Section";
import { SegmentedControl } from "../components/SegmentedControl";
import { EmptyState, Loading } from "../components/States";
import { UpdatedAgo } from "../components/UpdatedAgo";
import { useCachedGet } from "../lib/cachedQuery";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { alignsRight, columnHeading, gstCell, maskUnknownFigures } from "../lib/gstReport";
import type { FigureSources, GstColumn, GstRow } from "../lib/gstReport";
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

/** The columns worked out from the sales copy this report also reads: they are
 *  unknown, not nought, while that copy is missing (`SALES_NOT_BUILT`). */
const INVENTORY_SOURCES: FigureSources = {
  copies: {
    SALES: [
      "sold",
      "sell_through_pct",
      "weekly_sales",
      "weeks_cover",
      "margin_paise",
      "margin_pct",
      "gmroi",
    ],
  },
};

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
  const [online, setOnline] = useState(navigator.onLine);
  // What the last export said, kept apart from the report read's own answer.
  const [exportError, setExportError] = useState("");
  const [exportLost, setExportLost] = useState(false);
  const backwards = !!(filters.date_from && filters.date_to && filters.date_to < filters.date_from);

  // Through the shared cache (a report: always asked again on sight, behind
  // what was last shown). The previous filters' report stays up while new
  // filters load. Nothing is asked offline or for a period that runs backwards.
  const read = useCachedGet<Payload>("/reports/inventory", {
    params: inventoryParams(filters),
    enabled: online && !backwards,
  });
  const data = read.data ?? null;
  const readLost = !!read.error && isConnectionLost(read.error);
  // Half-way through changing both dates the period can run backwards; say so
  // rather than ask the server a question it will refuse.
  const error = backwards
    ? "The period ends before it starts. Choose a To date on or after the From date."
    : exportError || (read.error && !readLost ? apiErrorMessage(read.error) : "");
  // The browser says it is online, but the last request got no answer at all.
  const lost = readLost || exportLost;
  const busy = read.fetching;
  const { reload } = read;

  useEffect(() => {
    if (!read.fetching && read.data !== undefined && !read.error) setExportLost(false);
  }, [read.fetching, read.data, read.error]);

  const load = useCallback(() => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    reload();
  }, [reload]);

  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);

  function set<K extends keyof InventoryFilters>(key: K, value: InventoryFilters[K]) {
    setExportError("");
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setExportError("");
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
        setExportLost(true);
        return;
      }
      setExportError(await blobErrorMessage(reason));
    }
  }

  const disabled = !online || busy;
  const offlineNote = (!online || lost) && (
    <Note
      tone="warn"
      role="status"
      testId="inventory-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="inventory-retry"
            disabled={busy}
            onClick={load}
          >
            Try again
          </button>
        )
      }
    >
      {OFFLINE}
    </Note>
  );
  const errorNote = error && (
    <Note tone="error" testId="inventory-error">
      {error}
    </Note>
  );
  const lead =
    "Sell-through, weeks of cover, GMROI and dead stock, by store, brand, category or season.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Inventory" lead={lead} />
        <div className="page">
          {offlineNote}
          {error ? errorNote : online && !lost && <Loading label="Loading the inventory report" />}
        </div>
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = (data.rows as unknown as GstRow[]).map((row) =>
    maskUnknownFigures(row, columns, data.missing, INVENTORY_SOURCES),
  );
  const total = maskUnknownFigures(
    data.total as unknown as GstRow,
    columns,
    data.missing,
    INVENTORY_SOURCES,
  );
  const grouping = INVENTORY_GROUPINGS.find((g) => g.key === filters.group_by)?.label ?? "Store";
  const kpis = totalKpis(columns, total, KPI_KEYS, "inventory");

  // The period and the store, in the header where the page's own controls go:
  // the filters choose what every figure below is about.
  const filterBar = (
    <div className="page-head-actions" data-testid="inventory-filters">
      <label>
        <span className="sr-only">Store</span>
        <select
          className="input"
          style={{ width: "auto" }}
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
      <label>
        <span className="sr-only">From</span>
        <input
          className="input"
          type="date"
          style={{ width: "auto" }}
          data-testid="inventory-filter-from"
          value={filters.date_from}
          disabled={disabled}
          onChange={(event) => set("date_from", event.target.value)}
        />
      </label>
      <span className="muted-cell" aria-hidden="true">
        to
      </span>
      <label>
        <span className="sr-only">To</span>
        <input
          className="input"
          type="date"
          style={{ width: "auto" }}
          data-testid="inventory-filter-to"
          value={filters.date_to}
          disabled={disabled}
          onChange={(event) => set("date_to", event.target.value)}
        />
      </label>
      {/* What sell-through is worked out over: a filter on what the figures
          mean, beside the dates, not a second way of grouping the table. */}
      <span className="muted-cell" aria-hidden="true">
        Sell-through over
      </span>
      <SegmentedControl<InventorySpan>
        label="Sell-through over"
        testId="inventory-spans"
        value={filters.span as InventorySpan}
        disabled={disabled}
        onChange={(value) => set("span", value)}
        options={INVENTORY_SPANS.map((v) => ({
          value: v.key as InventorySpan,
          label: v.label,
          testId: `inventory-span-${v.key}`,
        }))}
      />
      <button
        type="button"
        className="btn"
        data-testid="inventory-export"
        disabled={disabled}
        onClick={() => void exportWorkbook()}
      >
        <Download size={16} aria-hidden="true" /> Export to Excel
      </button>
    </div>
  );

  return (
    <OperationsPage>
      <PageHeader
        title="Inventory"
        lead={lead}
        actions={
          <>
            <UpdatedAgo updatedAt={read.updatedAt} fetching={read.fetching} />
            {filterBar}
          </>
        }
      />
      <div className="page">
        {offlineNote}
        {errorNote}
        {data.missing.length > 0 && (
          <Note tone="warn" testId="inventory-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`inventory-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </Note>
        )}
        {rows.length > 0 && kpis.length > 0 && (
          <KpiStrip items={kpis} label="The period in figures" />
        )}

        <Section
          heading={`By ${grouping.toLowerCase()}`}
          ruled={rows.length > 0 && kpis.length > 0}
          aside={
            <p className="muted-cell" data-testid="inventory-as-of" style={{ margin: 0 }}>
              {asOfText(data.as_of)}
              {data.as_of &&
                ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
              {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
            </p>
          }
        >
          {/* The grouping swaps the whole table, so it is a SegmentedControl. */}
          <SegmentedControl<InventoryGroupBy>
            label="Group by"
            testId="inventory-groupings"
            value={filters.group_by as InventoryGroupBy}
            disabled={disabled}
            onChange={(value) => set("group_by", value)}
            options={INVENTORY_GROUPINGS.map((v) => ({
              value: v.key as InventoryGroupBy,
              label: v.label,
              testId: `inventory-group-${v.key}`,
            }))}
          />
          {rows.length === 0 ? (
            <EmptyState testId="inventory-empty">
              {busy
                ? "Loading…"
                : "Nothing sold, received or in stock in this period at these stores."}
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="data freeze-first" data-testid="inventory-table">
                <thead>
                  <tr>
                    {columns.map((c, index) => (
                      <th
                        key={c.key}
                        scope="col"
                        className={cellClass(c, index) || undefined}
                        data-testid={`inventory-col-${c.key}`}
                      >
                        {columnHeading(c.label)}
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
        </Section>
      </div>
    </OperationsPage>
  );
}

/** The totals the strip states, in this order, when the view has them. */
const KPI_KEYS = ["sold", "received", "sell_through_pct", "on_hand", "dead_pieces"];

/** Column priority (v2 Table): sell-through, cover and dead pieces always
 *  show; the rates and amounts behind them come in on wider screens (the
 *  spreadsheet always has every column). */
const PRIORITY: Partial<Record<string, "p2" | "p3">> = {
  model: "p3",
  weekly_sales: "p2",
  dead_pct: "p2",
  stock_cost_paise: "p2",
  dead_cost_paise: "p2",
  gmroi: "p3",
};

/** A cell's classes: figures right-aligned; the first column never hides. */
function cellClass(c: GstColumn, index: number, figure = "num"): string {
  return [alignsRight(c.kind) ? figure : "", index > 0 ? (PRIORITY[c.key] ?? "") : ""]
    .filter(Boolean)
    .join(" ");
}

/** The total row's figures as a KpiStrip, from what the table already holds. */
function totalKpis(
  columns: GstColumn[],
  total: GstRow,
  keys: readonly string[],
  prefix: string,
): Kpi[] {
  return keys
    .map((key) => columns.find((c) => c.key === key))
    .filter((c): c is GstColumn => c !== undefined && total[c.key] !== undefined)
    .map((c) => ({
      label: columnHeading(c.label),
      value: gstCell(total[c.key], c.kind),
      testId: `${prefix}-kpi-${c.key}`,
    }));
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
            className={cellClass(c, index, "num tabular") || undefined}
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
