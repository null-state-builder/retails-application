import { useCallback, useEffect, useRef, useState } from "react";

import { Download } from "lucide-react";

import { KpiStrip } from "../components/KpiStrip";
import type { Kpi } from "../components/KpiStrip";
import { Note } from "../components/Note";
import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { Section } from "../components/Section";
import { SegmentedControl } from "../components/SegmentedControl";
import { EmptyState, Loading } from "../components/States";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { alignsRight, columnHeading, gstCell, maskUnknownFigures } from "../lib/gstReport";
import type { FigureSources, GstColumn, GstRow } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import {
  SHRINKAGE_GROUPINGS,
  defaultShrinkageFilters,
  shrinkageParams,
} from "../lib/shrinkageReport";
import type { ShrinkageFilters, ShrinkageGroupBy } from "../lib/shrinkageReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["ShrinkageReport"]>;

/** The shares worked out against the sales copy this report also reads:
 *  unknown, not nought, while that copy is missing (`SALES_NOT_BUILT`). */
const SHRINKAGE_SOURCES: FigureSources = {
  copies: { SALES: ["pieces_sold", "pieces_pct", "sales_paise", "cost_share_pct"] },
};

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Shrinkage (store operations PRD ST-INV-4, ticket 44).
 *
 *  Pieces and cost lost to shrinkage - approved shrinkage adjustments and
 *  write-offs typed as shrinkage - by store, brand, category or month, as a
 *  share of sales. The figures come from the reporting copies the server keeps
 *  apart from billing and receiving; the server decides which stores, and
 *  sends cost only to those who may see it, so a store role's page and
 *  spreadsheet show pieces alone. It reads live data only: offline it says it
 *  needs a connection, and comes back by itself when the line does. */
export function ShrinkageReportPage() {
  const [filters, setFilters] = useState<ShrinkageFilters>(() => defaultShrinkageFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: ShrinkageFilters) => {
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
      const response = await api.get<Payload>("/reports/shrinkage", {
        params: shrinkageParams(wanted),
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

  function set<K extends keyof ShrinkageFilters>(key: K, value: ShrinkageFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/shrinkage/export.xlsx", {
        params: shrinkageParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `shrinkage-report-${filters.group_by}-${filters.date_from}-${filters.date_to}.xlsx`;
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
      testId="shrinkage-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="shrinkage-retry"
            disabled={busy}
            onClick={() => void load(filters)}
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
    <Note tone="error" testId="shrinkage-error">
      {error}
    </Note>
  );
  const lead =
    "Pieces lost to approved shrinkage adjustments and write-offs typed as shrinkage, by store, brand, category or month, as a share of sales.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Shrinkage" lead={lead} />
        <div className="page">
          {offlineNote}
          {error ? errorNote : online && !lost && <Loading label="Loading the shrinkage report" />}
        </div>
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = (data.rows as unknown as GstRow[]).map((row) =>
    maskUnknownFigures(row, columns, data.missing, SHRINKAGE_SOURCES),
  );
  const total = maskUnknownFigures(
    data.total as unknown as GstRow,
    columns,
    data.missing,
    SHRINKAGE_SOURCES,
  );
  const grouping = SHRINKAGE_GROUPINGS.find((g) => g.key === filters.group_by)?.label ?? "Store";
  const kpis = totalKpis(columns, total, KPI_KEYS, "shrinkage");

  // The period and the store, in the header where the page's own controls go:
  // the filters choose what every figure below is about.
  const filterBar = (
    <div className="page-head-actions" data-testid="shrinkage-filters">
      <label>
        <span className="sr-only">Store</span>
        <select
          className="input"
          style={{ width: "auto" }}
          data-testid="shrinkage-filter-store"
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
          data-testid="shrinkage-filter-from"
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
          data-testid="shrinkage-filter-to"
          value={filters.date_to}
          disabled={disabled}
          onChange={(event) => set("date_to", event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn"
        data-testid="shrinkage-export"
        disabled={disabled}
        onClick={() => void exportWorkbook()}
      >
        <Download size={16} aria-hidden="true" /> Export to Excel
      </button>
    </div>
  );

  return (
    <OperationsPage>
      <PageHeader title="Shrinkage" lead={lead} actions={filterBar} />
      <div className="page">
        {offlineNote}
        {errorNote}
        {data.missing.length > 0 && (
          <Note tone="warn" testId="shrinkage-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`shrinkage-missing-${m.code}`}>
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
            <p className="muted-cell" data-testid="shrinkage-as-of" style={{ margin: 0 }}>
              {asOfText(data.as_of)}
              {data.as_of &&
                ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
              {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
            </p>
          }
        >
          {/* The grouping swaps the whole table, so it is a SegmentedControl. */}
          <SegmentedControl<ShrinkageGroupBy>
            label="Group by"
            testId="shrinkage-groupings"
            value={filters.group_by as ShrinkageGroupBy}
            disabled={disabled}
            onChange={(value) => set("group_by", value)}
            options={SHRINKAGE_GROUPINGS.map((v) => ({
              value: v.key as ShrinkageGroupBy,
              label: v.label,
              testId: `shrinkage-group-${v.key}`,
            }))}
          />
          {rows.length === 0 ? (
            <EmptyState testId="shrinkage-empty">
              {busy ? "Loading…" : "Nothing sold or lost in this period at these stores."}
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="data freeze-first" data-testid="shrinkage-table">
                <thead>
                  <tr>
                    {columns.map((c, index) => (
                      <th
                        key={c.key}
                        scope="col"
                        className={cellClass(c, index) || undefined}
                        data-testid={`shrinkage-col-${c.key}`}
                      >
                        {columnHeading(c.label)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <ShrinkageTableRow key={String(row.key ?? index)} row={row} columns={columns} />
                  ))}
                </tbody>
                <tfoot>
                  <ShrinkageTableRow row={total} columns={columns} total />
                </tfoot>
              </table>
            </div>
          )}
          <details className="muted-cell" data-testid="shrinkage-basis">
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

/** The totals the strip states, in this order, when the view has them. Cost
 *  is sent only to those who may see it. */
const KPI_KEYS = ["pieces_lost", "pieces_sold", "pieces_pct", "cost_paise", "cost_share_pct"];

/** Column priority (v2 Table): a short table, so every column always shows. */
const PRIORITY: Partial<Record<string, "p2" | "p3">> = {};

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

function ShrinkageTableRow({
  row,
  columns,
  total,
}: {
  row: GstRow;
  columns: GstColumn[];
  total?: boolean;
}) {
  return (
    <tr data-testid={total ? "shrinkage-total" : "shrinkage-row"} data-key={String(row.key ?? "")}>
      {columns.map((c, index) => {
        const text = gstCell(row[c.key], c.kind);
        return (
          <td
            key={c.key}
            className={cellClass(c, index, "num tabular") || undefined}
            data-testid={`shrinkage-cell-${c.key}`}
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
