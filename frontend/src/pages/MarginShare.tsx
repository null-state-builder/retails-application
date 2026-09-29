import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import type { ApiRead, ApiSchemas } from "../lib/api";
import { isConnectionLost } from "../lib/auditLog";
import { alignsRight, blobErrorMessage, gstCell } from "../lib/gstReport";
import type { CellKind } from "../lib/gstReport";
import { brandIdOf, defaultMarginFilters, exportName, marginParams } from "../lib/marginShare";
import type { MarginFilters } from "../lib/marginShare";
import { asOfText } from "../lib/salesReport";
import "./Shared.css";

type Payload = ApiRead<ApiSchemas["MarginShareReport"]>;
type Row = Payload["rows"][number];
type Column = { key: string; label: string; kind: CellKind };

const OFFLINE = "Reports need a connection. Reconnect to see or export this statement.";
const ESTIMATE =
  "Estimate. How a sale divides between a brand and KDPS is not final until OQ-50 is decided. Nothing here is posted or owed.";

/** Brands > Margin Share (store operations PRD ST-BRD-4, ticket 27).
 *
 *  For SOR and concession brands, how a month's sales divided between the brand
 *  and KDPS: every brand at once, or one brand's statement line by line, with
 *  an Excel export of either. Each sale's split was worked out when its bill
 *  reached head office, from the margin in the brand terms in force on the bill
 *  date; a brand whose model or margin is unknown is listed, never split.
 *
 *  Labelled an estimate throughout until OQ-50 is decided. The figures come
 *  from the reporting copy the server keeps apart from billing; the server
 *  decides which stores, and only Accounts and Owner may read it at all. It
 *  reads live data only: offline it says it needs a connection, and comes back
 *  by itself when the line does. */
