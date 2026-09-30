import { useCallback, useEffect, useRef, useState } from "react";

import { OperationsPage } from "../components/OperationsPage";
import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import {
  GST_VIEWS,
  alignsRight,
  blobErrorMessage,
  defaultGstFilters,
  gstCell,
  gstParams,
} from "../lib/gstReport";
import type { GstColumn, GstFilters, GstRow, GstView } from "../lib/gstReport";
import { asOfText } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["GstReport"]>;

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
    <p className="warn-note" data-testid="gst-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="gst-retry"
            disabled={busy}
            onClick={() => void load(filters)}
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
  const notAFiling = (
    <p className="warn-note" data-testid="gst-not-a-filing">
      <strong>Prepared for filing, not a filing.</strong>{" "}
      {data?.not_a_filing ??
        "Nothing here is sent to the GST portal, and the CA checks it before anything is filed."}
    </p>
  );
  const lead =
    "Outward supplies by rate and HSN, B2B and B2C, credit notes, IRN status, and the documents issued and cancelled for GSTR-1 Table 13.";

  if (!data) {
    return (
      <OperationsPage>
        <PageHeader title="GST" lead={lead} />
        {notAFiling}
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="gst-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the GST report…</p>
        )}
      </OperationsPage>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = data.rows as GstRow[];
  const total = data.total as GstRow;

  return (
    <OperationsPage>
      <PageHeader title="GST" lead={lead} />
      {notAFiling}
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="gst-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
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
          <label className="field">
            <span>From</span>
            <input
              className="input"
              type="date"
              data-testid="gst-filter-from"
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
              data-testid="gst-filter-to"
              value={filters.date_to}
              disabled={disabled}
              onChange={(event) => set("date_to", event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="gst-export"
            disabled={disabled}
            onClick={() => void exportWorkbook()}
          >
            Export to Excel
          </button>
        </div>
        <div
          className="seg"
          role="tablist"
          aria-label="View"
          data-testid="gst-views"
          style={{ flexWrap: "wrap" }}
        >
          {GST_VIEWS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={filters.view === v.key}
              className={`seg-btn ${filters.view === v.key ? "active" : ""}`}
              data-testid={`gst-view-${v.key}`}
              disabled={disabled}
              onClick={() => set("view", v.key as GstView)}
            >
              {v.label}
            </button>
          ))}
        </div>
        <p className="muted-cell" data-testid="gst-as-of">
          {asOfText(data.as_of)}
          {data.as_of &&
            ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {error && (
          <p className="warn-note" data-testid="gst-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="gst-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`gst-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {rows.length === 0 ? (
          <p className="muted-cell" data-testid="gst-empty">
            {busy ? "Loading…" : "Nothing in this view for this period at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="gst-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={alignsRight(c.kind) ? "num" : undefined}
                      data-testid={`gst-col-${c.key}`}
                    >
                      {c.label}
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
      </section>
    </OperationsPage>
  );
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
            className={alignsRight(c.kind) ? "num tabular" : undefined}
            data-testid={`gst-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
    </tr>
  );
}
