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
import {
  GST_VIEWS,
  alignsRight,
  blobErrorMessage,
  columnHeading,
  defaultGstFilters,
  gstCell,
  gstParams,
  maskUnknownFigures,
} from "../lib/gstReport";
import type { FigureSources, GstColumn, GstFilters, GstRow, GstView } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["GstReport"]>;

/** The rate a by-rate row stands for is its key, not a figure. */
const GST_SOURCES: FigureSources = { keep: ["rate"] };

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > GST (store operations PRD ST-RPT-5, ticket 47).
 *
 *  The outward-supply figures Accounts needs to file GST: by rate and by HSN
 *  with B2B and B2C apart, each B2B invoice with its IRN status, each credit
 *  note, and the documents issued and cancelled in each series for GSTR-1
 *  Table 13, unused offline numbers included. It is prepared for filing and
 *  says so on the page and in the spreadsheet: it is not a filing.
 *
 *  The figures come from the reporting copy the server keeps apart from
 *  billing; the server decides which stores, and only Accounts and Owner may
 *  read it at all. It reads live data only: offline it says it needs a
 *  connection, and comes back by itself when the line does. */
export function GstReportPage() {
  const [filters, setFilters] = useState<GstFilters>(() => defaultGstFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: GstFilters) => {
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
      const response = await api.get<Payload>("/reports/gst", { params: gstParams(wanted) });
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

  function set<K extends keyof GstFilters>(key: K, value: GstFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/gst/export.xlsx", {
        params: gstParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `gst-report-${filters.view}-${filters.date_from}-${filters.date_to}.xlsx`;
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
      testId="gst-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="gst-retry"
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
    <Note tone="error" testId="gst-error">
      {error}
    </Note>
  );
  const notAFiling = (
    <Note tone="warn" testId="gst-not-a-filing">
      <strong>Prepared for filing, not a filing.</strong>{" "}
      {data?.not_a_filing ??
        "Nothing here is sent to the GST portal, and the CA checks it before anything is filed."}
    </Note>
  );
  const lead =
    "Outward supplies by rate and HSN, B2B and B2C, credit notes, IRN status, and the documents issued and cancelled for GSTR-1 Table 13.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="GST" lead={lead} />
        <div className="page">
          {notAFiling}
          {offlineNote}
          {error ? errorNote : online && !lost && <Loading label="Loading the GST report" />}
        </div>
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = (data.rows as GstRow[]).map((row) =>
    maskUnknownFigures(row, columns, data.missing, GST_SOURCES),
  );
  const total = maskUnknownFigures(data.total as GstRow, columns, data.missing, GST_SOURCES);
  const view = GST_VIEWS.find((v) => v.key === filters.view)?.label ?? "By rate";
  const kpis = totalKpis(columns, total, KPI_KEYS, "gst");

  // The period and the store, in the header where the page's own controls go:
  // the filters choose what every figure below is about.
  const filterBar = (
    <div className="page-head-actions" data-testid="gst-filters">
      <label>
        <span className="sr-only">Store</span>
        <select
          className="input"
          style={{ width: "auto" }}
          data-testid="gst-filter-store"
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
          data-testid="gst-filter-from"
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
          data-testid="gst-filter-to"
          value={filters.date_to}
          disabled={disabled}
          onChange={(event) => set("date_to", event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn"
        data-testid="gst-export"
        disabled={disabled}
        onClick={() => void exportWorkbook()}
      >
        <Download size={16} aria-hidden="true" /> Export to Excel
      </button>
    </div>
  );

  return (
    <OperationsPage>
      <PageHeader title="GST" lead={lead} actions={filterBar} />
      <div className="page">
        {notAFiling}
        {offlineNote}
        {errorNote}
        {data.missing.length > 0 && (
          <Note tone="warn" testId="gst-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`gst-missing-${m.code}`}>
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
          heading={view}
          ruled={rows.length > 0 && kpis.length > 0}
          aside={
            <p className="muted-cell" data-testid="gst-as-of" style={{ margin: 0 }}>
              {asOfText(data.as_of)}
              {data.as_of &&
                ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
              {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
            </p>
          }
        >
          {/* The view swaps the whole table, so it is a SegmentedControl. */}
          <SegmentedControl<GstView>
            label="View"
            testId="gst-views"
            value={filters.view as GstView}
            disabled={disabled}
            onChange={(next) => set("view", next)}
            options={GST_VIEWS.map((v) => ({
              value: v.key as GstView,
              label: v.label,
              testId: `gst-view-${v.key}`,
            }))}
          />
          {rows.length === 0 ? (
            <EmptyState testId="gst-empty">
              {busy ? "Loading…" : "Nothing in this view for this period at these stores."}
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="data freeze-first" data-testid="gst-table">
                <thead>
                  <tr>
                    {columns.map((c, index) => (
                      <th
                        key={c.key}
                        scope="col"
                        className={cellClass(c, index) || undefined}
                        data-testid={`gst-col-${c.key}`}
                      >
                        {columnHeading(c.label)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <GstTableRow key={String(row.key ?? index)} row={row} columns={columns} />
                  ))}
                </tbody>
                <tfoot>
                  <GstTableRow row={total} columns={columns} total />
                </tfoot>
              </table>
            </div>
          )}
          <details className="muted-cell" data-testid="gst-basis">
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

/** The totals the strip states, in this order, when the view has them: what
 *  Accounts files for the period. */
const KPI_KEYS = ["taxable_paise", "igst_paise", "cgst_paise", "sgst_paise", "tax_paise"];

/** Column priority (v2 Table): the net figures always show; the invoice and
 *  credit-note amounts behind them, and the IRN itself, come in on wider
 *  screens (the spreadsheet always has every column). */
const PRIORITY: Partial<Record<string, "p2" | "p3">> = {
  invoice_taxable_paise: "p2",
  invoice_tax_paise: "p2",
  credit_taxable_paise: "p2",
  credit_tax_paise: "p2",
  irn: "p3",
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

function GstTableRow({
  row,
  columns,
  total,
}: {
  row: GstRow;
  columns: GstColumn[];
  total?: boolean;
}) {
  return (
    <tr data-testid={total ? "gst-total" : "gst-row"} data-key={String(row.key ?? "")}>
      {columns.map((c, index) => {
        // The total row names itself in its first cell and leaves blank what it
        // does not add up (a rate, a date, a buyer).
        const value = row[c.key];
        const text = total && value === undefined ? "" : gstCell(value, c.kind);
        return (
          <td
            key={c.key}
            className={cellClass(c, index, "num tabular") || undefined}
            data-testid={`gst-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
    </tr>
  );
}