export function MarginSharePage() {
  const [filters, setFilters] = useState<MarginFilters>(() => defaultMarginFilters());
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  // The browser says it is online, but the last request got no answer at all.
  const [lost, setLost] = useState(false);
  const [busy, setBusy] = useState(false);
  // The brands the last every-brand statement named, so one can be picked.
  const [brands, setBrands] = useState<{ id: string; name: string }[]>([]);
  const request = useRef(0);

  const load = useCallback(async (wanted: MarginFilters) => {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    const mine = ++request.current;
    setBusy(true);
    setError("");
    try {
      const response = await api.get<Payload>("/reports/margin-share", { params: marginParams(wanted) });
      if (mine !== request.current) return;
      setLost(false);
      setData(response.data);
      if (!wanted.brand) {
        setBrands(
          response.data.rows.flatMap((row) => {
            const id = brandIdOf(row.key);
            return id ? [{ id, name: row.label ?? "" }] : [];
          }),
        );
      }
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

  function set<K extends keyof MarginFilters>(key: K, value: MarginFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function exportWorkbook() {
    if (!navigator.onLine) {
      setOnline(false);
      return;
    }
    setError("");
    try {
      const response = await api.get<Blob>("/reports/margin-share/export.xlsx", {
        params: marginParams(filters),
        responseType: "blob",
      });
      const href = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = href;
      link.download = exportName(filters, data?.brand?.name ?? null);
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
    <p className="warn-note" data-testid="margin-offline" role="status">
      {OFFLINE}
      {online && (
        <>
          {" "}
          <button
            type="button"
            className="btn"
            data-testid="margin-retry"
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
    "For SOR and concession brands: each month's sales, split between the brand and KDPS by the brand's margin. A brand whose model or margin is unknown is listed, not split.";
  const estimate = (
    <p className="warn-note" data-testid="margin-estimate" role="note">
      <strong>{ESTIMATE}</strong>
    </p>
  );

  if (!data) {
    return (
      <div className="page-pad">
        <PageHeader title="Margin Share" lead={lead} />
        {estimate}
        {offlineNote}
        {error ? (
          <p className="warn-note" data-testid="margin-error">
            {error}
          </p>
        ) : (
          online && !lost && <p>Loading the margin share statement…</p>
        )}
      </div>
    );
  }

  const columns = data.columns as Column[];
  const oneBrand = data.brand;

  return (
    <div className="page-pad">
      <PageHeader title="Margin Share" lead={lead} />
      {estimate}
      {offlineNote}
      <section className="card section-card">
        <div className="toolbar" data-testid="margin-filters">
          <label className="field">
            <span>Month</span>
            <input
              className="input"
              type="month"
              data-testid="margin-filter-month"
              value={filters.month}
              disabled={disabled}
              onChange={(event) => event.target.value && set("month", event.target.value)}
            />
          </label>
          <label className="field">
            <span>Store</span>
            <select
              className="input"
              data-testid="margin-filter-store"
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
            <span>Brand</span>
            <select
              className="input"
              data-testid="margin-filter-brand"
              value={filters.brand}
              disabled={disabled}
              onChange={(event) => set("brand", event.target.value)}
            >
              <option value="">Every brand</option>
              {oneBrand && !brands.some((b) => b.id === String(oneBrand.id)) && (
                <option value={String(oneBrand.id)}>{oneBrand.name}</option>
              )}
              {brands.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="margin-export"
            disabled={disabled}
            onClick={() => void exportWorkbook()}
          >
            Export to Excel
          </button>
        </div>
        <h2 className="h3" data-testid="margin-heading">
          {oneBrand ? `Statement for ${oneBrand.name}` : "Every brand"} · {data.month}
        </h2>
        <p className="muted-cell" data-testid="margin-as-of">
          {asOfText(data.as_of)}
          {data.as_of && ` · the copy is brought up to date every ${data.refreshed_every_minutes} minutes`}
          {` · ${data.stores.map((s) => s.code).join(", ")} · ${data.date_from} to ${data.date_to}`}
        </p>
        {error && (
          <p className="warn-note" data-testid="margin-error">
            {error}
          </p>
        )}
        {data.missing.length > 0 && (
          <div className="warn-note" data-testid="margin-missing">
            <strong>Not included, or to check</strong>
            <ul>
              {data.missing.map((m) => (
                <li key={m.code} data-testid={`margin-missing-${m.code}`}>
                  {m.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {data.unknown_brands.length > 0 && (
          <div className="warn-note" data-testid="margin-unknown">
            <strong>Listed, not split: the model or margin is unknown</strong>
            <ul>
              {data.unknown_brands.map((u) => (
                <li key={u.key} data-testid="margin-unknown-brand" data-key={u.key}>
                  {u.label}: {u.lines} {u.lines === 1 ? "line" : "lines"}, {gstCell(u.value_paise, "money")} (
                  {u.reasons.join("; ")})
                </li>
              ))}
            </ul>
          </div>
        )}
        {data.rows.length === 0 ? (
          <p className="muted-cell" data-testid="margin-empty">
            {busy ? "Loading…" : "No SOR or concession sales with a split in this month at these stores."}
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data" data-testid="margin-table">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th
                      key={c.key}
                      className={alignsRight(c.kind) ? "num" : undefined}
                      data-testid={`margin-col-${c.key}`}
                    >
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <MarginRow
                    key={row.key}
                    row={row}
                    columns={columns}
                    open={oneBrand ? undefined : (id) => set("brand", id)}
                    disabled={disabled}
                  />
                ))}
              </tbody>
              <tfoot>
                <MarginRow row={data.total} columns={columns} total />
              </tfoot>
            </table>
          </div>
        )}
        <details className="muted-cell" data-testid="margin-basis">
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

function MarginRow({
  row,
  columns,
  total,
  open,
  disabled,
}: {
  row: Row;
  columns: Column[];
  total?: boolean;
  open?: (brandId: string) => void;
  disabled?: boolean;
}) {
  const cells = row as unknown as Record<string, unknown>;
  const brandId = open ? brandIdOf(row.key) : null;
  return (
    <tr data-testid={total ? "margin-total" : "margin-row"} data-key={row.key}>
      {columns.map((c, index) => {
        const value = cells[c.key];
        // The total row names itself in its first cell and leaves the words blank.
        const text = total && (value === undefined || value === "") ? "" : gstCell(value, c.kind);
        let content: ReactNode = text;
        if (total && index === 0) content = <strong>{text}</strong>;
        else if (index === 0 && brandId && open) {
          content = (
            <button
              type="button"
              className="link-btn"
              data-testid="margin-open-brand"
              disabled={disabled}
              onClick={() => open(brandId)}
            >
              {text}
            </button>
          );
        }
        return (
          <td
            key={c.key}
            className={alignsRight(c.kind) ? "num tabular" : undefined}
            data-testid={`margin-cell-${c.key}`}
          >
            {content}
          </td>
        );
      })}
    </tr>
  );
}
