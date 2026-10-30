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
import { GIFT_VIEWS, defaultGiftFilters, giftParams } from "../lib/giftReport";
import type { GiftFilters, GiftView } from "../lib/giftReport";
import {
  alignsRight,
  blobErrorMessage,
  columnHeading,
  gstCell,
  maskUnknownFigures,
} from "../lib/gstReport";
import type { GstColumn, GstRow } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["GiftReport"]>;

const OFFLINE = "Reports need a connection. Reconnect to see or export this report.";

/** Reports > Gift Stock (store operations PRD ST-CMP-7, ticket 14).
 *
 *  Pieces given away free as a different item (a gift with purchase) are
 *  tagged as gifts when they leave stock; this lists them each month with the
 *  input tax credit Accounts reverses on them, per GSTIN. Buy 2 get 1 pieces
 *  are part of the sale and never appear.
 *
 *  The figures come from the reporting copy the server keeps apart from
 *  billing, and the server decides which stores and whether cost and credit
 *  are sent at all: only Accounts and Owner receive them. It reads live data
 *  only: offline it says it needs a connection, and comes back by itself. */
export function GiftStockReportPage() {
  const [filters, setFilters] = useState<GiftFilters>(() => defaultGiftFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);

  const load = useCallback(async (wanted: GiftFilters) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    if (wanted.date_from && wanted.date_to && wanted.date_to < wanted.date_from) {
      // Checked before the request count moves, so a request still in flight
      // clears the busy flag it set and the dates can be put right.
      setError("The period ends before it starts. Choose a To date on or after the From date.");
      return;
    }
    const mine = ++request.current;
    setBusy(true);
    setError("");
    try {
      const response = await api.get<Payload>("/reports/gift-stock", {
        params: giftParams(wanted),
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

  function set<K extends keyof GiftFilters>(key: K, value: GiftFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/gift-stock/export.xlsx", {
        params: giftParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = `gift-stock-${filters.view}-${filters.date_from}-${filters.date_to}.xlsx`;
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
      testId="gift-offline"
      action={
        online && (
          <button
            type="button"
            className="btn btn-sm"
            data-testid="gift-retry"
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
    <Note tone="error" testId="gift-error">
      {error}
    </Note>
  );
  const notAFiling = (
    <Note tone="warn" testId="gift-not-a-filing">
      <strong>Prepared for Accounts, not a filing.</strong>{" "}
      {data?.not_a_filing ?? "Nothing here is posted or filed; the CA checks it first."}
    </Note>
  );
  const lead =
    "Pieces given away free as a different item, and the input tax credit to reverse on them, per GSTIN.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="Gift Stock" lead={lead} />
        <div className="page">
          {notAFiling}
          {offlineNote}
          {error ? errorNote : online && !lost && <Loading label="Loading the gift stock report" />}
        </div>
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = (data.rows as GstRow[]).map((row) => maskUnknownFigures(row, columns, data.missing));
  const total = maskUnknownFigures(data.total as GstRow, columns, data.missing);
  const view = GIFT_VIEWS.find((v) => v.key === filters.view)?.label ?? "By GSTIN";
  const kpis = totalKpis(columns, total, KPI_KEYS, "gift");

  // The period and the store, in the header where the page's own controls go:
  // the filters choose what every figure below is about.
  const filterBar = (
    <div className="page-head-actions" data-testid="gift-filters">
      <label>
        <span className="sr-only">Store</span>
        <select
          className="input"
          style={{ width: "auto" }}
          data-testid="gift-filter-store"
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
          data-testid="gift-filter-from"
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
          data-testid="gift-filter-to"
          value={filters.date_to}
          disabled={disabled}
          onChange={(event) => set("date_to", event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn"
        data-testid="gift-export"
        disabled={disabled}
        onClick={() => void exportWorkbook()}
      >
        <Download size={16} aria-hidden="true" /> Export to Excel
      </button>
    </div>
  );

  return (
    <OperationsPage>
      <PageHeader title="Gift Stock" lead={lead} actions={filterBar} />
      <div className="page">
        {notAFiling}
        {offlineNote}
        {errorNote}
        {data.missing.length > 0 && (
          <Note tone="warn" testId="gift-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`gift-missing-${m.code}`}>
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
            <p className="muted-cell" data-testid="gift-as-of" style={{ margin: 0 }}>
              {asOfText(data.as_of)}
              {data.as_of &&
                ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
              {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
            </p>
          }
        >
          {/* The view swaps the whole table, so it is a SegmentedControl. */}
          <SegmentedControl<GiftView>
            label="View"
            testId="gift-views"
            value={filters.view as GiftView}
            disabled={disabled}
            onChange={(next) => set("view", next)}
            options={GIFT_VIEWS.map((v) => ({
              value: v.key as GiftView,
              label: v.label,
              testId: `gift-view-${v.key}`,
            }))}
          />
          {rows.length === 0 ? (
            <EmptyState testId="gift-empty">
              {busy ? "Loading…" : "No gift pieces in this period at these stores."}
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="data freeze-first" data-testid="gift-table">
                <thead>
                  <tr>
                    {columns.map((c, index) => (
                      <th
                        key={c.key}
                        scope="col"
                        className={cellClass(c, index) || undefined}
                        data-testid={`gift-col-${c.key}`}
                      >
                        {columnHeading(c.label)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <GiftTableRow key={String(row.key ?? index)} row={row} columns={columns} />
                  ))}
                </tbody>
                <tfoot>
                  <GiftTableRow row={total} columns={columns} total />
                </tfoot>
              </table>
            </div>
          )}
          <details className="muted-cell" data-testid="gift-basis">
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
const KPI_KEYS = ["pieces", "bills", "returned", "cost_paise", "itc_paise"];

/** Column priority (v2 Table): what identifies a piece and what it costs
 *  always show; its codes and offer come in on wider screens (the spreadsheet
 *  always has every column). */
const PRIORITY: Partial<Record<string, "p2" | "p3">> = {
  barcode: "p2",
  hsn: "p2",
  input_tax_pct: "p2",
  offer_name: "p3",
  pieces_unknown: "p3",
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

function GiftTableRow({
  row,
  columns,
  total,
}: {
  row: GstRow;
  columns: GstColumn[];
  total?: boolean;
}) {
  return (
    <tr data-testid={total ? "gift-total" : "gift-row"} data-key={String(row.key ?? "")}>
      {columns.map((c, index) => {
        // The total row names itself in its first cell and leaves blank what it
        // does not add up (a date, a barcode, a rate).
        const value = row[c.key];
        const text = total && value === undefined ? "" : gstCell(value, c.kind);
        return (
          <td
            key={c.key}
            className={cellClass(c, index, "num tabular") || undefined}
            data-testid={`gift-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
    </tr>
  );
}
