import { useCallback, useEffect, useRef, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { GIFT_VIEWS, defaultGiftFilters, giftParams } from "../lib/giftReport";
import type { GiftFilters, GiftView } from "../lib/giftReport";
import { alignsRight, blobErrorMessage, gstCell } from "../lib/gstReport";
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
    <p className="warn-note" data-testid="gift-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="gift-retry"
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
    <p className="warn-note" data-testid="gift-not-a-filing">
      <strong>Prepared for Accounts, not a filing.</strong>{" "}
      {data?.not_a_filing ?? "Nothing here is posted or filed; the CA checks it first."}
    </p>
  );
  const lead =
    "Pieces given away free as a different item, and the input tax credit to reverse on them, per GSTIN.";

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Gift Stock" lead={lead} />
        {notAFiling}
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="gift-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the gift stock report…</p>
        )}
      </div>
    );
  }

  const columns = data.columns as GstColumn[];
  const rows = data.rows as GstRow[];
  const total = data.total as GstRow;

  return (
    <div className="page-pad">
      <PageHeader title="Gift Stock" lead={lead} />
      {notAFiling}
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="gift-filters">
          <label className="field">
            <span>Store</span>
            <select
              className="input"
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
          <label className="field">
            <span>From</span>
            <input
              className="input"
              type="date"
              data-testid="gift-filter-from"
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
              data-testid="gift-filter-to"
              value={filters.date_to}
              disabled={disabled}
              onChange={(event) => set("date_to", event.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="gift-export"
            disabled={disabled}
            onClick={() => void exportWorkbook()}
          >
            Export to Excel
          </button>
        </div>
        <div className="seg" role="tablist" aria-label="View" data-testid="gift-views">
          {GIFT_VIEWS.map((v) => (
            <button
              key={v.key}
              type="button"
              role="tab"
              aria-selected={filters.view === v.key}
              className={`seg-btn ${filters.view === v.key ? "active" : ""}`}
              data-testid={`gift-view-${v.key}`}
              disabled={disabled}
              onClick={() => set("view", v.key as GiftView)}
            >
              {v.label}
            </button>
          ))}
        </div>
        <p className="muted-cell" data-testid="gift-as-of">
          {asOfText(data.as_of)}
          {data.as_of && ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {error && (
          <p className="warn-note" data-testid="gift-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="gift-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`gift-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {rows.length === 0 ? (
          <p className="muted-cell" data-testid="gift-empty">
            {busy ? "Loading…" : "No gift pieces in this period at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="gift-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={alignsRight(c.kind) ? "num" : undefined}
                      data-testid={`gift-col-${c.key}`}
                    >
                      {c.label}
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
      </section>
    </div>
  );
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
            className={alignsRight(c.kind) ? "num tabular" : undefined}
            data-testid={`gift-cell-${c.key}`}
          >
            {total && index === 0 ? <strong>{text}</strong> : text}
          </td>
        );
      })}
    </tr>
  );
}
