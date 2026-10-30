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
    <Note
      tone="warn"
      role="status"
      testId="exceptions-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="exceptions-retry"
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
    <Note tone="error" testId="exceptions-error">
      {error}
    </Note>
  );
  const lead =
    "Cancelled bills, manual discounts, manager PIN uses, cash variances, missing bill numbers, returns without a bill and late syncs, by store and staff member.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Exceptions" lead={lead} />
        <div className="page">
          {offlineNote}
          {error ? errorNote : online && !lost && <Loading label="Loading the exceptions report" />}
        </div>
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = (data.rows as unknown as GstRow[]).map((row) =>
    maskUnknownFigures(row, columns, data.missing),
  );
  const total = maskUnknownFigures(data.total as unknown as GstRow, columns, data.missing);
  const grouping =
    EXCEPTIONS_GROUPINGS.find((g) => g.key === filters.group_by)?.label ?? "Exceptions";
  const kpis = totalKpis(columns, total, KPI_KEYS, "exceptions");

  // The period and the store, in the header where the page's own controls go:
  // the filters choose what every figure below is about.
  const filterBar = (
    <div className="page-head-actions" data-testid="exceptions-filters">
      <label>
        <span className="sr-only">Store</span>
        <select
          className="input"
          style={{ width: "auto" }}
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
      <label>
        <span className="sr-only">From</span>
        <input
          className="input"
          type="date"
          style={{ width: "auto" }}
          data-testid="exceptions-filter-from"
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
          data-testid="exceptions-filter-to"
          value={filters.date_to}
          disabled={disabled}
          onChange={(event) => set("date_to", event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn"
        data-testid="exceptions-export"
        disabled={disabled}
        onClick={() => void exportWorkbook()}
      >
        <Download size={16} aria-hidden="true" /> Export to Excel
      </button>
    </div>
  );

  return (
    <OperationsPage>
      <PageHeader title="Exceptions" lead={lead} actions={filterBar} />
      <div className="page">
        {offlineNote}
        {errorNote}
        {data.missing.length > 0 && (
          <Note tone="warn" testId="exceptions-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`exceptions-missing-${m.code}`}>
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
          heading={grouping}
          ruled={rows.length > 0 && kpis.length > 0}
          aside={
            <p className="muted-cell" data-testid="exceptions-as-of" style={{ margin: 0 }}>
              {asOfText(data.as_of)}
              {data.as_of &&
                ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
              {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
            </p>
          }
        >
          {/* The grouping swaps the whole table, so it is a SegmentedControl. */}
          <SegmentedControl<ExceptionsGroupBy>
            label="Group by"
            testId="exceptions-groupings"
            value={filters.group_by as ExceptionsGroupBy}
            disabled={disabled}
            onChange={(value) => set("group_by", value)}
            options={EXCEPTIONS_GROUPINGS.map((v) => ({
              value: v.key as ExceptionsGroupBy,
              label: v.label,
              testId: `exceptions-group-${v.key}`,
            }))}
          />
          {rows.length === 0 ? (
            <EmptyState testId="exceptions-empty">
              {busy ? "Loading…" : "No exceptions in this period at these stores."}
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="data freeze-first" data-testid="exceptions-table">
                <thead>
                  <tr>
                    {columns.map((c, index) => (
                      <th
                        key={c.key}
                        scope="col"
                        className={cellClass(c, index) || undefined}
                        data-testid={`exceptions-col-${c.key}`}
                      >
                        {columnHeading(c.label)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <ExceptionsTableRow
                      key={String(row.key ?? index)}
                      row={row}
                      columns={columns}
                    />
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
        </Section>
      </div>
    </OperationsPage>
  );
}

/** The totals the strip states, in this order, when the view has them (at most
 *  five show). Each one-by-one view's own count and value come last. */
const KPI_KEYS = [
  "cancelled_bills",
  "manual_discounts",
  "pin_uses",
  "cash_variance_paise",
  "number_holes",
  "events",
  "value_paise",
];

/** Column priority (v2 Table): the counts always show; the amounts behind
 *  them come in on wider screens (the spreadsheet always has every column). */
const PRIORITY: Partial<Record<string, "p2" | "p3">> = {
  cancelled_paise: "p2",
  price_typed_paise: "p2",
  manual_discount_paise: "p2",
  no_bill_return_paise: "p2",
  no_bill_caps: "p3",
  checklist_missed: "p3",
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
            className={cellClass(c, index, "num tabular") || undefined}
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
